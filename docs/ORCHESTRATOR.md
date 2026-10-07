# Campaign orchestrator (Microsoft Agent Framework)

Runs calling campaigns across thousands of leads. The orchestration is two
**Microsoft Agent Framework (MAF) workflows**; the **Foundry voice agent on Teams Phone
is the Dialer step**; a **MAF `Agent` on a Foundry model** writes each rep hand-off.

```
Dispatch (every worker tick, per running campaign)
  Pacer ─fan-out─> ComplianceGate ─switch─┬ allow ─> Dialer (Teams Phone call job | simulation) ─fail─> RetryScheduler
                                          ├ defer ─> RetryScheduler
                                          └ deny  ─> Suppressor
Outcome (per finished call)
  OutcomeClassifier ─switch─┬ escalate ─> EscalationAgent (MAF Agent) ─> EscalationDispatcher
                            ├ retry ─> RetryScheduler   ├ nurture ─> Nurturer
                            ├ suppress ─> Suppressor    └ close ─> Closer
```

Code: `app/salesagent/orchestration/` (`workflows.py`, `escalation_agent.py`, `guardrails.py`,
`costs.py`, `store.py`, `simulation.py`, `runner.py`).

## Pacing and retries
- **Concurrency cap** per campaign (`max_concurrent`); the Pacer only claims as many due
  leads as there are free slots (`FOR UPDATE SKIP LOCKED`, safe with several workers).
- **Rep capacity**: dialing pauses for the day once `escalation_daily_cap` is reached, so
  the AI never creates more hand-offs than reps can take.
- **Retries** with jittered backoff (`retry_seconds`), capped by `max_attempts`; failed dials
  count as attempts so a misconfigured channel exhausts instead of looping.
- **Compliance gate** before every dial (consent, opt-out list, attempts per day, calling
  window in the lead's time zone, demo allowlist for live calls).
- **Crash-safe**: every transition is a status-guarded update; leads stuck in `dialing` are
  reclaimed after 2 minutes.

## Grounding
The escalation agent sees only a minimised **fact packet**: captured answers with
sanitised evidence, the deterministic score and valuation, the policy-allowed channels and
the vetted notes in `orchestration/knowledge.md`. No names, phone numbers or emails.

## Guardrails
| Stage | Rule |
|---|---|
| Input | Prompt-injection phrases in caller words are removed; emails, phone numbers and ID numbers are redacted; text is length-capped |
| Output | Must parse into `EscalationPlan`; every number must exist in the facts (rounded money allowed); carrier names must match the facts; no pricing/discount/guarantee language; no personal data |
| Action | The *code* decides whether a lead is escalated; the model only picks a channel from the allowed set and writes content |
| Fallback | Any violation, model error, timeout or budget stop uses the deterministic plan. Every intervention is stored in `guardrail_event` and counted in telemetry |

## Token cost management
- Model is called **only for escalated leads** (hot/qualified), never per call.
- **Plan cache**: identical fact packets reuse the validated plan (no tokens).
- **Budget reservation**: before each call the worst-case cost (prompt estimate + output cap)
  is reserved under a lock against both the campaign budget and `LLM_DAILY_BUDGET_USD`;
  concurrent workflows cannot overspend. The reservation is settled to actual usage.
- **Output cap** (`ESCALATION_MAX_OUTPUT_TOKENS`), low temperature, static system prompt
  (provider prompt-cache friendly); cached input tokens are priced separately.
- **Ledger**: `llm_usage` rows per call (tokens, cost, latency, status); cockpit shows spend vs
  budget, cost per escalation, cache hits, budget skips and fallbacks.

## Telemetry
OpenTelemetry traces and metrics. MAF workflow/executor spans and GenAI token metrics are
enabled (prompt/response content is **not** captured). Our metrics:
`salesagent.llm.tokens`, `salesagent.llm.cost`, `salesagent.llm.calls`,
`salesagent.guardrail.events`, `salesagent.campaign.dispatch`, `salesagent.campaign.outcomes`,
`salesagent.workflow.duration`. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to an OpenTelemetry
Collector (which can forward to Application Insights / Azure Monitor, Grafana, Jaeger).

## Enable the escalation agent
1. Deploy a model in the Foundry project (e.g. `gpt-4.1-mini`).
2. The Entra app (setup step C) needs the **Foundry User** role on the project.
3. `.env`: `FOUNDRY_PROJECT_ENDPOINT`, `AZURE_*`, `ESCALATION_MODEL=<deployment>`, and the
   model's token prices; `./deploy.sh`. The cockpit's **MAF agent** pill turns green.

Without a model, everything runs with deterministic plans (labelled `rules`).

## Simulation mode
Creates synthetic leads with fictional numbers (`NXX-555-01XX`) and **never dials**. Calls
resolve in seconds to seeded outcomes; connected calls go through the same scoring, gate,
workflows, guardrails and budget as live. About 6% of simulated callers try a prompt
injection or volunteer personal data, so the guardrails visibly fire. Simulated campaigns
never notify reps or write to Dataverse.
