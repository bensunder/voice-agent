"""Business logic behind each tool the Foundry voice agent can call.

Every function takes the already-resolved call context (`Ctx`) - the lead is
derived from the signed call token, never from model-supplied identifiers.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from psycopg.types.json import Jsonb

from ..container import Container
from ..db import audit, enqueue
from ..domain.qualification import SLOT_SCHEMA, SlotUpdate, score, value_opportunity
from ..domain.scheduling import find_candidates, parse_slot_id, spoken
from ..integrations.http import IntegrationError
from ..security import TokenError, verify_call_token
from .errors import Conflict, NotFound, ServiceError, Unauthorized, Unavailable

log = logging.getLogger(__name__)

FINISHED = ("completed", "failed", "cancelled", "blocked")


@dataclass(frozen=True)
class Ctx:
    lead_id: uuid.UUID
    attempt_id: uuid.UUID
    channel: str


async def resolve(c: Container, call_token: str) -> Ctx:
    """Map the agent's call token to the active call attempt."""
    s = c.settings
    token = (call_token or "").strip()
    async with c.pool.connection() as conn, conn.transaction():
        if token.lower() == "browser":
            if not s.demo_mode:
                raise Unauthorized("INVALID_CALL_TOKEN", "browser sessions are disabled")
            cur = await conn.execute(
                "SELECT id, lead_id, channel, status FROM call_attempt WHERE channel='browser'"
                " AND status IN ('queued','in_progress') AND armed_until > now()"
                " ORDER BY created_at DESC LIMIT 1"
            )
            row = await cur.fetchone()
            if not row:
                raise Unauthorized("NO_ARMED_SESSION", "no browser demo session is armed in the cockpit")
        else:
            try:
                claims = verify_call_token(s.call_token_secret.get_secret_value(), token)
            except TokenError as exc:
                raise Unauthorized("INVALID_CALL_TOKEN", str(exc)) from exc
            cur = await conn.execute(
                "SELECT id, lead_id, channel, status FROM call_attempt WHERE id=%s AND lead_id=%s",
                (claims.attempt_id, claims.lead_id),
            )
            row = await cur.fetchone()
            if not row:
                raise Unauthorized("INVALID_CALL_TOKEN", "unknown call")
        if row["status"] in FINISHED:
            raise Conflict("CALL_FINISHED", "this call has already ended")
        if row["status"] in ("queued", "dialing"):
            await conn.execute(
                "UPDATE call_attempt SET status='in_progress', started_at=COALESCE(started_at, now()),"
                " updated_at=now() WHERE id=%s",
                (row["id"],),
            )
            await audit(conn, "call_connected", lead_id=row["lead_id"], attempt_id=row["id"],
                        detail={"channel": row["channel"]})
    return Ctx(row["lead_id"], row["id"], row["channel"])


async def _slots(conn: Any, attempt_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    cur = await conn.execute(
        "SELECT name, value, confidence, evidence FROM qualification_slot WHERE attempt_id=%s", (attempt_id,)
    )
    return {r["name"]: {"value": r["value"], "confidence": r["confidence"], "evidence": r["evidence"]}
            for r in await cur.fetchall()}


async def get_lead_context(c: Container, ctx: Ctx) -> dict[str, Any]:
    async with c.pool.connection() as conn:
        cur = await conn.execute("SELECT first_name, company, product_interest FROM lead WHERE id=%s", (ctx.lead_id,))
        lead = await cur.fetchone()
        if not lead:
            raise NotFound("LEAD_NOT_FOUND", "lead not found")
        slots = await _slots(conn, ctx.attempt_id)
    remaining = [{"slot": k, "label": label} for k, (_, label) in SLOT_SCHEMA.items() if k not in slots]
    return {
        "first_name": lead["first_name"],
        "company": lead["company"],
        "product_interest": lead["product_interest"],
        "known_answers": {k: v["value"] for k, v in slots.items()},
        "still_to_ask": remaining,
    }


async def save_answers(c: Container, ctx: Ctx, update: SlotUpdate) -> dict[str, Any]:
    async with c.pool.connection() as conn, conn.transaction():
        for name, sv in update.slots.items():
            await conn.execute(
                """INSERT INTO qualification_slot (attempt_id, name, value, confidence, evidence)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (attempt_id, name) DO UPDATE SET value=EXCLUDED.value,
                     confidence=EXCLUDED.confidence, evidence=EXCLUDED.evidence, updated_at=now()""",
                (ctx.attempt_id, name, Jsonb(sv.value), sv.confidence, sv.evidence),
            )
        slots = await _slots(conn, ctx.attempt_id)
        result = score(slots, min_lines=c.price_book.min_lines)
        valuation = value_opportunity(slots, c.price_book)
        val_json = valuation.__dict__ if valuation else None
        await conn.execute(
            """INSERT INTO score_result (attempt_id, score, band, reason_codes, missing, verify,
                                         rubric_version, valuation, recommended_action)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (attempt_id) DO UPDATE SET score=EXCLUDED.score, band=EXCLUDED.band,
                 reason_codes=EXCLUDED.reason_codes, missing=EXCLUDED.missing, verify=EXCLUDED.verify,
                 rubric_version=EXCLUDED.rubric_version, valuation=EXCLUDED.valuation,
                 recommended_action=EXCLUDED.recommended_action, updated_at=now()""",
            (ctx.attempt_id, result.score, result.band.value, result.reason_codes, result.missing,
             result.verify, result.rubric_version, Jsonb(val_json) if val_json else None,
             result.recommended_action),
        )
        await audit(conn, "answers_saved", lead_id=ctx.lead_id, attempt_id=ctx.attempt_id,
                    detail={"slots": {k: v.value for k, v in update.slots.items()},
                            "score": result.score, "band": result.band.value,
                            "contract_value": valuation.contract_value if valuation else None})
    return {
        "band": result.band.value,
        "score": result.score,
        "recommended_action": result.recommended_action,
        "still_to_ask": [{"slot": m, "label": SLOT_SCHEMA[m][1]} for m in result.missing],
        "confirm_with_caller": result.verify,
    }


async def _held(conn: Any) -> set[tuple[str, datetime]]:
    cur = await conn.execute(
        "SELECT rep_upn, slot_start FROM slot_hold WHERE status='booked'"
        " OR (status='held' AND expires_at > now())"
    )
    return {(r["rep_upn"], r["slot_start"]) for r in await cur.fetchall()}


async def _busy(c: Container, start: datetime, end: datetime) -> dict[str, list[tuple[datetime, datetime]]]:
    if not c.graph:
        return {}
    busy: dict[str, list[tuple[datetime, datetime]]] = {}
    for rep in c.settings.reps:
        try:
            blocks = await c.graph.busy_blocks(rep.upn, start, end)
        except IntegrationError as exc:
            log.warning("calendar read failed for %s: %s", rep.upn, exc)
            raise Unavailable("CALENDAR_UNAVAILABLE", str(exc),
                              say="I'm having trouble reaching the calendar. Offer a callback instead.") from exc
        busy[rep.upn] = [(b.start, b.end) for b in blocks]
    return busy


def _rep_order(c: Container, ctx: Ctx) -> list[tuple[str, str]]:
    reps = [(r.upn, r.display_name) for r in c.settings.reps] or [("unassigned@local", "the specialist team")]
    k = ctx.lead_id.int % len(reps)  # stable round-robin per lead
    return reps[k:] + reps[:k]


async def get_offer_slots(c: Container, ctx: Ctx) -> dict[str, Any]:
    s = c.settings
    now = datetime.now(timezone.utc)
    busy = await _busy(c, now, now + timedelta(days=s.offer_days_ahead + 2))
    async with c.pool.connection() as conn:
        held = await _held(conn)
    cands = find_candidates(
        now_utc=now, tz=s.tz, reps=_rep_order(c, ctx), busy=busy, held=held,
        days_ahead=s.offer_days_ahead, work_start_hour=s.workday_start_hour,
        work_end_hour=s.workday_end_hour, minutes=s.meeting_minutes,
    )
    if not cands:
        return {"slots": [], "say": "No open times in the next few days; offer a callback instead."}
    async with c.pool.connection() as conn, conn.transaction():
        await audit(conn, "slots_offered", lead_id=ctx.lead_id, attempt_id=ctx.attempt_id,
                    detail={"slots": [{"rep": x.rep_name, "start": x.start.isoformat()} for x in cands],
                            "calendar_source": "graph" if c.graph else "business_hours"})
    return {
        "slots": [
            {"slot_id": x.slot_id, "when": spoken(x.start, s.tz), "with": x.rep_name,
             "duration_minutes": s.meeting_minutes}
            for x in cands
        ],
    }


async def hold_slot(c: Container, ctx: Ctx, slot_id: str) -> dict[str, Any]:
    s = c.settings
    try:
        rep_upn, start = parse_slot_id(slot_id)
    except ValueError as exc:
        raise ServiceError("INVALID_SLOT", "slot_id is not valid; call get_offer_slots again") from exc
    if rep_upn not in {r.upn for r in s.reps} and s.reps:
        raise ServiceError("INVALID_SLOT", "unknown rep in slot_id")
    end = start + timedelta(minutes=s.meeting_minutes)
    if start < datetime.now(timezone.utc) + timedelta(minutes=15):
        raise Conflict("SLOT_TOO_SOON", "that time has passed; offer new slots", say="Offer fresh times.")
    if c.graph:  # re-check the live calendar: someone may have booked it since it was offered
        busy = (await _busy(c, start, end)).get(rep_upn, [])
        if any(b0 < end and start < b1 for b0, b1 in busy):
            raise Conflict("SLOT_TAKEN", "that time was just taken", say="Apologise and offer the other time.")
    hold_id = uuid.uuid4()
    async with c.pool.connection() as conn, conn.transaction():
        # release this call's previous holds; expire stale holds so they cannot block the index
        await conn.execute(
            "UPDATE slot_hold SET status='released' WHERE attempt_id=%s AND status='held'", (ctx.attempt_id,)
        )
        await conn.execute("UPDATE slot_hold SET status='expired' WHERE status='held' AND expires_at <= now()")
        cur = await conn.execute(
            """INSERT INTO slot_hold (id, attempt_id, rep_upn, slot_start, slot_end, status, expires_at)
               VALUES (%s,%s,%s,%s,%s,'held', now() + make_interval(secs => %s))
               ON CONFLICT (rep_upn, slot_start) WHERE status IN ('held','booked') DO NOTHING
               RETURNING id, expires_at""",
            (hold_id, ctx.attempt_id, rep_upn, start, end, s.slot_hold_seconds),
        )
        row = await cur.fetchone()
        if not row:
            raise Conflict("SLOT_TAKEN", "that time was just taken", say="Apologise and offer the other time.")
        await audit(conn, "slot_held", lead_id=ctx.lead_id, attempt_id=ctx.attempt_id,
                    detail={"rep": rep_upn, "start": start.isoformat(), "expires_at": row["expires_at"].isoformat()})
    return {"hold_id": str(hold_id), "when": spoken(start, s.tz), "held_for_seconds": s.slot_hold_seconds}


async def book_meeting(c: Container, ctx: Ctx, hold_id: str, attendee_email: str | None) -> dict[str, Any]:
    s = c.settings
    try:
        hid = uuid.UUID(hold_id)
    except ValueError as exc:
        raise ServiceError("INVALID_HOLD", "hold_id is not valid") from exc
    async with c.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT h.*, l.first_name, l.last_name, l.company, l.email, l.phone_e164"
            " FROM slot_hold h JOIN call_attempt a ON a.id=h.attempt_id JOIN lead l ON l.id=a.lead_id"
            " WHERE h.id=%s AND h.attempt_id=%s",
            (hid, ctx.attempt_id),
        )
        hold = await cur.fetchone()
        cur = await conn.execute("SELECT * FROM meeting WHERE attempt_id=%s", (ctx.attempt_id,))
        existing = await cur.fetchone()
    if existing and existing["status"] in ("booked", "pending_graph"):
        return {"booked": True, "when": spoken(existing["starts_at"], s.tz), "already_booked": True}
    if not hold:
        raise NotFound("HOLD_NOT_FOUND", "no such hold for this call")
    if hold["status"] != "held" or hold["expires_at"] <= datetime.now(timezone.utc):
        raise Conflict("HOLD_EXPIRED", "the hold expired", say="Re-check availability with get_offer_slots.")

    contact = f"{hold['first_name']} {hold['last_name']}".strip()
    email = attendee_email or hold["email"]
    join_url = event_id = None
    status = "pending_graph"
    if c.graph:
        sc = await _score_snapshot(c, ctx)
        body = (
            f"<p>Discovery call booked by the AI sales assistant.</p>"
            f"<p><b>{escape(contact)}</b>, {escape(hold['company'])} ({escape(hold['phone_e164'])})</p>"
            f"<p>Lead score: <b>{sc.get('score', '-')}</b> ({escape(str(sc.get('band', '-')))})</p>"
            f"<p><a href=\"{escape(s.public_cockpit_url)}/#/attempt/{ctx.attempt_id}\">Open call briefing</a></p>"
        )
        try:
            ev = await c.graph.book_teams_meeting(
                organizer_upn=hold["rep_upn"],
                subject=f"{s.company_name} x {hold['company']}: enterprise wireless discovery",
                body_html=body, start=hold["slot_start"], end=hold["slot_end"],
                attendee_email=email, attendee_name=contact, transaction_id=str(hid),
            )
        except IntegrationError as exc:
            log.error("Graph booking failed for hold %s: %s", hid, exc)
            raise Unavailable("BOOKING_FAILED", str(exc),
                              say="Tell the caller the specialist will email to confirm the time.") from exc
        join_url, event_id, status = ev.join_url, ev.event_id, "booked"

    async with c.pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE slot_hold SET status='booked' WHERE id=%s", (hid,))
        await conn.execute(
            """INSERT INTO meeting (attempt_id, lead_id, rep_upn, starts_at, ends_at, graph_event_id, join_url, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (attempt_id) DO UPDATE SET rep_upn=EXCLUDED.rep_upn, starts_at=EXCLUDED.starts_at,
                 ends_at=EXCLUDED.ends_at, graph_event_id=EXCLUDED.graph_event_id,
                 join_url=EXCLUDED.join_url, status=EXCLUDED.status""",
            (ctx.attempt_id, ctx.lead_id, hold["rep_upn"], hold["slot_start"], hold["slot_end"],
             event_id, join_url, status),
        )
        await conn.execute("UPDATE lead SET owner_upn=%s, status='meeting_booked', updated_at=now() WHERE id=%s",
                           (hold["rep_upn"], ctx.lead_id))
        await audit(conn, "meeting_booked", lead_id=ctx.lead_id, attempt_id=ctx.attempt_id,
                    detail={"rep": hold["rep_upn"], "start": hold["slot_start"].isoformat(),
                            "teams_join_url": join_url, "calendar": "graph" if c.graph else "not_connected"})
    result = {"booked": True, "when": spoken(hold["slot_start"], s.tz), "teams_meeting": bool(join_url)}
    if not c.graph:
        result["say"] = "Confirm the time; say the specialist will send the calendar invite."
    elif not email:
        result["say"] = "The meeting is on the specialist's calendar; they will send the invite."
    return result


async def _score_snapshot(c: Container, ctx: Ctx) -> dict[str, Any]:
    async with c.pool.connection() as conn:
        cur = await conn.execute("SELECT score, band FROM score_result WHERE attempt_id=%s", (ctx.attempt_id,))
        return await cur.fetchone() or {}


async def request_transfer(c: Container, ctx: Ctx, reason: str) -> dict[str, Any]:
    """Records the hand-off. The live transfer itself is executed by the Foundry
    voice agent's configured Teams transfer target (the rep's Teams identity)."""
    async with c.pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT band, score FROM score_result WHERE attempt_id=%s", (ctx.attempt_id,))
        sc = await cur.fetchone()
        if not sc or sc["band"] != "hot":
            raise Conflict("NOT_ELIGIBLE", "only hot leads are transferred live",
                           say="Offer to book a meeting instead.")
        await audit(conn, "transfer_requested", lead_id=ctx.lead_id, attempt_id=ctx.attempt_id,
                    detail={"reason": reason[:200], "score": sc["score"]})
        await enqueue(conn, "rep.notify", f"rep.notify:{ctx.attempt_id}:transfer",
                      {"attempt_id": str(ctx.attempt_id), "kind": "transfer"})
    return {"transfer": "approved", "target": "sales_specialist"}


async def schedule_callback(c: Container, ctx: Ctx, preferred_time: str, note: str | None) -> dict[str, Any]:
    async with c.pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE call_attempt SET outcome='callback_requested', next_action=%s, updated_at=now() WHERE id=%s",
            (f"Call back: {preferred_time[:120]}" + (f" ({note[:200]})" if note else ""), ctx.attempt_id),
        )
        await audit(conn, "callback_scheduled", lead_id=ctx.lead_id, attempt_id=ctx.attempt_id,
                    detail={"preferred_time": preferred_time[:120], "note": (note or "")[:200]})
    return {"scheduled": True}


async def opt_out(c: Container, ctx: Ctx) -> dict[str, Any]:
    async with c.pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT phone_e164 FROM lead WHERE id=%s", (ctx.lead_id,))
        lead = await cur.fetchone()
        await conn.execute(
            "INSERT INTO opt_out (tenant_key, phone_e164, source) VALUES (%s,%s,'voice_agent')"
            " ON CONFLICT DO NOTHING",
            (c.settings.tenant_key, lead["phone_e164"]),
        )
        await conn.execute(
            "UPDATE call_attempt SET outcome='opted_out', next_action='Do not contact', updated_at=now()"
            " WHERE id=%s", (ctx.attempt_id,),
        )
        await conn.execute("UPDATE lead SET status='opted_out', updated_at=now() WHERE id=%s", (ctx.lead_id,))
        await conn.execute(
            "UPDATE call_attempt SET status='cancelled', terminal_reason='OPTED_OUT', updated_at=now()"
            " WHERE lead_id=%s AND id<>%s AND status IN ('queued','dialing')",
            (ctx.lead_id, ctx.attempt_id),
        )
        await audit(conn, "opted_out", lead_id=ctx.lead_id, attempt_id=ctx.attempt_id,
                    detail={"effective": "immediately, all campaigns"})
    return {"opted_out": True, "say": "Confirm they will not be called again, thank them, and end the call."}


async def complete_call(c: Container, ctx: Ctx, outcome: str, summary: str, next_action: str | None) -> dict[str, Any]:
    async with c.pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT outcome FROM call_attempt WHERE id=%s", (ctx.attempt_id,))
        prior = (await cur.fetchone())["outcome"]
        final_outcome = prior if prior == "opted_out" else outcome
        new_status = "completed" if ctx.channel == "browser" else "in_progress"
        await conn.execute(
            "UPDATE call_attempt SET outcome=%s, summary=%s, next_action=COALESCE(%s, next_action),"
            " status=%s, ended_at=CASE WHEN %s='completed' THEN now() ELSE ended_at END, updated_at=now()"
            " WHERE id=%s",
            (final_outcome, summary[:4000], next_action[:400] if next_action else None,
             new_status, new_status, ctx.attempt_id),
        )
        await audit(conn, "call_summarized", lead_id=ctx.lead_id, attempt_id=ctx.attempt_id,
                    detail={"outcome": final_outcome, "summary": summary[:600]})
        await enqueue_finalize(conn, ctx.attempt_id)
    return {"recorded": True}


async def enqueue_finalize(conn: Any, attempt_id: uuid.UUID) -> None:
    """Post-call fan-out. Dedupe keys make this safe to call from both the
    agent's complete_call and the worker's call-ended detection."""
    await enqueue(conn, "dataverse.upsert", f"dataverse.upsert:{attempt_id}:final", {"attempt_id": str(attempt_id)})
    await enqueue(conn, "rep.notify", f"rep.notify:{attempt_id}:final", {"attempt_id": str(attempt_id), "kind": "final"})
