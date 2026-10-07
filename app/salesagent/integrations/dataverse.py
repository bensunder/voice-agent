"""Power Platform Dataverse: the CRM-facing record of every qualified call.

The table is created by `python -m salesagent.provision_dataverse`, which uses the
environment's default publisher prefix. Rows use the call-attempt UUID as their
primary key, so writes are PATCH upserts and replays are naturally idempotent.

The service principal needs an *application user* in the Power Platform
environment with a security role that can create/update this table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from .http import TokenProvider, raise_for, request_with_retry

SYSTEM = "dataverse"
API = "/api/data/v9.2"


class DataverseClient:
    def __init__(self, tokens: TokenProvider, http: httpx.AsyncClient, base_url: str, prefix: str, entity_set: str):
        self._tokens = tokens
        self._http = http
        self._base = base_url.rstrip("/")
        self._prefix = prefix
        self._set = entity_set

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {await self._tokens.token(self._base + '/.default')}",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        if extra:
            h.update(extra)
        return h

    def _col(self, name: str) -> str:
        return f"{self._prefix}_{name}"

    def build_record(self, r: dict[str, Any]) -> dict[str, Any]:
        """Map the canonical briefing to Dataverse columns (None values are omitted)."""
        meeting_start: datetime | None = r.get("meeting_start")
        fields = {
            "name": f"{r['company']} - {r['contact_name']}"[:100],
            "company": r["company"],
            "contactname": r["contact_name"],
            "phone": r["phone"],
            "score": r.get("score"),
            "band": r.get("band"),
            "mobilelines": r.get("mobile_lines"),
            "currentcarrier": r.get("current_carrier"),
            "renewalmonths": r.get("contract_months_remaining"),
            "decisionrole": r.get("decision_role"),
            "opportunityvalue": r.get("contract_value"),
            "reasoncodes": ", ".join(r.get("reason_codes") or [])[:400] or None,
            "summary": r.get("summary"),
            "nextaction": r.get("next_action"),
            "outcome": r.get("outcome"),
            "meetingstart": meeting_start.isoformat() if meeting_start else None,
            "meetingurl": r.get("join_url"),
            "repupn": r.get("rep_upn"),
        }
        return {self._col(k): v for k, v in fields.items() if v is not None}

    async def upsert_qualification(self, attempt_id: str, record: dict[str, Any]) -> None:
        url = f"{self._base}{API}/{self._set}({attempt_id})"
        resp = await request_with_retry(
            self._http, SYSTEM, "PATCH", url, json=self.build_record(record), headers=await self._headers()
        )
        raise_for(SYSTEM, resp)
