"""Lead intake, compliance gating and call initiation."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, EmailStr, Field, field_validator

from ..container import Container
from ..db import audit
from ..domain import compliance
from ..integrations.http import IntegrationError
from ..security import mint_call_token
from .errors import Conflict, NotFound, ServiceError, Unavailable

log = logging.getLogger(__name__)

CONSENT_TEXT = (
    "I agree to receive a call, which may be placed by an AI assistant and recorded, "
    "about the product I requested information on."
)


class LeadIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(default="", max_length=80)
    company: str = Field(min_length=1, max_length=160)
    phone: str = Field(min_length=7, max_length=32)
    email: EmailStr | None = None
    timezone: str | None = None
    consent: bool

    @field_validator("first_name", "last_name", "company")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("consent")
    @classmethod
    def _must_consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("consent is required before the lead can be called")
        return v


async def create_lead(c: Container, data: LeadIn, source: str) -> dict[str, Any]:
    try:
        phone = compliance.normalize_us_phone(data.phone)
    except ValueError as exc:
        raise ServiceError("INVALID_PHONE", str(exc)) from exc
    tz_name = data.timezone or c.settings.business_timezone
    try:
        ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001 - zoneinfo raises several types
        raise ServiceError("INVALID_TIMEZONE", f"unknown timezone {tz_name}") from exc

    async with c.pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO lead (tenant_key, first_name, last_name, company, phone_e164, email, timezone,
                              product_interest, consent_source, consent_text, consent_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (tenant_key, phone_e164) DO UPDATE SET
                first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name,
                company = EXCLUDED.company, email = COALESCE(EXCLUDED.email, lead.email),
                timezone = EXCLUDED.timezone, consent_source = EXCLUDED.consent_source,
                consent_text = EXCLUDED.consent_text, consent_at = now(), updated_at = now()
            RETURNING id, first_name, company, phone_e164, (xmax = 0) AS inserted
            """,
            (
                c.settings.tenant_key, data.first_name, data.last_name, data.company, phone,
                str(data.email) if data.email else None, tz_name, c.settings.product_name,
                source, CONSENT_TEXT,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        await audit(conn, "lead_captured", lead_id=row["id"],
                    detail={"company": row["company"], "source": source, "new": row["inserted"]})
    return {"id": str(row["id"]), "first_name": row["first_name"], "company": row["company"],
            "phone_e164": row["phone_e164"]}


async def _load_lead(conn: Any, c: Container, lead_id: uuid.UUID, lock: bool = False) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT * FROM lead WHERE id = %s AND tenant_key = %s" + (" FOR UPDATE" if lock else ""),
        (lead_id, c.settings.tenant_key),
    )
    lead = await cur.fetchone()
    if not lead:
        raise NotFound("LEAD_NOT_FOUND", "lead not found")
    return lead


def agent_inputs(c: Container, lead: dict[str, Any], token: str) -> dict[str, Any]:
    """Structured inputs handed to the Foundry voice agent for this call.
    Deliberately minimal: no phone number, email or internal IDs."""
    return {
        "first_name": lead["first_name"],
        "company": lead["company"],
        "company_name": c.settings.company_name,
        "product_interest": lead["product_interest"],
        "call_token": token,
    }


async def start_teams_call(c: Container, lead_id: uuid.UUID, requested_by: str,
                           campaign_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Gate the lead, record the attempt, then place the call over Teams Phone."""
    s = c.settings
    now = datetime.now(timezone.utc)
    async with c.pool.connection() as conn, conn.transaction():
        lead = await _load_lead(conn, c, lead_id, lock=True)
        cur = await conn.execute(
            "SELECT 1 FROM opt_out WHERE tenant_key=%s AND phone_e164=%s", (s.tenant_key, lead["phone_e164"])
        )
        opted_out = await cur.fetchone() is not None
        cur = await conn.execute(
            "SELECT count(*) AS n FROM call_attempt WHERE lead_id=%s AND channel='teams_phone'"
            " AND created_at > now() - interval '24 hours' AND status <> 'blocked'",
            (lead_id,),
        )
        attempts_today = (await cur.fetchone())["n"]
        cur = await conn.execute(
            "SELECT 1 FROM call_attempt WHERE lead_id=%s AND channel='teams_phone'"
            " AND status IN ('queued','dialing','in_progress')",
            (lead_id,),
        )
        if await cur.fetchone():
            raise Conflict("CALL_IN_PROGRESS", "this lead already has a call in progress")

        decision = compliance.evaluate(
            compliance.GateInput(
                phone_e164=lead["phone_e164"],
                has_consent=bool(lead["consent_at"]),
                opted_out=opted_out,
                attempts_today=attempts_today,
                lead_timezone=ZoneInfo(lead["timezone"]),
                now_utc=now,
                demo_mode=s.demo_mode,
                allowlist=s.allowlist,
                window_start_hour=s.calling_window_start_hour,
                window_end_hour=s.calling_window_end_hour,
            )
        )
        attempt_id = uuid.uuid4()
        status = "queued" if decision.allowed else "blocked"
        await conn.execute(
            "INSERT INTO call_attempt (id, lead_id, channel, status, gate_reason, idempotency_key, campaign_id)"
            " VALUES (%s,%s,'teams_phone',%s,%s,%s,%s)",
            (attempt_id, lead_id, status, decision.reason, f"attempt:{attempt_id}", campaign_id),
        )
        await audit(conn, "gate_decision", lead_id=lead_id, attempt_id=attempt_id,
                    detail={"verdict": decision.verdict.value, "reason": decision.reason,
                            "retry_at": decision.retry_at_utc.isoformat() if decision.retry_at_utc else None,
                            "requested_by": requested_by})

    if not decision.allowed:
        return {"attempt_id": str(attempt_id), "status": "blocked", "reason": decision.reason}

    if c.channel is None:
        await _mark_failed(c, attempt_id, lead_id, "TEAMS_PHONE_NOT_CONFIGURED")
        raise Unavailable("TEAMS_PHONE_NOT_CONFIGURED",
                          "Teams Phone / Foundry telephony is not configured on this server")

    token = mint_call_token(s.call_token_secret.get_secret_value(), lead_id, attempt_id, s.call_token_ttl_seconds)
    try:
        job = await c.channel.place_call(
            idempotency_key=str(attempt_id), phone_e164=lead["phone_e164"], inputs=agent_inputs(c, lead, token)
        )
    except IntegrationError as exc:
        log.error("place_call failed for attempt %s: %s", attempt_id, exc)
        await _mark_failed(c, attempt_id, lead_id, f"PLACE_CALL_FAILED: {exc}")
        raise Unavailable("PLACE_CALL_FAILED", str(exc)) from exc

    async with c.pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE call_attempt SET status='dialing', foundry_call_job_id=%s, foundry_status=%s,"
            " started_at=now(), updated_at=now() WHERE id=%s",
            (job.job_id, job.status, attempt_id),
        )
        await audit(conn, "call_dialing", lead_id=lead_id, attempt_id=attempt_id,
                    detail={"call_job_id": job.job_id, "status": job.status, "channel": "teams_phone"})
    return {"attempt_id": str(attempt_id), "status": "dialing", "call_job_id": job.job_id}


async def _mark_failed(c: Container, attempt_id: uuid.UUID, lead_id: uuid.UUID, reason: str) -> None:
    async with c.pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE call_attempt SET status='failed', terminal_reason=%s, ended_at=now(), updated_at=now()"
            " WHERE id=%s",
            (reason[:500], attempt_id),
        )
        await audit(conn, "call_failed", lead_id=lead_id, attempt_id=attempt_id, detail={"reason": reason[:500]})


async def arm_browser_session(c: Container, lead_id: uuid.UUID) -> dict[str, Any]:
    """Demo mode: let the Foundry browser preview act as this lead's call.
    Only one session can be armed; arming a new one cancels the previous."""
    s = c.settings
    if not s.demo_mode:
        raise ServiceError("DEMO_MODE_OFF", "browser sessions are only available in demo mode")
    attempt_id = uuid.uuid4()
    async with c.pool.connection() as conn, conn.transaction():
        lead = await _load_lead(conn, c, lead_id)
        cur = await conn.execute(
            "SELECT 1 FROM opt_out WHERE tenant_key=%s AND phone_e164=%s", (s.tenant_key, lead["phone_e164"])
        )
        if await cur.fetchone():
            raise ServiceError("OPTED_OUT", "this lead has opted out")
        await conn.execute(
            "UPDATE call_attempt SET status='cancelled', terminal_reason='SUPERSEDED', ended_at=now(),"
            " updated_at=now() WHERE channel='browser' AND status IN ('queued','in_progress')"
        )
        await conn.execute(
            "INSERT INTO call_attempt (id, lead_id, channel, status, gate_reason, idempotency_key, armed_until)"
            " VALUES (%s,%s,'browser','queued','DEMO_BROWSER_SESSION',%s,%s)",
            (attempt_id, lead_id, f"attempt:{attempt_id}",
             datetime.now(timezone.utc) + timedelta(seconds=s.browser_session_ttl_seconds)),
        )
        await audit(conn, "browser_session_armed", lead_id=lead_id, attempt_id=attempt_id,
                    detail={"ttl_seconds": s.browser_session_ttl_seconds})
    token = mint_call_token(s.call_token_secret.get_secret_value(), lead_id, attempt_id, s.call_token_ttl_seconds)
    return {"attempt_id": str(attempt_id), "call_token": token, "inputs": agent_inputs(c, lead, "browser")}
