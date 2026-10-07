"""Background worker: outbox delivery, Teams call-job tracking, lease expiry.

Run as its own container (`python -m salesagent.worker`). Multiple replicas are
safe: outbox rows are claimed with FOR UPDATE SKIP LOCKED.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from datetime import timedelta
from typing import Any

from .config import get_settings
from .container import Container
from .db import audit, migrate
from .integrations.http import IntegrationError, NotConfigured
from .logging_setup import configure_logging
from .services import briefing
from .services.agent_tools import enqueue_finalize

log = logging.getLogger("salesagent.worker")


async def handle(c: Container, topic: str, payload: dict[str, Any]) -> str:
    attempt_id = uuid.UUID(payload["attempt_id"])
    b = await briefing.build(c, attempt_id)
    if topic == "dataverse.upsert":
        if not c.dataverse:
            raise NotConfigured("dataverse")
        await c.dataverse.upsert_qualification(str(attempt_id), b)
        return "dataverse_synced"
    if topic == "rep.notify":
        kind = payload.get("kind", "final")
        if not briefing.should_notify(b, kind):
            return "rep_notify_skipped"
        if not c.power_automate:
            raise NotConfigured("power_automate")
        await c.power_automate.notify_rep(briefing.rep_card_payload(b, kind), f"{attempt_id}:{kind}")
        return "rep_notified"
    raise ValueError(f"unknown topic {topic}")


async def process_outbox(c: Container, batch: int = 10) -> int:
    async with c.pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT * FROM outbox WHERE status='pending' AND next_attempt_at <= now()"
            " ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
            (batch,),
        )
        rows = await cur.fetchall()
        for row in rows:
            attempt_id = row["payload"].get("attempt_id")
            try:
                result = await handle(c, row["topic"], row["payload"])
            except NotConfigured as exc:
                # Not an error worth retrying hot: park it, re-check every 10 minutes.
                await conn.execute(
                    "UPDATE outbox SET last_error=%s, next_attempt_at=now()+interval '10 minutes',"
                    " updated_at=now() WHERE id=%s",
                    (str(exc), row["id"]),
                )
                continue
            except Exception as exc:  # noqa: BLE001 - every failure must be recorded, never lost
                attempts = row["attempts"] + 1
                dead = attempts >= c.settings.outbox_max_attempts or (
                    isinstance(exc, IntegrationError) and not exc.retryable and (exc.status or 0) in (400, 401, 403, 404)
                )
                backoff = timedelta(seconds=min(900, 5 * 2**attempts))
                await conn.execute(
                    "UPDATE outbox SET attempts=%s, last_error=%s, status=%s,"
                    " next_attempt_at=now()+%s, updated_at=now() WHERE id=%s",
                    (attempts, str(exc)[:1000], "dead" if dead else "pending", backoff, row["id"]),
                )
                await audit(conn, "delivery_failed" if dead else "delivery_retry", attempt_id=attempt_id,
                            detail={"topic": row["topic"], "attempt": attempts, "error": str(exc)[:300]})
                log.warning("outbox %s (%s) failed attempt %s: %s", row["id"], row["topic"], attempts, exc)
                continue
            await conn.execute("UPDATE outbox SET status='done', attempts=attempts+1, updated_at=now()"
                               " WHERE id=%s", (row["id"],))
            await audit(conn, result, attempt_id=attempt_id, detail={"topic": row["topic"]})
        return len(rows)


async def track_calls(c: Container) -> None:
    if not c.channel:
        return
    async with c.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, lead_id, foundry_call_job_id, foundry_status FROM call_attempt"
            " WHERE channel='teams_phone' AND foundry_call_job_id IS NOT NULL"
            " AND status IN ('dialing','in_progress') LIMIT 50"
        )
        active = await cur.fetchall()
    for a in active:
        try:
            state = await c.channel.get_call(a["foundry_call_job_id"])
        except IntegrationError as exc:
            log.warning("get_call failed for %s: %s", a["id"], exc)
            continue
        if state.status == a["foundry_status"] and not state.terminal:
            continue
        async with c.pool.connection() as conn, conn.transaction():
            if state.terminal:
                final = "completed" if state.status == "completed" else "failed"
                await conn.execute(
                    "UPDATE call_attempt SET status=%s, foundry_status=%s, terminal_reason=%s,"
                    " outcome=COALESCE(outcome, %s), ended_at=COALESCE(ended_at, now()), updated_at=now()"
                    " WHERE id=%s",
                    (final, state.status, state.terminal_reason,
                     None if final == "completed" else (state.terminal_reason or "not_connected"), a["id"]),
                )
                await audit(conn, "call_ended", lead_id=a["lead_id"], attempt_id=a["id"],
                            detail={"status": state.status, "reason": state.terminal_reason})
                await enqueue_finalize(conn, a["id"])
            else:
                await conn.execute(
                    "UPDATE call_attempt SET foundry_status=%s, updated_at=now() WHERE id=%s",
                    (state.status, a["id"]),
                )
                await audit(conn, "call_status", lead_id=a["lead_id"], attempt_id=a["id"],
                            detail={"status": state.status})


async def expire_leases(c: Container) -> None:
    async with c.pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE slot_hold SET status='expired' WHERE status='held' AND expires_at <= now()")
        cur = await conn.execute(
            "UPDATE call_attempt SET status='cancelled', terminal_reason='SESSION_EXPIRED', ended_at=now(),"
            " updated_at=now() WHERE channel='browser' AND status IN ('queued','in_progress')"
            " AND armed_until <= now() RETURNING id, lead_id"
        )
        for r in await cur.fetchall():
            await audit(conn, "browser_session_expired", lead_id=r["lead_id"], attempt_id=r["id"])
            await enqueue_finalize(conn, r["id"])


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    await migrate(settings.database_url.get_secret_value())
    c = await Container.create(settings, pool_size=4)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker started")
    tick = 0
    try:
        while not stop.is_set():
            try:
                busy = await process_outbox(c)
                if tick % 2 == 0:
                    await track_calls(c)
                if tick % 5 == 0:
                    await expire_leases(c)
            except Exception:  # noqa: BLE001 - keep the loop alive; log with traceback
                log.exception("worker iteration failed")
                busy = 0
            tick += 1
            if not busy:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
                except TimeoutError:
                    pass
    finally:
        await c.close()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
