"""Teams Phone outbound calling through the Foundry voice agent.

Call path: Foundry telephony call job -> Azure Communication Services /
Teams Phone extensibility connection -> Teams resource account (caller ID is the
Teams service number) -> PSTN -> lead. When the lead answers, Foundry streams
audio to the voice agent.

The orchestrator depends on the `CallChannel` protocol only, so the channel can
be swapped (e.g. Twilio connection) without touching business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import CreateTelephonyCallJobRequest, TelephonyOutboundDestination
from azure.core.exceptions import HttpResponseError

from .http import IntegrationError, TokenProvider

SYSTEM = "teams_phone"

TERMINAL = {"completed", "blocked", "expired", "failed", "cancelled"}


@dataclass(frozen=True)
class CallJobState:
    job_id: str
    status: str
    terminal_reason: str | None
    attempt_count: int | None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


class CallChannel(Protocol):
    async def place_call(self, *, idempotency_key: str, phone_e164: str, inputs: dict[str, Any]) -> CallJobState: ...
    async def get_call(self, job_id: str) -> CallJobState: ...
    async def close(self) -> None: ...


def _state(job: Any) -> CallJobState:
    status = getattr(job, "status", None)
    reason = getattr(job, "terminal_reason", None)
    return CallJobState(
        job_id=str(job.id),
        status=str(getattr(status, "value", status) or "unknown"),
        terminal_reason=str(getattr(reason, "value", reason)) if reason else None,
        attempt_count=getattr(job, "attempt_count", None),
    )


class TeamsPhoneChannel:
    def __init__(
        self,
        tokens: TokenProvider,
        project_endpoint: str,
        agent_name: str,
        connection_name: str,
        resource_account_id: str,
    ):
        self._client = AIProjectClient(endpoint=project_endpoint, credential=tokens.credential)
        self._agent = agent_name
        self._connection = connection_name
        self._source = resource_account_id

    async def place_call(self, *, idempotency_key: str, phone_e164: str, inputs: dict[str, Any]) -> CallJobState:
        body = CreateTelephonyCallJobRequest(
            destination=TelephonyOutboundDestination(type="phone_number", value=phone_e164),
            connection_name=self._connection,
            source=self._source,
            purpose="sales_lead_qualification",
            structured_inputs=inputs,
        )
        try:
            job = await self._client.beta.voice_agents.telephony.create_call_job(
                self._agent, body, idempotency_key=idempotency_key
            )
        except HttpResponseError as exc:
            raise IntegrationError(SYSTEM, f"create_call_job failed: {exc.message}", exc.status_code,
                                   retryable=(exc.status_code or 0) >= 500) from exc
        return _state(job)

    async def get_call(self, job_id: str) -> CallJobState:
        try:
            job = await self._client.beta.voice_agents.telephony.get_call_job(self._agent, job_id)
        except HttpResponseError as exc:
            raise IntegrationError(SYSTEM, f"get_call_job failed: {exc.message}", exc.status_code,
                                   retryable=(exc.status_code or 0) >= 500) from exc
        return _state(job)

    async def close(self) -> None:
        await self._client.close()
