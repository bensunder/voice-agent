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

## What is in the box

| Path | What |
|---|---|
| `app/salesagent/api/tool_api.py` | 9 voice-agent tools (OpenAPI at `/agent/openapi.json`), API-key + signed per-call token |
| `app/salesagent/api/cockpit.py` + `static/cockpit` | Live cockpit (lead form, live call, timeline) over SSE |
| `app/salesagent/domain/` | Deterministic scoring rubric, price book, compliance gate, slot finding |
| `app/salesagent/integrations/` | Teams Phone (Foundry call jobs), Graph, Dataverse, Power Automate |
| `app/salesagent/worker.py` | Outbox delivery with retries, call-job tracking, lease expiry |
| `app/salesagent/provision_dataverse.py` | Creates the Dataverse table and columns |
| `app/salesagent/doctor.py` | End-to-end integration check |
| `agent/instructions.md` | Voice agent instructions |
| `power-automate/` | Flow trigger schema + adaptive card |
| `docs/SETUP.md`, `docs/DEMO.md` | Runbook and demo script |

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

## Quick start

See `docs/SETUP.md`. Tests (needs Postgres 16):

```bash
cd app && pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/salesagent_test pytest
```
