"""Microsoft Graph: rep availability and Teams meeting booking.

Uses application permissions (client credentials). Grant only:
  * Calendars.ReadWrite (Application)
and scope it to the sales-rep mailboxes with Exchange RBAC for Applications so
the app cannot touch any other mailbox in the tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .http import IntegrationError, TokenProvider, raise_for, request_with_retry

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/.default"
SYSTEM = "graph"


@dataclass(frozen=True)
class BusyBlock:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class BookedEvent:
    event_id: str
    join_url: str | None
    web_link: str | None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse(dt_obj: dict[str, str]) -> datetime:
    raw = dt_obj["dateTime"].split(".")[0]
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


class GraphClient:
    def __init__(self, tokens: TokenProvider, http: httpx.AsyncClient):
        self._tokens = tokens
        self._http = http

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._tokens.token(SCOPE)}",
            "Prefer": 'outlook.timezone="UTC"',
        }

    async def busy_blocks(self, upn: str, start: datetime, end: datetime) -> list[BusyBlock]:
        url = f"{GRAPH}/users/{quote(upn)}/calendarView"
        params = {
            "startDateTime": _iso(start),
            "endDateTime": _iso(end),
            "$select": "start,end,showAs,isCancelled",
            "$top": "200",
        }
        blocks: list[BusyBlock] = []
        next_url: str | None = url
        while next_url:
            resp = await request_with_retry(
                self._http, SYSTEM, "GET", next_url,
                params=params if next_url == url else None,
                headers=await self._headers(),
            )
            raise_for(SYSTEM, resp)
            data = resp.json()
            for ev in data.get("value", []):
                if ev.get("isCancelled") or ev.get("showAs") == "free":
                    continue
                blocks.append(BusyBlock(_parse(ev["start"]), _parse(ev["end"])))
            next_url = data.get("@odata.nextLink")
        return blocks

    async def book_teams_meeting(
        self,
        *,
        organizer_upn: str,
        subject: str,
        body_html: str,
        start: datetime,
        end: datetime,
        attendee_email: str | None,
        attendee_name: str,
        transaction_id: str,
    ) -> BookedEvent:
        """Create a Teams meeting on the rep's calendar.

        `transactionId` makes the POST idempotent on Graph's side, so a retry after a
        timeout cannot create a duplicate meeting.
        """
        event: dict[str, object] = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "start": {"dateTime": _iso(start), "timeZone": "UTC"},
            "end": {"dateTime": _iso(end), "timeZone": "UTC"},
            "isOnlineMeeting": True,
            "onlineMeetingProvider": "teamsForBusiness",
            "allowNewTimeProposals": True,
            "transactionId": transaction_id,
        }
        if attendee_email:
            event["attendees"] = [
                {"emailAddress": {"address": attendee_email, "name": attendee_name}, "type": "required"}
            ]
        resp = await request_with_retry(
            self._http, SYSTEM, "POST", f"{GRAPH}/users/{quote(organizer_upn)}/events",
            json=event, headers=await self._headers(),
        )
        raise_for(SYSTEM, resp)
        data = resp.json()
        if not data.get("id"):
            raise IntegrationError(SYSTEM, "event created without id")
        return BookedEvent(
            event_id=data["id"],
            join_url=(data.get("onlineMeeting") or {}).get("joinUrl"),
            web_link=data.get("webLink"),
        )
