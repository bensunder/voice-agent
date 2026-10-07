"""MCP (Model Context Protocol) endpoint exposing the same voice-agent tools.

Foundry voice agents attach tools in the portal via **Add a MCP tool**, so the
tool surface is offered over MCP's Streamable HTTP transport as well as OpenAPI.
This is a small, dependency-free, stateless implementation of the parts a tool
server needs: `initialize`, `ping`, `tools/list`, `tools/call` (JSON responses,
no server-initiated streams). Auth: the same tool API key, sent as `X-API-Key`
or `Authorization: Bearer <key>`. Business logic is shared with the REST tools.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from ..container import Container
from ..domain.qualification import SlotUpdate
from ..security import constant_time_equals
from ..services import agent_tools as tools
from ..services.errors import ServiceError

log = logging.getLogger("salesagent.mcp")

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "ai-sales-agent-tools", "version": "1.0.0"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    model: type[BaseModel]
    run: Callable[[Container, tools.Ctx, Any], Awaitable[dict[str, Any]]]


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline $defs so simple MCP clients do not need $ref resolution."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and node["$ref"].startswith("#/$defs/"):
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    out = walk(schema)
    out.pop("title", None)
    return out


def build_registry() -> dict[str, Tool]:
    from . import tool_api as t  # models live with the REST tools; imported lazily to avoid a cycle

    specs = [
        Tool("get_lead_context",
             "Start of call: call once after the caller agrees to talk. Returns first name, company, answers "
             "already captured and the qualification questions still to ask.",
             t.Base, lambda c, ctx, b: tools.get_lead_context(c, ctx)),
        Tool("save_answers",
             "Record qualification facts the caller just stated. Call after every answer. Returns the band "
             "(hot, qualified, nurture, disqualified, incomplete), recommended next step and remaining questions. "
             "Never tell the caller the numeric score or any price.",
             t.SaveAnswersIn, lambda c, ctx, b: tools.save_answers(c, ctx, SlotUpdate(slots=b.answers))),
        Tool("get_offer_slots",
             "Find two open meeting times with an enterprise specialist. Use when the band is hot or qualified. "
             "Read both 'when' options aloud and let the caller choose.",
             t.Base, lambda c, ctx, b: tools.get_offer_slots(c, ctx)),
        Tool("hold_slot", "Reserve the time the caller picked (pass its slot_id) for a few minutes.",
             t.HoldIn, lambda c, ctx, b: tools.hold_slot(c, ctx, b.slot_id)),
        Tool("book_meeting",
             "Confirm the held time as a Teams meeting on the specialist's calendar. Say 'let me lock that in' first.",
             t.BookIn, lambda c, ctx, b: tools.book_meeting(
                 c, ctx, b.hold_id, str(b.attendee_email) if b.attendee_email else None)),
        Tool("request_transfer",
             "Approve a live hand-off to the specialist. Only for band 'hot' when the caller wants to talk now.",
             t.TransferIn, lambda c, ctx, b: tools.request_transfer(c, ctx, b.reason)),
        Tool("schedule_callback", "Record that the caller wants to be called back later.",
             t.CallbackIn, lambda c, ctx, b: tools.schedule_callback(c, ctx, b.preferred_time, b.note)),
        Tool("opt_out",
             "The caller asked not to be called again. Call immediately; effective at once for all future calls.",
             t.Base, lambda c, ctx, b: tools.opt_out(c, ctx)),
        Tool("complete_call",
             "Wrap up: record the outcome and a 3-5 sentence factual summary. Call once before saying goodbye.",
             t.CompleteIn, lambda c, ctx, b: tools.complete_call(c, ctx, b.outcome, b.summary, b.next_action)),
    ]
    return {s.name: s for s in specs}


def _rpc_result(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _text(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}],
            "structuredContent": payload, "isError": is_error}


def _authorized(request: Request, c: Container) -> bool:
    key = request.headers.get("x-api-key", "")
    auth = request.headers.get("authorization", "")
    if not key and auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    return constant_time_equals(key, c.settings.tool_api_key.get_secret_value())


def build_router() -> APIRouter:
    router = APIRouter()
    registry = build_registry()
    listing = [
        {"name": tl.name, "description": tl.description,
         "inputSchema": _inline_refs(tl.model.model_json_schema())}
        for tl in registry.values()
    ]

    async def handle(c: Container, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        method, id_, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if id_ is None:  # notification (e.g. notifications/initialized): no response
            return None
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            return _rpc_result(id_, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                     "serverInfo": SERVER_INFO,
                                     "instructions": "Sales qualification tools. Always pass call_token."})
        if method == "ping":
            return _rpc_result(id_, {})
        if method == "tools/list":
            return _rpc_result(id_, {"tools": listing})
        if method == "tools/call":
            tl = registry.get(params.get("name", ""))
            if not tl:
                return _rpc_error(id_, -32602, f"Unknown tool: {params.get('name')}")
            try:
                body = tl.model.model_validate(params.get("arguments") or {})
                ctx = await tools.resolve(c, body.call_token)  # type: ignore[attr-defined]
                return _rpc_result(id_, _text(await tl.run(c, ctx, body)))
            except ValidationError as exc:
                errs = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
                return _rpc_result(id_, _text({"error": {"code": "INVALID_ARGUMENTS", "details": errs}}, True))
            except ServiceError as exc:
                err: dict[str, Any] = {"code": exc.code, "message": exc.message}
                if exc.say:
                    err["say"] = exc.say
                return _rpc_result(id_, _text({"error": err}, True))
            except Exception:  # noqa: BLE001 - tool failures are reported to the agent, not raised
                log.exception("MCP tool %s failed", tl.name)
                return _rpc_result(id_, _text({"error": {"code": "INTERNAL", "message": "tool failed"}}, True))
        return _rpc_error(id_, -32601, f"Method not found: {method}")

    @router.post("/mcp", include_in_schema=False)
    async def mcp_post(request: Request) -> Response:
        c: Container = request.app.state.c
        if not _authorized(request, c):
            return JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": 'Bearer realm="mcp"'})
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)
        if isinstance(payload, list):  # JSON-RPC batch (older protocol versions)
            out = [r for r in [await handle(c, m) for m in payload] if r is not None]
            return JSONResponse(out) if out else Response(status_code=202)
        result = await handle(c, payload)
        if result is None:
            return Response(status_code=202)
        return JSONResponse(result)

    @router.get("/mcp", include_in_schema=False)
    async def mcp_get() -> Response:
        # Server-initiated streams are not offered (spec-permitted).
        return Response(status_code=405, headers={"Allow": "POST"})

    @router.delete("/mcp", include_in_schema=False)
    async def mcp_delete() -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    return router
