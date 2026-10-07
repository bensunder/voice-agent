"""Cockpit: the human-facing live view (lead form, live call, briefing, audit).

Protected by HTTP basic auth. Live updates are pushed with Server-Sent Events
fed by a single Postgres LISTEN connection fanned out to subscribers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from importlib import resources
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from ..config import integration_status
from ..container import Container
from ..domain.qualification import SLOT_SCHEMA
from ..services import briefing, leads
from ..services.errors import ServiceError, Unauthorized
from ..security import constant_time_equals
from .common import install, lifespan_factory

log = logging.getLogger("salesagent.cockpit")
basic = HTTPBasic(auto_error=False, realm="AI Sales Cockpit")


async def require_user(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)) -> str:
    c: Container = request.app.state.c
    ok = creds is not None and constant_time_equals(creds.username, c.settings.cockpit_username) and \
        constant_time_equals(creds.password, c.settings.cockpit_password.get_secret_value())
    if not ok:
        raise Unauthorized("AUTH_REQUIRED", "authentication required")
    return creds.username  # type: ignore[union-attr]


class Broadcaster:
    """One LISTEN connection, many SSE subscribers. Reconnects on failure."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="sse-broadcaster")

    async def aclose(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
                    await conn.execute("LISTEN sales_events")
                    backoff = 1.0
                    async for n in conn.notifies():
                        msg = json.loads(n.payload)
                        for q in list(self._subs):
                            if q.full():  # slow consumer: drop it rather than block everyone
                                self._subs.discard(q)
                                continue
                            q.put_nowait(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("LISTEN connection lost (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


def _c(request: Request) -> Container:
    return request.app.state.c


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    c = _c(request)
    s = c.settings
    async with c.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT a.id, a.channel, a.status, a.outcome, a.gate_reason, a.created_at, a.armed_until,
                      l.id AS lead_id, l.first_name, l.last_name, l.company, l.phone_e164,
                      sr.score, sr.band, sr.valuation->>'contract_value' AS contract_value,
                      m.starts_at AS meeting_start
               FROM call_attempt a JOIN lead l ON l.id=a.lead_id
               LEFT JOIN score_result sr ON sr.attempt_id=a.id
               LEFT JOIN meeting m ON m.attempt_id=a.id
               WHERE l.tenant_key=%s ORDER BY a.created_at DESC LIMIT 25""",
            (s.tenant_key,),
        )
        attempts = await cur.fetchall()
        cur = await conn.execute(
            "SELECT id, first_name, last_name, company, phone_e164, status, created_at FROM lead"
            " WHERE tenant_key=%s ORDER BY created_at DESC LIMIT 25", (s.tenant_key,))
        lead_rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT count(*) FILTER (WHERE status='pending') AS pending,"
            " count(*) FILTER (WHERE status='dead') AS dead FROM outbox")
        outbox = await cur.fetchone()
    return {
        "company_name": s.company_name,
        "demo_mode": s.demo_mode,
        "allowlist_size": len(s.allowlist),
        "integrations": integration_status(s),
        "reps": [{"upn": r.upn, "name": r.display_name} for r in s.reps],
        "slot_labels": {k: v[1] for k, v in SLOT_SCHEMA.items()},
        "attempts": attempts,
        "leads": lead_rows,
        "outbox": outbox,
    }


@router.get("/attempts/{attempt_id}")
async def attempt_detail(attempt_id: uuid.UUID, request: Request) -> dict[str, Any]:
    c = _c(request)
    b = await briefing.build(c, attempt_id)
    async with c.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT name, value, confidence, evidence, updated_at FROM qualification_slot WHERE attempt_id=%s"
            " ORDER BY updated_at", (attempt_id,))
        slots = await cur.fetchall()
        cur = await conn.execute(
            "SELECT id, kind, detail, created_at FROM audit_event WHERE attempt_id=%s ORDER BY id", (attempt_id,))
        events = await cur.fetchall()
        cur = await conn.execute(
            "SELECT status, foundry_call_job_id, foundry_status, terminal_reason, gate_reason, channel"
            " FROM call_attempt WHERE id=%s", (attempt_id,))
        attempt = await cur.fetchone()
    return {"briefing": b, "slots": slots, "events": events, "attempt": attempt}


@router.post("/leads", status_code=201)
async def create_lead(body: leads.LeadIn, request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
    return await leads.create_lead(_c(request), body, source=f"cockpit:{user}")


@router.post("/leads/{lead_id}/call")
async def call_lead(lead_id: uuid.UUID, request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
    return await leads.start_teams_call(_c(request), lead_id, requested_by=user)


@router.post("/leads/{lead_id}/browser-session")
async def browser_session(lead_id: uuid.UUID, request: Request) -> dict[str, Any]:
    return await leads.arm_browser_session(_c(request), lead_id)


@router.post("/demo/reset")
async def demo_reset(request: Request, user: str = Depends(require_user)) -> dict[str, str]:
    c = _c(request)
    if not c.settings.demo_mode:
        raise ServiceError("DEMO_MODE_OFF", "reset is only available in demo mode")
    async with c.pool.connection() as conn, conn.transaction():
        await conn.execute("TRUNCATE outbox, audit_event, meeting, slot_hold, score_result,"
                           " qualification_slot, call_attempt, opt_out, lead RESTART IDENTITY")
        await conn.execute("INSERT INTO audit_event (kind, detail) VALUES ('demo_reset', %s)",
                           (Jsonb({"by": user}),))
    return {"status": "reset"}


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    c = _c(request)
    bc: Broadcaster = request.app.state.broadcaster
    q = bc.subscribe()

    async def stream() -> AsyncIterator[bytes]:
        try:
            yield b"retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                async with c.pool.connection() as conn:
                    cur = await conn.execute(
                        "SELECT id, lead_id, attempt_id, kind, detail, created_at FROM audit_event WHERE id=%s",
                        (msg["id"],))
                    row = await cur.fetchone()
                if row:
                    yield f"id: {row['id']}\nevent: audit\ndata: {json.dumps(row, default=str)}\n\n".encode()
        finally:
            bc.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _start_broadcaster(app: FastAPI, c: Container) -> Broadcaster:
    bc = Broadcaster(c.settings.database_url.get_secret_value())
    bc.start()
    app.state.broadcaster = bc
    return bc


def create_app() -> FastAPI:
    app = FastAPI(title="AI Sales Cockpit", lifespan=lifespan_factory(pool_size=8, on_start=_start_broadcaster),
                  docs_url=None, redoc_url=None, openapi_url=None)
    install(app)
    app.include_router(router)
    static_dir = resources.files("salesagent").joinpath("static/cockpit")
    app.mount("/assets", StaticFiles(directory=str(static_dir)), name="assets")

    @app.get("/", include_in_schema=False, dependencies=[Depends(require_user)])
    async def index() -> FileResponse:
        return FileResponse(str(static_dir.joinpath("index.html")), headers={"Cache-Control": "no-store"})

    @app.exception_handler(Unauthorized)
    async def _unauth(_: Request, exc: Unauthorized) -> Any:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="AI Sales Cockpit"'})

    return app


app = create_app()
