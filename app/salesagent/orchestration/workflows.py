"""Microsoft Agent Framework workflows that run a calling campaign.

Dispatch workflow (one run per campaign tick):

    Pacer ──fan-out──> ComplianceGate ──switch──┬─ allow ──> Dialer ──(fail)──> RetryScheduler
                                                 ├─ defer ──> RetryScheduler
                                                 └─ deny  ──> Suppressor

Outcome workflow (one run per ended call):

    OutcomeClassifier ──switch──┬─ escalate ──> EscalationAgent ──> EscalationDispatcher
                                ├─ retry    ──> RetryScheduler
                                ├─ nurture  ──> Nurturer
                                ├─ suppress ──> Suppressor
                                └─ close    ──> Closer

The Foundry voice agent is the Dialer step in live mode (a Teams Phone call job);
in simulation mode the Dialer uses the simulated channel. Executors hold no state;
every transition is a guarded, idempotent database update, so a run can be retried.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from agent_framework import Case, Default, Executor, WorkflowBuilder, WorkflowContext, handler

from .. import telemetry
from ..container import Container
from ..db import audit, enqueue
from ..domain import compliance
from ..services import leads as lead_service
from ..services.errors import ServiceError
from . import simulation, store
from .escalation_agent import EscalationPlanner

log = logging.getLogger(__name__)
PRIORITY = {"P1": 3, "P2": 2, "P3": 1}


# ---------------------------------------------------------------- messages
@dataclass
class CampaignTick:
    campaign_id: uuid.UUID


@dataclass
class LeadWork:
    campaign: store.Campaign
    lead_id: uuid.UUID
    attempts: int


@dataclass
class GateResult:
    work: LeadWork
    verdict: str
    reason: str
    retry_at: datetime | None = None


@dataclass
class RetryRequest:
    campaign: store.Campaign
    lead_id: uuid.UUID
    reason: str
    not_before: datetime | None = None


@dataclass
class CallEnded:
    campaign_id: uuid.UUID
    lead_id: uuid.UUID
    attempt_id: uuid.UUID


@dataclass
class Classified:
    campaign: store.Campaign
    lead_id: uuid.UUID
    attempt_id: uuid.UUID
    route: str
    reason: str
    band: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    score: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    renewal_months: int | None = None


@dataclass
class PlanReady:
    item: Classified
    plan: dict[str, Any]
    source: str
    reason: str
    cost_usd: float


@dataclass
class StepResult:
    """Terminal output of either workflow (one per lead touched)."""

    lead_id: uuid.UUID | None
    route: str
    reason: str


# ---------------------------------------------------------------- dispatch executors
class Pacer(Executor):
    def __init__(self, c: Container):
        super().__init__("pacer")
        self.c = c

    @handler
    async def tick(self, msg: CampaignTick, ctx: WorkflowContext[LeadWork, StepResult]) -> None:
        async with self.c.pool.connection() as conn, conn.transaction():
            camp = await store.load(conn, msg.campaign_id)
            if not camp or camp.status != "running":
                return
            cap, why = await store.capacity(conn, camp)
            claimed = await store.claim_due(conn, camp, cap)
        if why != "OK":
            telemetry.dispatch_routes.add(1, {"route": "paused", "reason": why})
            await ctx.yield_output(StepResult(None, "paced", why))
        for row in claimed:
            await ctx.send_message(LeadWork(camp, row["lead_id"], row["attempts"]))


class ComplianceGate(Executor):
    def __init__(self, c: Container):
        super().__init__("compliance_gate")
        self.c = c

    @handler
    async def check(self, work: LeadWork, ctx: WorkflowContext[GateResult]) -> None:
        s = self.c.settings
        async with self.c.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT l.phone_e164, l.timezone, l.consent_at,
                          EXISTS (SELECT 1 FROM opt_out o WHERE o.tenant_key=l.tenant_key
                                  AND o.phone_e164=l.phone_e164) AS opted_out,
                          (SELECT count(*) FROM call_attempt a WHERE a.lead_id=l.id
                             AND a.created_at > now() - interval '24 hours'
                             AND a.channel <> 'simulation' AND a.status <> 'blocked') AS attempts_today
                   FROM lead l WHERE l.id=%s""",
                (work.lead_id,),
            )
            lead = await cur.fetchone()
        if not lead:
            await ctx.send_message(GateResult(work, "deny", "LEAD_NOT_FOUND"))
            return
        live = work.campaign.mode == "live"
        windowed = work.campaign.respect_calling_window
        d = compliance.evaluate(compliance.GateInput(
            phone_e164=lead["phone_e164"], has_consent=bool(lead["consent_at"]), opted_out=lead["opted_out"],
            attempts_today=lead["attempts_today"], lead_timezone=ZoneInfo(lead["timezone"]),
            now_utc=datetime.now(timezone.utc),
            demo_mode=s.demo_mode and live,  # the allowlist protects real phones only
            allowlist=s.allowlist,
            window_start_hour=s.calling_window_start_hour if windowed else 0,
            window_end_hour=s.calling_window_end_hour if windowed else 24,
        ))
        telemetry.dispatch_routes.add(1, {"route": d.verdict.value, "reason": d.reason, "mode": work.campaign.mode})
        await ctx.send_message(GateResult(work, d.verdict.value, d.reason, d.retry_at_utc))


class Dialer(Executor):
    """The Foundry voice agent step: a Teams Phone call job in live mode."""

    def __init__(self, c: Container):
        super().__init__("dialer")
        self.c = c

    @handler
    async def dial(self, g: GateResult, ctx: WorkflowContext[RetryRequest, StepResult]) -> None:
        w = g.work
        camp = w.campaign
        if camp.mode == "simulation":
            async with self.c.pool.connection() as conn, conn.transaction():
                attempt_id = await simulation.start_call(conn, camp.id, w.lead_id)
                await store.mark_in_call(conn, camp.id, w.lead_id, attempt_id)
            await ctx.yield_output(StepResult(w.lead_id, "dialed", "simulation"))
            return
        try:
            r = await lead_service.start_teams_call(self.c, w.lead_id, requested_by=f"campaign:{camp.id}",
                                                    campaign_id=camp.id)
        except ServiceError as exc:
            await ctx.send_message(RetryRequest(camp, w.lead_id, exc.code))
            return
        if r.get("status") != "dialing":
            await ctx.send_message(RetryRequest(camp, w.lead_id, r.get("reason") or "NOT_DIALED"))
            return
        async with self.c.pool.connection() as conn, conn.transaction():
            await store.mark_in_call(conn, camp.id, w.lead_id, uuid.UUID(r["attempt_id"]))
        await ctx.yield_output(StepResult(w.lead_id, "dialed", "teams_phone"))


class RetryScheduler(Executor):
    def __init__(self, c: Container):
        super().__init__("retry_scheduler")
        self.c = c

    async def _retry(self, camp: store.Campaign, lead_id: uuid.UUID, reason: str, not_before: datetime | None,
                     ctx: WorkflowContext[None, StepResult]) -> None:
        async with self.c.pool.connection() as conn, conn.transaction():
            route = await store.schedule_retry(conn, camp, lead_id, reason, not_before)
        telemetry.outcome_routes.add(1, {"route": route, "reason": reason})
        await ctx.yield_output(StepResult(lead_id, route, reason))

    @handler
    async def deferred(self, g: GateResult, ctx: WorkflowContext[None, StepResult]) -> None:
        await self._retry(g.work.campaign, g.work.lead_id, g.reason, g.retry_at, ctx)

    @handler
    async def failed(self, r: RetryRequest, ctx: WorkflowContext[None, StepResult]) -> None:
        # A failed dial counts as an attempt, so a misconfigured channel exhausts instead of looping.
        async with self.c.pool.connection() as conn, conn.transaction():
            await conn.execute("UPDATE campaign_lead SET attempts=attempts+1 WHERE campaign_id=%s AND lead_id=%s"
                               " AND status='dialing'", (r.campaign.id, r.lead_id))
        await self._retry(r.campaign, r.lead_id, r.reason, r.not_before, ctx)

    @handler
    async def no_contact(self, k: Classified, ctx: WorkflowContext[None, StepResult]) -> None:
        await self._retry(k.campaign, k.lead_id, k.reason, None, ctx)


class Suppressor(Executor):
    def __init__(self, c: Container):
        super().__init__("suppressor")
        self.c = c

    async def _suppress(self, camp_id: uuid.UUID, lead_id: uuid.UUID, reason: str, from_status: tuple[str, ...],
                        ctx: WorkflowContext[None, StepResult]) -> None:
        async with self.c.pool.connection() as conn, conn.transaction():
            await store.finish(conn, camp_id, lead_id, "suppressed", reason, from_status=from_status)
        await ctx.yield_output(StepResult(lead_id, "suppressed", reason))

    @handler
    async def denied(self, g: GateResult, ctx: WorkflowContext[None, StepResult]) -> None:
        await self._suppress(g.work.campaign.id, g.work.lead_id, g.reason, ("dialing",), ctx)

    @handler
    async def opted_out(self, k: Classified, ctx: WorkflowContext[None, StepResult]) -> None:
        await self._suppress(k.campaign.id, k.lead_id, k.reason, ("in_call",), ctx)


# ---------------------------------------------------------------- outcome executors
class OutcomeClassifier(Executor):
    def __init__(self, c: Container):
        super().__init__("outcome_classifier")
        self.c = c

    @handler
    async def classify(self, e: CallEnded, ctx: WorkflowContext[Classified]) -> None:
        async with self.c.pool.connection() as conn:
            camp = await store.load(conn, e.campaign_id)
            cur = await conn.execute("SELECT status, outcome, summary, terminal_reason FROM call_attempt WHERE id=%s",
                                     (e.attempt_id,))
            a = await cur.fetchone()
            cur = await conn.execute("SELECT * FROM score_result WHERE attempt_id=%s", (e.attempt_id,))
            sc = await cur.fetchone() or {}
            cur = await conn.execute("SELECT name, value, confidence, evidence FROM qualification_slot"
                                     " WHERE attempt_id=%s", (e.attempt_id,))
            slots = {r["name"]: {"value": r["value"], "confidence": r["confidence"], "evidence": r["evidence"]}
                     for r in await cur.fetchall()}
        if not camp or not a:
            return
        band = sc.get("band")
        outcome = a["outcome"]
        if outcome == "opted_out":
            route, reason = "suppress", "OPTED_OUT"
        elif band in ("hot", "qualified"):
            route, reason = "escalate", band.upper()
        elif band == "nurture":
            route, reason = "nurture", "NURTURE"
        elif band == "disqualified":
            route, reason = "close", "DISQUALIFIED"
        elif band == "incomplete":
            route, reason = "retry", "INCOMPLETE_QUALIFICATION"
        else:
            route, reason = "retry", (outcome or a["terminal_reason"] or a["status"] or "NO_CONTACT").upper()
        telemetry.outcome_routes.add(1, {"route": route, "reason": reason, "mode": camp.mode})
        months = (slots.get("contract_months_remaining") or {}).get("value")
        await ctx.send_message(Classified(camp, e.lead_id, e.attempt_id, route, reason, band, slots, dict(sc),
                                          a["summary"], months if isinstance(months, int) else None))


class EscalationAgentExecutor(Executor):
    """Wraps the MAF escalation agent (grounded, guarded, budgeted)."""

    def __init__(self, planner: EscalationPlanner):
        super().__init__("escalation_agent")
        self.planner = planner

    @handler
    async def plan(self, k: Classified, ctx: WorkflowContext[PlanReady]) -> None:
        r = await self.planner.plan(campaign_id=k.campaign.id, attempt_id=k.attempt_id, slots=k.slots,
                                    score=k.score, summary=k.summary)
        await ctx.send_message(PlanReady(k, r.plan.model_dump(), r.source, r.reason, r.cost_usd))


class EscalationDispatcher(Executor):
    def __init__(self, c: Container):
        super().__init__("escalation_dispatcher")
        self.c = c

    @handler
    async def dispatch(self, p: PlanReady, ctx: WorkflowContext[None, StepResult]) -> None:
        k = p.item
        async with self.c.pool.connection() as conn, conn.transaction():
            ok = await store.finish(conn, k.campaign.id, k.lead_id, "escalated", k.reason, outcome="escalated",
                                    plan=p.plan, plan_source=p.source, priority=PRIORITY.get(p.plan["priority"], 1),
                                    from_status=("in_call",))
            if ok and k.campaign.mode == "live":
                await audit(conn, "escalated", lead_id=k.lead_id, attempt_id=k.attempt_id,
                            detail={"priority": p.plan["priority"], "channel": p.plan["recommended_channel"],
                                    "plan_source": p.source, "campaign_id": str(k.campaign.id)})
                await enqueue(conn, "rep.notify", f"rep.notify:{k.attempt_id}:escalation",
                              {"attempt_id": str(k.attempt_id), "kind": "escalation"})
        await ctx.yield_output(StepResult(k.lead_id, "escalated", f"{p.plan['priority']}/{p.source}"))


class Nurturer(Executor):
    def __init__(self, c: Container):
        super().__init__("nurturer")
        self.c = c

    @handler
    async def nurture(self, k: Classified, ctx: WorkflowContext[None, StepResult]) -> None:
        months = k.renewal_months if k.renewal_months is not None else 6
        touch = datetime.now(timezone.utc) + timedelta(days=max(14, (months - 2) * 30))
        async with self.c.pool.connection() as conn, conn.transaction():
            await store.finish(conn, k.campaign.id, k.lead_id, "nurture", k.reason, outcome="nurture",
                               next_attempt_at=touch, from_status=("in_call",))
        await ctx.yield_output(StepResult(k.lead_id, "nurture", touch.date().isoformat()))


class Closer(Executor):
    def __init__(self, c: Container):
        super().__init__("closer")
        self.c = c

    @handler
    async def close(self, k: Classified, ctx: WorkflowContext[None, StepResult]) -> None:
        async with self.c.pool.connection() as conn, conn.transaction():
            await store.finish(conn, k.campaign.id, k.lead_id, "disqualified", k.reason, outcome="disqualified",
                               from_status=("in_call",))
        await ctx.yield_output(StepResult(k.lead_id, "disqualified", k.reason))


# ---------------------------------------------------------------- builders
def build_dispatch(c: Container) -> Any:
    pacer, gate, dialer = Pacer(c), ComplianceGate(c), Dialer(c)
    retry, suppress = RetryScheduler(c), Suppressor(c)
    return (
        WorkflowBuilder(name="campaign_dispatch", description="Pace, gate and dial due leads",
                        start_executor=pacer, output_from=[pacer, dialer, retry, suppress])
        .add_edge(pacer, gate)
        .add_switch_case_edge_group(gate, [
            Case(condition=lambda g: g.verdict == "allow", target=dialer),
            Case(condition=lambda g: g.verdict == "defer", target=retry),
            Default(target=suppress),
        ])
        .add_edge(dialer, retry)
        .build()
    )


def build_outcome(c: Container, planner: EscalationPlanner) -> Any:
    classifier, agent, dispatcher = OutcomeClassifier(c), EscalationAgentExecutor(planner), EscalationDispatcher(c)
    retry, suppress, nurture, close = RetryScheduler(c), Suppressor(c), Nurturer(c), Closer(c)
    return (
        WorkflowBuilder(name="campaign_outcome", description="Route a finished call",
                        start_executor=classifier, output_from=[dispatcher, retry, suppress, nurture, close])
        .add_switch_case_edge_group(classifier, [
            Case(condition=lambda k: k.route == "escalate", target=agent),
            Case(condition=lambda k: k.route == "retry", target=retry),
            Case(condition=lambda k: k.route == "nurture", target=nurture),
            Case(condition=lambda k: k.route == "suppress", target=suppress),
            Default(target=close),
        ])
        .add_edge(agent, dispatcher)
        .build()
    )


async def run_dispatch(c: Container, campaign_id: uuid.UUID) -> list[StepResult]:
    started = time.perf_counter()
    with telemetry.span("campaign.dispatch", **{"campaign.id": str(campaign_id)}):
        result = await build_dispatch(c).run(CampaignTick(campaign_id))
    telemetry.workflow_duration.record((time.perf_counter() - started) * 1000, {"workflow": "dispatch"})
    return list(result.get_outputs())


async def run_outcome(c: Container, planner: EscalationPlanner, ended: CallEnded) -> list[StepResult]:
    started = time.perf_counter()
    with telemetry.span("campaign.outcome", **{"campaign.id": str(ended.campaign_id),
                                                "attempt.id": str(ended.attempt_id)}):
        result = await build_outcome(c, planner).run(ended)
    telemetry.workflow_duration.record((time.perf_counter() - started) * 1000, {"workflow": "outcome"})
    return list(result.get_outputs())
