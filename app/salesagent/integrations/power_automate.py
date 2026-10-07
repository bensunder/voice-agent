"""Power Automate: notify the salesperson in Teams.

The flow is triggered by "When an HTTP request is received" and posts an
adaptive card to the rep in Teams (see power-automate/README.md). The trigger
URL contains a SAS signature, so it is stored as a secret.
"""

from __future__ import annotations

from typing import Any

import httpx

from .http import raise_for, request_with_retry

SYSTEM = "power_automate"


class PowerAutomateClient:
    def __init__(self, http: httpx.AsyncClient, webhook_url: str):
        self._http = http
        self._url = webhook_url

    async def notify_rep(self, payload: dict[str, Any], dedupe_key: str) -> None:
        resp = await request_with_retry(
            self._http, SYSTEM, "POST", self._url, json=payload,
            headers={"Content-Type": "application/json", "x-dedupe-key": dedupe_key},
        )
        raise_for(SYSTEM, resp)
