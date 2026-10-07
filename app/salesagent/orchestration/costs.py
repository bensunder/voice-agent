"""Token cost management: pricing, budget reservation, and the usage ledger.

Budgets are enforced *before* a model call by reserving the worst-case cost
(estimated prompt tokens + the output cap) in the ledger under an advisory lock,
so concurrent workflows can never overspend. After the call the reservation is
settled to the actual token usage. Two budgets apply: the campaign budget and a
global daily budget. When either would be exceeded the model is skipped and the
deterministic planner is used, so a campaign never stalls on cost.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .. import telemetry
from ..config import Settings

GLOBAL_LOCK = 4_211_771


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


def estimate_tokens(text: str) -> int:
    """Conservative estimate (~3.5 chars/token for English + JSON)."""
    return max(1, int(len(text) / 3.5) + 8)


def price(s: Settings, u: Usage) -> float:
    uncached = max(0, u.input_tokens - u.cached_tokens)
    return (
        uncached * s.llm_price_input_per_1m
        + u.cached_tokens * s.llm_price_cached_input_per_1m
        + u.output_tokens * s.llm_price_output_per_1m
    ) / 1_000_000


def _money(v: float) -> Decimal:
    return Decimal(str(round(v, 6)))


@dataclass(frozen=True)
class Reservation:
    allowed: bool
    reason: str
    estimated_usd: float
    ledger_id: int | None = None


async def reserve(
    pool: Any, s: Settings, *, campaign_id: uuid.UUID | None, attempt_id: uuid.UUID | None,
    agent: str, model: str, prompt: str,
) -> Reservation:
    est = price(s, Usage(input_tokens=estimate_tokens(prompt), output_tokens=s.escalation_max_output_tokens))
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (GLOBAL_LOCK,))
        cur = await conn.execute(
            "SELECT COALESCE(sum(cost_usd),0) AS day FROM llm_usage WHERE created_at >= date_trunc('day', now())"
        )
        day = float((await cur.fetchone())["day"])
        reason = "OK"
        if day + est > s.llm_daily_budget_usd:
            reason = "DAILY_BUDGET"
        elif campaign_id:
            cur = await conn.execute(
                "SELECT c.llm_budget_usd AS budget, COALESCE((SELECT sum(cost_usd) FROM llm_usage u"
                " WHERE u.campaign_id=c.id),0) AS spent FROM campaign c WHERE c.id=%s",
                (campaign_id,),
            )
            row = await cur.fetchone()
            if row and float(row["spent"]) + est > float(row["budget"]):
                reason = "CAMPAIGN_BUDGET"
        status = "reserved" if reason == "OK" else "budget_skip"
        cur = await conn.execute(
            "INSERT INTO llm_usage (campaign_id, attempt_id, agent, model, cost_usd, status)"
            " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (campaign_id, attempt_id, agent, model, _money(est if status == "reserved" else 0), status),
        )
        ledger_id = (await cur.fetchone())["id"]
    if reason != "OK":
        telemetry.llm_calls.add(1, {"agent": agent, "model": model, "status": "budget_skip"})
    return Reservation(reason == "OK", reason, est, ledger_id)


async def settle(
    pool: Any, s: Settings, ledger_id: int, *, agent: str, model: str, usage: Usage, latency_ms: int, status: str,
) -> float:
    """Replace the reservation with the actual cost of the call."""
    cost = price(s, usage)
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE llm_usage SET input_tokens=%s, output_tokens=%s, cached_tokens=%s, cost_usd=%s,"
            " latency_ms=%s, status=%s WHERE id=%s",
            (usage.input_tokens, usage.output_tokens, usage.cached_tokens, _money(cost), latency_ms, status, ledger_id),
        )
    attrs = {"agent": agent, "model": model, "status": status}
    telemetry.llm_calls.add(1, attrs)
    if usage.input_tokens:
        telemetry.llm_tokens.add(usage.input_tokens, {**attrs, "direction": "input"})
    if usage.output_tokens:
        telemetry.llm_tokens.add(usage.output_tokens, {**attrs, "direction": "output"})
    if cost:
        telemetry.llm_cost.add(cost, attrs)
    return cost


async def record_cache_hit(pool: Any, *, campaign_id: uuid.UUID | None, attempt_id: uuid.UUID | None,
                           agent: str, model: str) -> None:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO llm_usage (campaign_id, attempt_id, agent, model, status) VALUES (%s,%s,%s,%s,'cache_hit')",
            (campaign_id, attempt_id, agent, model),
        )
    telemetry.llm_calls.add(1, {"agent": agent, "model": model, "status": "cache_hit"})
