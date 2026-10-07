"""End-to-end flow against real Postgres: cockpit -> voice tools -> worker."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from salesagent import worker
from salesagent.api import cockpit, tool_api
from salesagent.integrations.http import IntegrationError
from salesagent.integrations.teams_phone import CallJobState

from .conftest import AUTH, KEY

pytestmark = pytest.mark.asyncio(loop_scope="session")


class FakeChannel:
    def __init__(self):
        self.placed: list[dict] = []
        self.status = "in_progress"

    async def place_call(self, *, idempotency_key, phone_e164, inputs):
        self.placed.append({"key": idempotency_key, "phone": phone_e164, "inputs": inputs})
        return CallJobState(f"job-{idempotency_key[:8]}", "accepted", None, 0)

    async def get_call(self, job_id):
        return CallJobState(job_id, self.status, None if self.status != "failed" else "no_answer", 1)

    async def close(self):
        pass


class Recorder:
    def __init__(self, fail_times: int = 0):
        self.calls: list = []
        self.fail_times = fail_times

    async def upsert_qualification(self, attempt_id, record):
        if self.fail_times:
            self.fail_times -= 1
            raise IntegrationError("dataverse", "HTTP 503", 503, retryable=True)
        self.calls.append((attempt_id, record))

    async def notify_rep(self, payload, dedupe_key):
        self.calls.append((dedupe_key, payload))


@asynccontextmanager
async def running(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            yield client


@pytest.fixture(scope="module")
async def apps():
    async with running(cockpit.app) as ck, running(tool_api.app) as tl:
        yield ck, tl


async def tool(tl, name, token, **body):
    return await tl.post(f"/tools/{name}", json={"call_token": token, **body}, headers=KEY)


async def new_lead(ck, phone="(801) 555-0123", first="John"):
    r = await ck.post("/api/leads", headers=AUTH, json={
        "first_name": first, "last_name": "Rivera", "company": "Contoso Logistics",
        "phone": phone, "email": "john@contoso.example", "consent": True})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_auth_is_enforced(apps):
    ck, tl = apps
    assert (await ck.get("/api/state")).status_code == 401
    assert (await ck.get("/")).status_code == 401
    r = await tl.post("/tools/get_lead_context", json={"call_token": "browser"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_API_KEY"
    r = await tool(tl, "get_lead_context", "v1.forged.token")
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CALL_TOKEN"
    assert (await ck.get("/readyz")).json()["status"] == "ready"


async def test_lead_requires_consent_and_valid_phone(apps):
    ck, _ = apps
    r = await ck.post("/api/leads", headers=AUTH, json={"first_name": "A", "company": "B", "phone": "8015550123",
                                                        "consent": False})
    assert r.status_code == 422
    r = await ck.post("/api/leads", headers=AUTH, json={"first_name": "A", "company": "B", "phone": "555",
                                                        "consent": True})
    assert r.status_code == 422


async def test_full_browser_conversation(apps):
    ck, tl = apps
    lead_id = await new_lead(ck)
    r = await ck.post(f"/api/leads/{lead_id}/browser-session", headers=AUTH)
    assert r.status_code == 200, r.text
    attempt_id = r.json()["attempt_id"]

    ctx = (await tool(tl, "get_lead_context", "browser")).json()
    assert ctx["first_name"] == "John" and len(ctx["still_to_ask"]) == 7

    r = await tool(tl, "save_answers", "browser", answers={
        "mobile_lines": {"value": 600, "evidence": "about 600 lines"},
        "current_carrier": {"value": "Verizon"}})
    assert r.json()["band"] == "incomplete"
    r = await tool(tl, "save_answers", "browser", answers={
        "contract_months_remaining": {"value": "4"}, "decision_role": {"value": "decision_maker"},
        "pain_points": {"value": ["cost"]}, "timeline_months": {"value": 3}})
    body = r.json()
    assert body["band"] == "hot" and body["score"] == 100

    bad = await tool(tl, "save_answers", "browser", answers={"mobile_lines": {"value": "lots"}})
    assert bad.status_code == 422

    slots = (await tool(tl, "get_offer_slots", "browser")).json()["slots"]
    assert len(slots) == 2 and all(" at " in s["when"] for s in slots)

    # Arming a second session supersedes the first.
    lead2 = await new_lead(ck, phone="(801) 555-0144", first="Mia")
    r2 = await ck.post(f"/api/leads/{lead2}/browser-session", headers=AUTH)
    assert r2.status_code == 200  # arming a new session supersedes the first
    token2 = r2.json()["call_token"]
    r = await tool(tl, "get_lead_context", "browser")
    assert r.json()["first_name"] == "Mia"
    # re-arm John to continue his call
    r = await ck.post(f"/api/leads/{lead_id}/browser-session", headers=AUTH)
    attempt_id = r.json()["attempt_id"]
    token1 = r.json()["call_token"]
    await tool(tl, "save_answers", token1, answers={
        "mobile_lines": {"value": 600}, "contract_months_remaining": {"value": 4},
        "decision_role": {"value": "decision_maker"}, "pain_points": {"value": ["cost"]},
        "timeline_months": {"value": 3}, "current_carrier": {"value": "Verizon"}})
    # Mia's token belongs to a superseded attempt -> rejected
    assert (await tool(tl, "get_lead_context", token2)).json()["error"]["code"] == "CALL_FINISHED"

    slot = (await tool(tl, "get_offer_slots", token1)).json()["slots"][0]["slot_id"]
    hold = (await tool(tl, "hold_slot", token1, slot_id=slot)).json()
    assert "hold_id" in hold

    booked = (await tool(tl, "book_meeting", token1, hold_id=hold["hold_id"])).json()
    assert booked["booked"] is True and booked["teams_meeting"] is False  # Graph not configured in tests
    again = (await tool(tl, "book_meeting", token1, hold_id=hold["hold_id"])).json()
    assert again.get("already_booked") is True

    r = await tool(tl, "request_transfer", token1, reason="wants to talk now")
    assert r.json()["transfer"] == "approved"

    r = await tool(tl, "complete_call", token1, outcome="meeting_booked",
                   summary="John manages 600 lines on Verizon, renewal in 4 months, cost is the main pain. Meeting booked.")
    assert r.status_code == 200
    assert (await tool(tl, "get_lead_context", token1)).json()["error"]["code"] == "CALL_FINISHED"

    detail = (await ck.get(f"/api/attempts/{attempt_id}", headers=AUTH)).json()
    b = detail["briefing"]
    assert b["score"] == 100 and b["band"] == "hot" and b["contract_value"] == 734400
    assert b["meeting_when"] and b["current_carrier"] == "Verizon"
    kinds = [e["kind"] for e in detail["events"]]
    for k in ("browser_session_armed", "answers_saved", "slots_offered", "slot_held", "meeting_booked",
              "transfer_requested", "call_summarized"):
        assert k in kinds

    # Worker: deliver the outbox to Dataverse + Power Automate, with one transient failure.
    c = ck._transport.app.state.c  # type: ignore[attr-defined]
    dv, pa = Recorder(fail_times=1), Recorder()
    c.dataverse, c.power_automate = dv, pa
    for _ in range(3):
        await worker.process_outbox(c)
        async with c.pool.connection() as conn:
            await conn.execute("UPDATE outbox SET next_attempt_at=now() WHERE status='pending'")
            await conn.commit()
    assert len(dv.calls) == 1 and dv.calls[0][1]["mobile_lines"] == 600
    cards = [p for _, p in pa.calls]
    assert {p["kind"] for p in cards} == {"final", "transfer"}
    final = next(p for p in cards if p["kind"] == "final")
    assert final["estimated_value"] == "$734,400" and final["carrier"] == "Verizon" and final["band"] == "HOT"
    async with c.pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM outbox WHERE status='done'")
        assert (await cur.fetchone())["n"] == 3


async def test_opt_out_blocks_future_calls(apps):
    ck, tl = apps
    lead_id = await new_lead(ck, phone="(801) 555-0177", first="Ava")
    token = (await ck.post(f"/api/leads/{lead_id}/browser-session", headers=AUTH)).json()["call_token"]
    assert (await tool(tl, "opt_out", token)).json()["opted_out"] is True
    r = await ck.post(f"/api/leads/{lead_id}/call", headers=AUTH)
    assert r.json()["status"] == "blocked" and r.json()["reason"] == "OPTED_OUT"
    r = await ck.post(f"/api/leads/{lead_id}/browser-session", headers=AUTH)
    assert r.status_code == 400


async def test_teams_phone_call_lifecycle(apps):
    ck, tl = apps
    c = ck._transport.app.state.c  # type: ignore[attr-defined]
    async with c.pool.connection() as conn:
        await conn.execute("DELETE FROM opt_out")
        await conn.commit()

    # not allowlisted in demo mode -> blocked by the gate
    other = await new_lead(ck, phone="(801) 555-0199", first="Leo")
    r = await ck.post(f"/api/leads/{other}/call", headers=AUTH)
    assert r.json() == {"attempt_id": r.json()["attempt_id"], "status": "blocked", "reason": "DEMO_NOT_ALLOWLISTED"}

    lead_id = await new_lead(ck)  # allowlisted number
    c.channel = None
    r = await ck.post(f"/api/leads/{lead_id}/call", headers=AUTH)
    assert r.status_code == 503 and r.json()["error"]["code"] == "TEAMS_PHONE_NOT_CONFIGURED"

    fake = FakeChannel()
    c.channel = fake
    r = await ck.post(f"/api/leads/{lead_id}/call", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "dialing", r.text
    attempt_id = r.json()["attempt_id"]
    placed = fake.placed[0]
    assert placed["phone"] == "+18015550123" and placed["key"] == attempt_id
    assert set(placed["inputs"]) == {"first_name", "company", "company_name", "product_interest", "call_token"}

    # duplicate click while dialing is rejected
    assert (await ck.post(f"/api/leads/{lead_id}/call", headers=AUTH)).status_code == 409

    token = placed["inputs"]["call_token"]
    assert (await tool(tl, "get_lead_context", token)).status_code == 200
    await tool(tl, "save_answers", token, answers={"mobile_lines": {"value": 120},
               "contract_months_remaining": {"value": 9}, "decision_role": {"value": "influencer"}})

    fake.status = "completed"
    await worker.track_calls(c)
    detail = (await ck.get(f"/api/attempts/{attempt_id}", headers=AUTH)).json()
    assert detail["attempt"]["status"] == "completed"
    assert "call_ended" in [e["kind"] for e in detail["events"]]
    async with c.pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM outbox WHERE dedupe_key LIKE %s", (f"%{attempt_id}%",))
        assert (await cur.fetchone())["n"] == 2
    # agent token is dead once the call has ended
    assert (await tool(tl, "get_lead_context", token)).json()["error"]["code"] == "CALL_FINISHED"


async def test_unknown_attempt_and_openapi(apps):
    ck, tl = apps
    r = await ck.get(f"/api/attempts/{uuid.uuid4()}", headers=AUTH)
    assert r.status_code == 404
    spec = (await tl.get("/agent/openapi.json")).json()
    ops = {op["operationId"] for p in spec["paths"].values() for op in p.values()}
    assert ops == {"get_lead_context", "save_answers", "get_offer_slots", "hold_slot", "book_meeting",
                   "request_transfer", "schedule_callback", "opt_out", "complete_call"}
    assert spec["servers"][0]["url"].startswith("https://")
    assert "APIKeyHeader" in spec["components"]["securitySchemes"]


async def test_demo_reset(apps):
    ck, _ = apps
    assert (await ck.post("/api/demo/reset", headers=AUTH)).json() == {"status": "reset"}
    s = (await ck.get("/api/state", headers=AUTH)).json()
    assert s["leads"] == [] and s["attempts"] == []


async def test_concurrent_holds_on_one_slot_have_exactly_one_winner(apps):
    ck, tl = apps
    c = ck._transport.app.state.c  # type: ignore[attr-defined]
    fake = FakeChannel()
    c.channel = fake
    c.settings.demo_mode = False  # several distinct numbers for this test
    try:
        tokens = []
        for i in range(6):
            lid = await new_lead(ck, phone=f"(801) 555-02{i:02d}", first=f"Racer{i}")
            r = await ck.post(f"/api/leads/{lid}/call", headers=AUTH)
            assert r.json()["status"] == "dialing", r.text
            tokens.append(fake.placed[-1]["inputs"]["call_token"])
        slot = (await tool(tl, "get_offer_slots", tokens[0])).json()["slots"][0]["slot_id"]
        results = await asyncio.gather(*[tool(tl, "hold_slot", t, slot_id=slot) for t in tokens])
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 409, 409, 409, 409, 409], [r.json() for r in results]
        assert all(r.json()["error"]["code"] == "SLOT_TAKEN" for r in results if r.status_code == 409)
        # the slot is no longer offered to anyone else
        offered = [s["slot_id"] for s in (await tool(tl, "get_offer_slots", tokens[1])).json()["slots"]]
        assert slot not in offered
    finally:
        c.settings.demo_mode = True
