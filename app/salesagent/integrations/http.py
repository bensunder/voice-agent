"""Shared HTTP plumbing: Entra tokens and a retrying request helper."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx
from azure.identity.aio import ClientSecretCredential

log = logging.getLogger(__name__)

RETRYABLE = {408, 429, 500, 502, 503, 504}


class IntegrationError(Exception):
    """A call to an external system failed in a way the caller should surface."""

    def __init__(self, system: str, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(f"{system}: {message}")
        self.system = system
        self.status = status
        self.retryable = retryable


class NotConfigured(IntegrationError):
    def __init__(self, system: str):
        super().__init__(system, "not configured", retryable=False)


class TokenProvider:
    """Caches one ClientSecretCredential; azure-identity caches tokens per scope."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        self._cred = ClientSecretCredential(tenant_id, client_id, client_secret)

    @property
    def credential(self) -> ClientSecretCredential:
        return self._cred

    async def token(self, scope: str) -> str:
        return (await self._cred.get_token(scope)).token

    async def close(self) -> None:
        await self._cred.close()


async def request_with_retry(
    client: httpx.AsyncClient,
    system: str,
    method: str,
    url: str,
    *,
    attempts: int = 4,
    **kwargs: Any,
) -> httpx.Response:
    """Retry transient failures with jittered backoff, honouring Retry-After."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt == attempts:
                break
            await asyncio.sleep(min(8.0, 0.5 * 2**attempt) + random.random() / 4)
            continue
        if resp.status_code not in RETRYABLE or attempt == attempts:
            return resp
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * 2**attempt
        log.warning("%s %s %s -> %s, retrying in %.1fs", system, method, url, resp.status_code, delay)
        await asyncio.sleep(min(delay, 20.0) + random.random() / 4)
    raise IntegrationError(system, f"transport failure: {last_exc}", retryable=True)


def raise_for(system: str, resp: httpx.Response) -> None:
    if resp.is_success:
        return
    try:
        body = resp.json()
        msg = body.get("error", {}).get("message") or str(body)[:300]
    except ValueError:
        msg = resp.text[:300]
    raise IntegrationError(system, f"HTTP {resp.status_code}: {msg}", resp.status_code, resp.status_code in RETRYABLE)
