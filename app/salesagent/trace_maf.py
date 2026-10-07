"""Show the Microsoft Agent Framework workflows running, step by step.

Prints both MAF workflow graphs (built by WorkflowBuilder in orchestration/workflows.py),
then runs ONE real orchestration pass on a small simulated campaign with MAF event
streaming on, so every executor hand-off is visible: which executor ran, which typed
message it received, and where the lead ended up.

Stop the background worker first so this pass runs in isolation, then start it again:

    docker compose stop worker
    docker compose run --rm worker python -m salesagent.trace_maf
    docker compose start worker

The campaign is left running ("MAF trace"), so once the worker is back the retries
continue in the cockpit's Campaign orchestrator tab. "Reset demo" removes it.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from typing import Any

from agent_framework import Workflow, WorkflowViz

from .config import get_settings
from .container import Container
from .orchestration import runner, simulation, store
from .orchestration.workflows import CallEnded, CampaignTick, build_dispatch, build_outcome

LEADS = 12


def banner(t: str) -> None:
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


async def stream(wf: Workflow, message: Any) -> tuple[list[tuple[str, str]], list[Any]]:
    """Run a workflow with MAF event streaming; return (executor, message type) hops and outputs."""
    hops: list[tuple[str, str]] = []
    outputs: list[Any] = []
    async for ev in wf.run(message, stream=True):
        if ev.type == "executor_invoked":
            hops.append((ev.executor_id or "?", type(ev.data).__name__ if ev.data is not None else "-"))
        elif ev.type == "output":
            outputs.append(ev.data)
        elif ev.type in ("executor_failed", "failed", "error"):
            hops.append((ev.executor_id or "workflow", f"FAILED {ev.details or ev.data}"))
    return hops, outputs


async def main() -> int:
    s = get_settings()
    c = await Container.create(s, pool_size=6)
    try:
        planner = runner.make_planner(c)

        banner("1. THE TWO MAF WORKFLOWS (agent_framework.WorkflowBuilder, orchestration/workflows.py)")
        for wf in (build_dispatch(c), build_outcome(c, planner)):
            print(f"\n{wf.name}  ({type(wf).__module__}.{type(wf).__name__})")
            print(f"  start executor : {wf.get_start_executor().id}")
            print(f"  executors      : {', '.join(e.id for e in wf.get_executors_list())}")
            print("  graph (Mermaid):")
            for line in WorkflowViz(wf).to_mermaid().splitlines():
                print(f"    {line}")

        banner(f"2. CREATE A SIMULATED CAMPAIGN ({LEADS} synthetic leads, no real calls)")
        made = await store.create(c, store.CampaignIn(name="MAF trace", mode="simulation", lead_count=LEADS,
                                                      max_concurrent=LEADS, max_attempts=3, llm_budget_usd=1.0))
        cid = made["id"]
        await store.set_status(c, cid, "running")  # type: ignore[arg-type]
        print(f"campaign {cid}: {made['leads']} leads, status=running, escalation planner model={planner.model}")

        banner("3. MAF WORKFLOW #1  campaign_dispatch  (one pass, event stream)")
        hops, outputs = await stream(build_dispatch(c), CampaignTick(cid))  # type: ignore[arg-type]
        seen: Counter[str] = Counter(h for h, _ in hops)
        for executor, msg in dict.fromkeys(hops):
            print(f"  executor_invoked  {executor:<18} received {msg:<12} x{seen[executor]}")
        print(f"  -> {sum(1 for o in outputs if o.route == 'dialed')} leads dialed by the Dialer "
              f"(simulation; live mode calls leads.start_teams_call -> Foundry voice agent)")

        banner("4. CALLS FINISH  (simulated outcomes written via the same persist_answers() + score())")
        async with c.pool.connection() as conn:
            await conn.execute("UPDATE call_attempt SET sim_complete_at=now() WHERE channel='simulation'"
                               " AND status='in_progress' AND campaign_id=%s", (cid,))
            await conn.commit()
        print(f"  {await simulation.complete_due(c)} calls completed")

        banner("5. MAF WORKFLOW #2  campaign_outcome  (one run per finished call)")
        ended = [r for r in await store.ended_attempts(c) if str(r["campaign_id"]) == cid]
        routes: Counter[str] = Counter()
        for i, row in enumerate(ended, 1):
            hops, outputs = await stream(build_outcome(c, planner),
                                         CallEnded(row["campaign_id"], row["lead_id"], row["attempt_id"]))
            chain = " -> ".join(f"{h}({m})" for h, m in hops)
            out = outputs[-1] if outputs else None
            result = f"{out.route} [{out.reason}]" if out else "-"
            routes[out.route if out else "none"] += 1
            print(f"  lead {i:>2}: {chain}\n           => {result}")

        banner("6. RESULT")
        for route, n in routes.most_common():
            print(f"  {route:<14} {n}")
        print("\nEvery hop above is a MAF Executor @handler receiving a typed message from the previous one.")
        print("Start the worker again (docker compose start worker) and watch 'MAF trace' continue in the cockpit.")
        return 0
    finally:
        await c.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
