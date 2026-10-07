# AI Sales Agent

An AI employee that calls leads from the company's **Teams Phone** number, qualifies them in a
natural **voice** conversation run by an **Azure AI Foundry** voice agent, books a Teams meeting
on the rep's calendar through **Microsoft Graph**, records the opportunity in **Dataverse** and
notifies the rep in Teams through **Power Automate**.

```
Lead ─► Cockpit ─► Compliance gate ─► Foundry telephony call job
                                          │  Teams Phone extensibility
Phone ◄── PSTN ◄── Teams service number ◄─┘  (resource account ─► ACS)
  │
  └─ conversation ─► Foundry voice agent ──tools──► Tool API (this repo, srv1507394)
                                                     │  Postgres: leads, slots, scores, audit
                                                     ├─ Graph: free/busy + Teams meeting
                                                     └─ outbox ─► Dataverse row
                                                               └► Power Automate ─► Teams card to rep
```

Two demos in one stack:
1. **Live call**: one lead, a real Teams Phone call, live cockpit.
2. **Campaign orchestrator**: a **Microsoft Agent Framework** workflow driving pacing,
   compliance, retries and escalation across thousands of leads, with the Foundry voice agent
   as the dial step, and a MAF escalation agent with grounding, guardrails, token-cost
   management and OpenTelemetry. See `docs/ORCHESTRATOR.md`.

## What is in the box

| Path | What |
|---|---|
| `app/salesagent/api/tool_api.py` | 9 voice-agent tools (OpenAPI at `/agent/openapi.json`), API-key + signed per-call token |
| `app/salesagent/api/cockpit.py` + `static/cockpit` | Live cockpit (lead form, live call, timeline) over SSE |
| `app/salesagent/domain/` | Deterministic scoring rubric, price book, compliance gate, slot finding |
| `app/salesagent/integrations/` | Teams Phone (Foundry call jobs), Graph, Dataverse, Power Automate |
| `app/salesagent/orchestration/` | MAF workflows, escalation agent, guardrails, cost ledger, simulation |
| `app/salesagent/telemetry.py` | OpenTelemetry traces + metrics (MAF instrumentation enabled) |
| `app/salesagent/worker.py` | Outbox delivery with retries, call-job tracking, lease expiry |
| `app/salesagent/provision_dataverse.py` | Creates the Dataverse table and columns |
| `app/salesagent/doctor.py` | End-to-end integration check |
| `app/salesagent/api/mcp.py` | The same tools over MCP (Streamable HTTP) for Foundry voice agents |
| `app/salesagent/trace_maf.py` | Runs the MAF workflows live and prints every executor hand-off |
| `app/salesagent/proof.py` | Proves LLM controls live: guardrails, grounding, budget, tracing, fallback |
| `agent/instructions.md` | Foundry voice agent settings, MCP tool setup and exact instructions |
| `power-automate/` | Flow trigger schema + adaptive card |
| `docs/SETUP.md`, `docs/DEMO.md`, `docs/ORCHESTRATOR.md` | Runbook, demo script, orchestrator design |

## Design decisions

- **The LLM extracts facts; code decides.** Score (rubric `wireless-v1`) and opportunity value
  (price book) are deterministic and auditable to the caller's own words.
- **Confused-deputy safe.** Tools resolve the lead from an HMAC call token minted per attempt;
  model-supplied identifiers are never trusted.
- **No double-booking.** Slot holds are leases enforced by a partial unique index; Graph
  `transactionId` makes meeting creation idempotent.
- **Nothing lost.** CRM and notification writes go through a transactional outbox with
  exponential backoff and a dead-letter state.
- **Degrades honestly.** Each integration is optional at runtime; the cockpit shows which are
  connected and the agent is told what to say when one is unavailable.
- **Locked down.** Only loopback ports, Postgres on an internal-only network, read-only
  containers, dropped capabilities, secrets as files.

## Show it running

On the server, from the repo directory:

```bash
# MAF orchestration: both workflow graphs + one pass with streamed executor events
docker compose stop worker
docker compose run --rm worker python -m salesagent.trace_maf
docker compose start worker

# LLM controls: 8 misbehaving-model scenarios -> cost ledger, guardrail log, spans, PASS/FAIL
docker compose exec worker python -m salesagent.proof
```

## Quick start

See `docs/SETUP.md`. Tests (needs Postgres 16):

```bash
cd app && pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/salesagent_test pytest
```
