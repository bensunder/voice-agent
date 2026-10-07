"""Tool API called by the Foundry voice agent during a live call.

Imported into the agent as an OpenAPI tool from /agent/openapi.json, with the
API key configured as a `X-API-Key` header connection. Every operation also
requires the per-call `call_token` the agent received as a structured input.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, EmailStr, Field

from ..config import get_settings
from ..container import Container
from ..domain.qualification import SLOT_SCHEMA, SlotValue, SlotUpdate
from ..security import constant_time_equals
from ..services import agent_tools as tools
from ..services.errors import Unauthorized
from .common import install, lifespan_factory

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Tool API key")


async def require_api_key(request: Request, key: str | None = Depends(api_key_header)) -> None:
    c: Container = request.app.state.c
    if not constant_time_equals(key or "", c.settings.tool_api_key.get_secret_value()):
        raise Unauthorized("INVALID_API_KEY", "missing or invalid API key")


router = APIRouter(prefix="/tools", dependencies=[Depends(require_api_key)])

TOKEN_DESC = ("The call_token value from this conversation's inputs. Pass it exactly as received; "
              "use the literal 'browser' only if no call_token input exists.")


class Base(BaseModel):
    call_token: str = Field(min_length=3, max_length=2048, description=TOKEN_DESC)


class SaveAnswersIn(Base):
    answers: dict[str, SlotValue] = Field(
        description=(
            "Facts the caller stated, keyed by slot name. Allowed slots: "
            + "; ".join(f"{k} ({label})" for k, (_, label) in SLOT_SCHEMA.items())
            + ". decision_role is one of decision_maker, influencer, neither. pain_points is a list "
            "drawn from cost, coverage, service, devices, other. Include the caller's own words as "
            "evidence and lower confidence when the answer was vague."
        ),
    )


class HoldIn(Base):
    slot_id: str = Field(description="slot_id returned by get_offer_slots for the time the caller chose")


class BookIn(Base):
    hold_id: str = Field(description="hold_id returned by hold_slot")
    attendee_email: EmailStr | None = Field(
        default=None, description="Caller's email only if they spelled it out and confirmed it")


class TransferIn(Base):
    reason: str = Field(default="caller asked to speak with a specialist", max_length=200)


class CallbackIn(Base):
    preferred_time: str = Field(max_length=120, description="When the caller asked to be called back, in their words")
    note: str | None = Field(default=None, max_length=200)


class CompleteIn(Base):
    outcome: Literal[
        "meeting_booked", "transferred", "callback_requested", "nurture", "not_interested",
        "wrong_person", "opted_out", "voicemail", "other",
    ]
    summary: str = Field(min_length=10, max_length=4000,
                         description="3-5 sentence factual summary for the salesperson. No invented numbers.")
    next_action: str | None = Field(default=None, max_length=400)


async def _ctx(request: Request, token: str) -> tuple[Container, tools.Ctx]:
    c: Container = request.app.state.c
    return c, await tools.resolve(c, token)


@router.post("/get_lead_context", operation_id="get_lead_context",
             summary="Start of call: who you are speaking with and what is still unknown")
async def get_lead_context(body: Base, request: Request) -> dict[str, Any]:
    """Call once right after the caller agrees to talk. Returns their first name, company,
    any answers already captured, and the qualification questions still to ask."""
    c, ctx = await _ctx(request, body.call_token)
    return await tools.get_lead_context(c, ctx)


@router.post("/save_answers", operation_id="save_answers",
             summary="Record qualification facts the caller just stated")
async def save_answers(body: SaveAnswersIn, request: Request) -> dict[str, Any]:
    """Call every time the caller answers a qualification question. Returns the lead's band
    (hot, qualified, nurture, disqualified, incomplete), the recommended next step and which
    questions remain. Never state the numeric score or any price to the caller."""
    c, ctx = await _ctx(request, body.call_token)
    return await tools.save_answers(c, ctx, SlotUpdate(slots=body.answers))


@router.post("/get_offer_slots", operation_id="get_offer_slots",
             summary="Find two open meeting times with an enterprise specialist")
async def get_offer_slots(body: Base, request: Request) -> dict[str, Any]:
    """Call when the band is hot or qualified and the caller is open to a meeting.
    Read the 'when' text of both options aloud and let the caller choose."""
    c, ctx = await _ctx(request, body.call_token)
    return await tools.get_offer_slots(c, ctx)


@router.post("/hold_slot", operation_id="hold_slot",
             summary="Reserve the time the caller picked for a few minutes")
async def hold_slot(body: HoldIn, request: Request) -> dict[str, Any]:
    """Call as soon as the caller picks a time, before confirming details. If the slot was
    taken, apologise and offer the other time."""
    c, ctx = await _ctx(request, body.call_token)
    return await tools.hold_slot(c, ctx, body.slot_id)


@router.post("/book_meeting", operation_id="book_meeting",
             summary="Confirm the held time as a Teams meeting on the specialist's calendar")
async def book_meeting(body: BookIn, request: Request) -> dict[str, Any]:
    """Call after the caller confirms the held time. Say 'let me lock that in' first; this can
    take a second or two."""
    c, ctx = await _ctx(request, body.call_token)
    return await tools.book_meeting(c, ctx, body.hold_id, str(body.attendee_email) if body.attendee_email else None)


@router.post("/request_transfer", operation_id="request_transfer",
             summary="Approve a live hand-off to the specialist (hot leads only)")
async def request_transfer(body: TransferIn, request: Request) -> dict[str, Any]:
    """Call only when the band is hot and the caller wants to talk now. If approved, use your
    transfer capability with the 'sales_specialist' target."""
    c, ctx = await _ctx(request, body.call_token)
    return await tools.request_transfer(c, ctx, body.reason)


@router.post("/schedule_callback", operation_id="schedule_callback",
             summary="Record that the caller wants to be called back later")
async def schedule_callback(body: CallbackIn, request: Request) -> dict[str, Any]:
    c, ctx = await _ctx(request, body.call_token)
    return await tools.schedule_callback(c, ctx, body.preferred_time, body.note)


@router.post("/opt_out", operation_id="opt_out",
             summary="The caller asked not to be called again")
async def opt_out(body: Base, request: Request) -> dict[str, Any]:
    """Call immediately whenever the caller says stop calling, remove me, or do not call.
    Effective at once for all future calls."""
    c, ctx = await _ctx(request, body.call_token)
    return await tools.opt_out(c, ctx)


@router.post("/complete_call", operation_id="complete_call",
             summary="Wrap up: record the outcome and a summary for the salesperson")
async def complete_call(body: CompleteIn, request: Request) -> dict[str, Any]:
    """Call once at the end of every conversation, before saying goodbye."""
    c, ctx = await _ctx(request, body.call_token)
    return await tools.complete_call(c, ctx, body.outcome, body.summary, body.next_action)


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="AI Sales Agent - Voice Tools",
        version="1.0.0",
        description="Tools for the Foundry voice agent that qualifies inbound sales leads.",
        lifespan=lifespan_factory(pool_size=10),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install(app)
    app.include_router(router)
    from .mcp import build_router as build_mcp_router  # same tools over MCP for Foundry voice agents

    app.include_router(build_mcp_router())

    spec_cache: dict[str, Any] = {}

    @app.get("/agent/openapi.json", include_in_schema=False)
    async def agent_openapi() -> dict[str, Any]:
        if not spec_cache:
            spec = get_openapi(title=app.title, version=app.version, description=app.description,
                               routes=app.routes)
            spec["servers"] = [{"url": s.public_tool_api_url}]
            spec_cache.update(spec)
        return spec_cache

    return app


app = create_app()
