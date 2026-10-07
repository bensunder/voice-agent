"""Drives the campaign workflows from the worker loop."""

import asyncio
import logging

from ..container import Container
from . import simulation, store
from .escalation_agent import EscalationPlanner, FoundryBackend
from .workflows import CallEnded, run_dispatch, run_outcome

log = logging.getLogger(__name__)


def make_planner(c: Container) -> EscalationPlanner:
    backend = None
    if c.settings.escalation_agent_configured and c.tokens:
        try:
            backend = FoundryBackend(c.settings, c.tokens.credential)
        except Exception as exc:  # noqa: BLE001 - fall back to rules, keep campaigns running
            log.error("escalation agent unavailable, using rules: %s", exc)
    return EscalationPlanner(c.settings, c.pool, backend)


async def tick(c: Container, planner: EscalationPlanner) -> int:
    """One orchestration pass. Returns the amount of work done (0 = idle)."""
    work = await simulation.complete_due(c)

    ended = await store.ended_attempts(c)
    if ended:
        sem = asyncio.Semaphore(c.settings.outcome_concurrency)

        async def one(row: dict) -> None:
            async with sem:
                try:
                    await run_outcome(c, planner, CallEnded(row["campaign_id"], row["lead_id"], row["attempt_id"]))
                except Exception:  # noqa: BLE001 - one bad lead must not stop the campaign
                    log.exception("outcome workflow failed for attempt %s", row["attempt_id"])

        await asyncio.gather(*(one(r) for r in ended))
        work += len(ended)

    for cid in await store.running_campaigns(c):
        try:
            work += len(await run_dispatch(c, cid))
        except Exception:  # noqa: BLE001
            log.exception("dispatch workflow failed for campaign %s", cid)

    await store.reap_stuck(c)
    await store.complete_finished(c)
    return work
