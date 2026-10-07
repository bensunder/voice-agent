"""Authentication primitives.

* Tool API key  - static shared secret held in the Foundry tool connection.
* Call token    - short-lived HMAC token minted per call attempt and handed to the
                  voice agent as a structured input. Every tool call must carry it;
                  the server resolves the lead from the token and ignores any lead
                  identifier the model might supply (confused-deputy protection).
* Cockpit       - HTTP basic auth for the human-facing UI.
* Browser demo  - in demo mode only, the literal token "browser" resolves to the
                  single session armed from the cockpit (Foundry browser preview
                  cannot always inject per-call inputs). It expires automatically.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass

TOKEN_VERSION = "v1"


class TokenError(Exception):
    pass


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class CallClaims:
    lead_id: uuid.UUID
    attempt_id: uuid.UUID
    expires_at: int


def mint_call_token(secret: str, lead_id: uuid.UUID, attempt_id: uuid.UUID, ttl_seconds: int) -> str:
    payload = {"l": str(lead_id), "a": str(attempt_id), "e": int(time.time()) + ttl_seconds}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(secret.encode(), f"{TOKEN_VERSION}.{body}".encode(), hashlib.sha256).digest())
    return f"{TOKEN_VERSION}.{body}.{sig}"


def verify_call_token(secret: str, token: str, now: float | None = None) -> CallClaims:
    try:
        version, body, sig = token.strip().split(".")
    except ValueError as exc:
        raise TokenError("malformed token") from exc
    if version != TOKEN_VERSION:
        raise TokenError("unsupported token version")
    expected = _b64e(hmac.new(secret.encode(), f"{version}.{body}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise TokenError("bad signature")
    try:
        data = json.loads(_b64d(body))
        claims = CallClaims(uuid.UUID(data["l"]), uuid.UUID(data["a"]), int(data["e"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenError("bad payload") from exc
    if claims.expires_at < (now if now is not None else time.time()):
        raise TokenError("token expired")
    return claims


def constant_time_equals(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())
