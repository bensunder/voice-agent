"""Campaign persistence. All state transitions are guarded by the current status so
they are idempotent and safe under concurrent workers."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from ..container import Container
from ..db import audit

IN_FLIGHT = ("dialing", "in_call")
OPEN = ("pending", "retry_wait", "dialing", "in_call")
SYNTHETIC_SOURCE = "synthetic:simulation"


class CampaignIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    mode: str = Field(pattern="^(live|simulation)$")
    lead_count: int = Field(default=1000, ge=1, le=20000, description="simulation only")
    max_concurrent: int = Field(default=25, ge=1, le=500)
    max_attempts: int = Field(default=3, ge=1, le=6)
    retry_seconds: list[int] | None = None
    escalation_daily_cap: int = Field(default=200, ge=0, le=100000)
    llm_budget_usd: float = Field(default=1.0, ge=0, le=1000)
    respect_calling_window: bool | None = None

    @model_validator(mode="after")
    def _defaults(self) -> "CampaignIn":
        if self.retry_seconds is None:
            # Simulation compresses hours into seconds so the demo shows retries happening.
            self.retry_seconds = [8, 20, 45] if self.mode == "simulation" else [4 * 3600, 24 * 3600, 48 * 3600]
        if not 1 <= len(self.retry_seconds) <= 6 or any(x < 1 for x in self.retry_seconds):
            raise ValueError("retry_seconds must have 1-6 positive values")
        if self.respect_calling_window is None:
            self.respect_calling_window = self.mode == "live"
        return self


@dataclass(frozen=True)
class Campaign:
    id: uuid.UUID
    name: str
    mode: str
    status: str
    max_concurrent: int
    max_attempts: int
    retry_seconds: list[int]
    escalation_daily_cap: int
    respect_calling_window: bool


def _campaign(row: dict[str, Any]) -> Campaign:
    return Campaign(row["id"], row["name"], row["mode"], row["status"], row["max_concurrent"],
                    row["max_attempts"], list(row["retry_seconds"]), row["escalation_daily_cap"],
                    row["respect_calling_window"])


async def load(conn: Any, campaign_id: uuid.UUID) -> Campaign | None:
    cur = await conn.execute("SELECT * FROM campaign WHERE id=%s", (campaign_id,))
    row = await cur.fetchone()
    return _campaign(row) if row else None


async def create(c: Container, data: CampaignIn) -> dict[str, Any]:
    s = c.settings
    async with c.pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """INSERT INTO campaign (tenant_key, name, mode, max_concurrent, max_attempts, retry_seconds,
                 escalation_daily_cap, llm_budget_usd, respect_calling_window)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (s.tenant_key, data.name, data.mode, data.max_concurrent, data.max_attempts, data.retry_seconds,
             data.escalation_daily_cap, data.llm_budget_usd, data.respect_calling_window),
        )
        cid = (await cur.fetchone())["id"]
        if data.mode == "simulation":
            await _seed_synthetic(conn, s.tenant_key, s.business_timezone, s.product_name, cid, data.lead_count)
        else:
            await conn.execute(
                """INSERT INTO campaign_lead (campaign_id, lead_id)
                   SELECT %s, l.id FROM lead l WHERE l.tenant_key=%s AND l.consent_source <> %s
                     AND NOT EXISTS (SELECT 1 FROM opt_out o WHERE o.tenant_key=l.tenant_key
                                     AND o.phone_e164=l.phone_e164)""",
                (cid, s.tenant_key, SYNTHETIC_SOURCE),
            )
        cur = await conn.execute("SELECT count(*) AS n FROM campaign_lead WHERE campaign_id=%s", (cid,))
        n = (await cur.fetchone())["n"]
        await audit(conn, "campaign_created", detail={"campaign_id": str(cid), "name": data.name,
                                                       "mode": data.mode, "leads": n})
    return {"id": str(cid), "leads": n}


async def _seed_synthetic(conn: Any, tenant: str, tz: str, product: str, cid: uuid.UUID, n: int) -> None:
    """Fictional numbers only: NXX-555-01XX is reserved for fiction in every US area code."""
    gen = """
        SELECT i,
               '+1' || (201 + (i / 100) %% 700)::text || '55501' || lpad((i %% 100)::text, 2, '0') AS phone,
               (ARRAY['Avery','Jordan','Taylor','Morgan','Riley','Casey','Quinn','Drew','Parker','Reese'])[1 + i %% 10] AS first_name,
               (ARRAY['Logistics','Health','Builders','Foods','Energy','Retail','Transit','Labs','Freight','Systems'])[1 + (i / 10) %% 10] AS sector
        FROM generate_series(0, %s - 1) AS g(i)
    """
    await conn.execute(
        f"""INSERT INTO lead (tenant_key, first_name, last_name, company, phone_e164, timezone, product_interest,
                              consent_source, consent_text, consent_at)
            SELECT %s, first_name, 'Sim', 'Sim ' || sector || ' ' || (i + 1)::text, phone, %s, %s, %s,
                   'Synthetic lead for simulation; never dialled.', now()
            FROM ({gen}) g
            ON CONFLICT (tenant_key, phone_e164) DO NOTHING""",
        (tenant, tz, product, SYNTHETIC_SOURCE, n),
    )
    await conn.execute(
        f"""INSERT INTO campaign_lead (campaign_id, lead_id, next_attempt_at)
            SELECT %s, l.id, now() FROM ({gen}) g JOIN lead l ON l.tenant_key=%s AND l.phone_e164=g.phone
            ON CONFLICT DO NOTHING""",
        (cid, n, tenant),
    )


async def set_status(c: Container, campaign_id: uuid.UUID, status: str) -> dict[str, str]:
    allowed = {"running": ("draft", "paused"), "paused": ("running",), "stopped": ("draft", "running", "paused")}
    async with c.pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "UPDATE campaign SET status=%s, started_at=COALESCE(started_at, CASE WHEN %s='running' THEN now() END),"
            " ended_at=CASE WHEN %s='stopped' THEN now() ELSE ended_at END"
            " WHERE id=%s AND status = ANY(%s) RETURNING id",
            (status, status, status, campaign_id, list(allowed[status])),
        )
        if not await cur.fetchone():
            return {"status": "unchanged"}
        await audit(conn, f"campaign_{status}", detail={"campaign_id": str(campaign_id)})
    return {"status": status}


async def running_campaigns(c: Container) -> list[uuid.UUID]:
    async with c.pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM campaign WHERE status='running' AND tenant_key=%s",
                                 (c.settings.tenant_key,))
        return [r["id"] for r in await cur.fetchall()]


async def capacity(conn: Any, camp: Campaign) -> tuple[int, str]:
    cur = await conn.execute(
        "SELECT count(*) FILTER (WHERE status = ANY(%s)) AS inflight,"
        " count(*) FILTER (WHERE status='escalated' AND updated_at >= date_trunc('day', now())) AS esc_today"
        " FROM campaign_lead WHERE campaign_id=%s",
        (list(IN_FLIGHT), camp.id),
    )
    row = await cur.fetchone()
    if row["esc_today"] >= camp.escalation_daily_cap:
        return 0, "REP_CAPACITY_REACHED"
    return max(0, camp.max_concurrent - row["inflight"]), "OK"


async def claim_due(conn: Any, camp: Campaign, n: int) -> list[dict[str, Any]]:
    if n <= 0:
        return []
    cur = await conn.execute(
        """UPDATE campaign_lead cl SET status='dialing', updated_at=now()
           FROM (SELECT lead_id FROM campaign_lead WHERE campaign_id=%s AND status IN ('pending','retry_wait')
                   AND next_attempt_at <= now()
                 ORDER BY priority DESC, next_attempt_at LIMIT %s FOR UPDATE SKIP LOCKED) due
           WHERE cl.campaign_id=%s AND cl.lead_id=due.lead_id
           RETURNING cl.lead_id, cl.attempts""",
        (camp.id, n, camp.id),
    )
    return list(await cur.fetchall())


async def mark_in_call(conn: Any, camp_id: uuid.UUID, lead_id: uuid.UUID, attempt_id: uuid.UUID) -> None:
    await conn.execute(
        "UPDATE campaign_lead SET status='in_call', attempts=attempts+1, last_attempt_id=%s, updated_at=now()"
        " WHERE campaign_id=%s AND lead_id=%s AND status='dialing'",
        (attempt_id, camp_id, lead_id),
    )


async def schedule_retry(conn: Any, camp: Campaign, lead_id: uuid.UUID, reason: str,
                         not_before: datetime | None = None, from_status: tuple[str, ...] = ("dialing", "in_call")) -> str:
    cur = await conn.execute("SELECT attempts FROM campaign_lead WHERE campaign_id=%s AND lead_id=%s",
                             (camp.id, lead_id))
    row = await cur.fetchone()
    attempts = row["attempts"] if row else 0
    if attempts >= camp.max_attempts:
        await conn.execute(
            "UPDATE campaign_lead SET status='exhausted', last_reason=%s, updated_at=now()"
            " WHERE campaign_id=%s AND lead_id=%s AND status = ANY(%s)",
            (reason, camp.id, lead_id, list(from_status)),
        )
        return "exhausted"
    delay = camp.retry_seconds[min(max(attempts - 1, 0), len(camp.retry_seconds) - 1)]
    at = datetime.now(timezone.utc) + timedelta(seconds=delay * random.uniform(0.85, 1.15))
    if not_before and not_before > at:
        at = not_before
    await conn.execute(
        "UPDATE campaign_lead SET status='retry_wait', next_attempt_at=%s, last_reason=%s, updated_at=now()"
        " WHERE campaign_id=%s AND lead_id=%s AND status = ANY(%s)",
        (at, reason, camp.id, lead_id, list(from_status)),
    )
    return "retry"


async def finish(conn: Any, camp_id: uuid.UUID, lead_id: uuid.UUID, status: str, reason: str,
                 *, outcome: str | None = None, plan: dict[str, Any] | None = None,
                 plan_source: str | None = None, priority: int | None = None,
                 next_attempt_at: datetime | None = None, from_status: tuple[str, ...] = ("dialing", "in_call")) -> bool:
    cur = await conn.execute(
        "UPDATE campaign_lead SET status=%s, last_reason=%s, last_outcome=COALESCE(%s, last_outcome),"
        " plan=COALESCE(%s, plan), plan_source=COALESCE(%s, plan_source), priority=COALESCE(%s, priority),"
        " next_attempt_at=COALESCE(%s, next_attempt_at), updated_at=now()"
        " WHERE campaign_id=%s AND lead_id=%s AND status = ANY(%s) RETURNING lead_id",
        (status, reason, outcome, Jsonb(plan) if plan else None, plan_source, priority, next_attempt_at,
         camp_id, lead_id, list(from_status)),
    )
    return await cur.fetchone() is not None


async def ended_attempts(c: Container, limit: int = 200) -> list[dict[str, Any]]:
    async with c.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT a.id AS attempt_id, cl.campaign_id, cl.lead_id
               FROM campaign_lead cl JOIN call_attempt a ON a.id = cl.last_attempt_id
               JOIN campaign cp ON cp.id = cl.campaign_id
               WHERE cl.status='in_call' AND cp.status IN ('running','paused','stopped')
                 AND a.status IN ('completed','failed','cancelled')
               LIMIT %s""",
            (limit,),
        )
        return list(await cur.fetchall())


async def reap_stuck(c: Container) -> None:
    """A worker crash between claim and dial leaves leads in 'dialing'; release them."""
    async with c.pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE campaign_lead SET status='retry_wait', next_attempt_at=now(), last_reason='RECLAIMED'"
            " WHERE status='dialing' AND updated_at < now() - interval '2 minutes'"
        )


async def complete_finished(c: Container) -> None:
    async with c.pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """UPDATE campaign cp SET status='completed', ended_at=now()
               WHERE status='running' AND NOT EXISTS (
                 SELECT 1 FROM campaign_lead cl WHERE cl.campaign_id=cp.id AND cl.status = ANY(%s))
               RETURNING id""",
            (list(OPEN),),
        )
        for r in await cur.fetchall():
            await audit(conn, "campaign_completed", detail={"campaign_id": str(r["id"])})


async def summary(c: Container, campaign_id: uuid.UUID) -> dict[str, Any]:
    async with c.pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM campaign WHERE id=%s", (campaign_id,))
        camp = await cur.fetchone()
        if not camp:
            return {}
        cur = await conn.execute(
            "SELECT status, count(*) AS n FROM campaign_lead WHERE campaign_id=%s GROUP BY status", (campaign_id,))
        funnel = {r["status"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute(
            """SELECT count(*) FILTER (WHERE a.status IN ('completed','failed','cancelled')) AS calls_done,
                      count(*) FILTER (WHERE a.outcome='conversation') AS conversations,
                      count(*) FILTER (WHERE a.outcome IN ('no_answer','voicemail')) AS no_contact
               FROM call_attempt a WHERE a.campaign_id=%s""", (campaign_id,))
        calls = await cur.fetchone()
        cur = await conn.execute(
            """SELECT count(*) FILTER (WHERE status IN ('ok','guardrail_fallback','error')) AS model_calls,
                      count(*) FILTER (WHERE status='cache_hit') AS cache_hits,
                      count(*) FILTER (WHERE status='budget_skip') AS budget_skips,
                      count(*) FILTER (WHERE status='guardrail_fallback') AS guardrail_fallbacks,
                      count(*) FILTER (WHERE status='error') AS errors,
                      COALESCE(sum(input_tokens),0) AS input_tokens, COALESCE(sum(output_tokens),0) AS output_tokens,
                      COALESCE(sum(cached_tokens),0) AS cached_tokens, COALESCE(sum(cost_usd),0) AS cost_usd,
                      COALESCE(avg(latency_ms) FILTER (WHERE status='ok'),0) AS avg_latency_ms
               FROM llm_usage WHERE campaign_id=%s""", (campaign_id,))
        llm = await cur.fetchone()
        cur = await conn.execute(
            "SELECT stage, rule, count(*) AS n FROM guardrail_event WHERE campaign_id=%s GROUP BY stage, rule"
            " ORDER BY n DESC", (campaign_id,))
        guards = list(await cur.fetchall())
        cur = await conn.execute(
            """SELECT cl.lead_id, cl.priority, cl.plan, cl.plan_source, cl.last_reason, cl.updated_at,
                      l.company, sr.score, sr.band, sr.valuation->>'contract_value' AS contract_value
               FROM campaign_lead cl JOIN lead l ON l.id=cl.lead_id
               LEFT JOIN score_result sr ON sr.attempt_id=cl.last_attempt_id
               WHERE cl.campaign_id=%s AND cl.status='escalated'
               ORDER BY cl.updated_at DESC LIMIT 8""", (campaign_id,))
        escalations = list(await cur.fetchall())
        cur = await conn.execute(
            "SELECT COALESCE(sum((sr.valuation->>'contract_value')::numeric),0) AS pipeline"
            " FROM campaign_lead cl JOIN score_result sr ON sr.attempt_id=cl.last_attempt_id"
            " WHERE cl.campaign_id=%s AND cl.status='escalated'", (campaign_id,))
        pipeline = (await cur.fetchone())["pipeline"]
    esc_n = funnel.get("escalated", 0)
    return {
        "campaign": camp,
        "funnel": funnel,
        "in_flight": sum(funnel.get(s, 0) for s in IN_FLIGHT),
        "calls": calls,
        "llm": {**llm, "cost_per_escalation": (float(llm["cost_usd"]) / esc_n) if esc_n else None},
        "guardrails": guards,
        "escalations": escalations,
        "pipeline_usd": pipeline,
    }
