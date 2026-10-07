"""Contract tests for the Microsoft integrations using recorded-shape HTTP mocks."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from azure.ai.projects.models import (
    CreateTelephonyCallJobRequest,
    TelephonyOutboundDestination,
)

from salesagent.integrations.dataverse import DataverseClient
from salesagent.integrations.graph import GraphClient
from salesagent.integrations.http import IntegrationError
from salesagent.integrations.power_automate import PowerAutomateClient

UTC = timezone.utc


class FakeTokens:
    def __init__(self):
        self.scopes: list[str] = []

    async def token(self, scope: str) -> str:
        self.scopes.append(scope)
        return "tok"


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_graph_busy_blocks_paginates_and_skips_free():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if "skiptoken" not in str(req.url):
            return httpx.Response(200, json={
                "value": [
                    {"start": {"dateTime": "2026-10-08T16:00:00.0000000"}, "end": {"dateTime": "2026-10-08T17:00:00.0000000"}, "showAs": "busy"},
                    {"start": {"dateTime": "2026-10-08T18:00:00.0000000"}, "end": {"dateTime": "2026-10-08T19:00:00.0000000"}, "showAs": "free"},
                ],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/a/calendarView?$skiptoken=x"})
        return httpx.Response(200, json={"value": [
            {"start": {"dateTime": "2026-10-09T15:00:00"}, "end": {"dateTime": "2026-10-09T15:30:00"}, "showAs": "tentative", "isCancelled": False}]})

    tokens = FakeTokens()
    g = GraphClient(tokens, client(handler))  # type: ignore[arg-type]
    blocks = await g.busy_blocks("ana@contoso.com", datetime(2026, 10, 8, tzinfo=UTC), datetime(2026, 10, 10, tzinfo=UTC))
    assert [b.start for b in blocks] == [datetime(2026, 10, 8, 16, tzinfo=UTC), datetime(2026, 10, 9, 15, tzinfo=UTC)]
    assert seen[0].headers["Prefer"] == 'outlook.timezone="UTC"'
    assert seen[0].url.params["startDateTime"] == "2026-10-08T00:00:00"
    assert tokens.scopes[0] == "https://graph.microsoft.com/.default"


async def test_graph_books_idempotent_teams_meeting():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "AAMk1", "webLink": "https://outlook",
                                         "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/x"}})

    g = GraphClient(FakeTokens(), client(handler))  # type: ignore[arg-type]
    ev = await g.book_teams_meeting(
        organizer_upn="ana@contoso.com", subject="s", body_html="<p>b</p>",
        start=datetime(2026, 10, 8, 16, 30, tzinfo=UTC), end=datetime(2026, 10, 8, 17, tzinfo=UTC),
        attendee_email="john@x.com", attendee_name="John", transaction_id="hold-123")
    b = captured["body"]
    assert captured["url"].endswith("/users/ana%40contoso.com/events")
    assert b["isOnlineMeeting"] is True and b["onlineMeetingProvider"] == "teamsForBusiness"
    assert b["transactionId"] == "hold-123" and b["start"] == {"dateTime": "2026-10-08T16:30:00", "timeZone": "UTC"}
    assert b["attendees"][0]["emailAddress"]["address"] == "john@x.com"
    assert ev.join_url.startswith("https://teams.microsoft.com/")


async def test_graph_errors_surface_with_status():
    g = GraphClient(FakeTokens(), client(lambda r: httpx.Response(403, json={"error": {"message": "Access is denied"}})))  # type: ignore[arg-type]
    with pytest.raises(IntegrationError) as ei:
        await g.busy_blocks("a@x.com", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
    assert ei.value.status == 403 and "Access is denied" in str(ei.value)


async def test_dataverse_upsert_uses_attempt_id_and_prefix():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(method=req.method, url=str(req.url), body=json.loads(req.content))
        return httpx.Response(204)

    tokens = FakeTokens()
    dv = DataverseClient(tokens, client(handler), "https://org1.crm.dynamics.com/", "cr7a1",  # type: ignore[arg-type]
                         "cr7a1_aicallqualifications")
    await dv.upsert_qualification("0d4c9a1e-1111-2222-3333-444455556666", {
        "company": "Contoso", "contact_name": "John Rivera", "phone": "+18015550123", "score": 100,
        "band": "hot", "mobile_lines": 600, "contract_value": 734400.0, "reason_codes": ["A", "B"],
        "meeting_start": datetime(2026, 10, 8, 16, 30, tzinfo=UTC), "summary": None})
    assert captured["method"] == "PATCH"
    assert captured["url"] == ("https://org1.crm.dynamics.com/api/data/v9.2/"
                               "cr7a1_aicallqualifications(0d4c9a1e-1111-2222-3333-444455556666)")
    body = captured["body"]
    assert body["cr7a1_name"] == "Contoso - John Rivera" and body["cr7a1_mobilelines"] == 600
    assert body["cr7a1_reasoncodes"] == "A, B" and "cr7a1_summary" not in body
    assert tokens.scopes == ["https://org1.crm.dynamics.com/.default"]


async def test_power_automate_retries_then_succeeds():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}) if calls["n"] == 1 else httpx.Response(202)

    pa = PowerAutomateClient(client(handler), "https://prod.westus.logic.azure.com/workflows/x")
    await pa.notify_rep({"kind": "final"}, "k")
    assert calls["n"] == 2


def test_teams_call_job_request_shape():
    body = CreateTelephonyCallJobRequest(
        destination=TelephonyOutboundDestination(type="phone_number", value="+18015550123"),
        connection_name="teams-phone", source="ra-object-id", purpose="sales_lead_qualification",
        structured_inputs={"first_name": "John", "call_token": "v1.x.y"})
    d = body.as_dict()
    assert d["destination"] == {"type": "phone_number", "value": "+18015550123"}
    assert d["connection_name"] == "teams-phone" and d["source"] == "ra-object-id"
    assert d["structured_inputs"]["call_token"] == "v1.x.y"
