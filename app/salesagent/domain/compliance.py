"""Compliance gate: decides whether a lead may be dialled right now.

Pure function over explicit inputs so it is fully unit-testable. Every decision
is persisted by the caller to the audit log with its reason code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

E164 = re.compile(r"^\+[1-9]\d{7,14}$")
US_E164 = re.compile(r"^\+1[2-9]\d{2}[2-9]\d{6}$")


class Verdict(StrEnum):
    ALLOW = "allow"
    DEFER = "defer"
    DENY = "deny"


@dataclass(frozen=True)
class GateInput:
    phone_e164: str
    has_consent: bool
    opted_out: bool
    attempts_today: int
    lead_timezone: ZoneInfo
    now_utc: datetime
    demo_mode: bool
    allowlist: frozenset[str]
    window_start_hour: int
    window_end_hour: int
    max_attempts_per_day: int = 3


@dataclass(frozen=True)
class GateDecision:
    verdict: Verdict
    reason: str
    retry_at_utc: datetime | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


def normalize_us_phone(raw: str) -> str:
    """Normalise common US formats to E.164; raises ValueError if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "1" + digits
    candidate = "+" + digits
    if not US_E164.match(candidate):
        raise ValueError("phone must be a valid US number")
    return candidate


def evaluate(g: GateInput) -> GateDecision:
    if not E164.match(g.phone_e164):
        return GateDecision(Verdict.DENY, "INVALID_PHONE")
    if g.opted_out:
        return GateDecision(Verdict.DENY, "OPTED_OUT")
    if not g.has_consent:
        return GateDecision(Verdict.DENY, "NO_CONSENT")
    if g.demo_mode and g.phone_e164 not in g.allowlist:
        return GateDecision(Verdict.DENY, "DEMO_NOT_ALLOWLISTED")
    if g.attempts_today >= g.max_attempts_per_day:
        return GateDecision(Verdict.DENY, "ATTEMPT_CAP")

    local = g.now_utc.astimezone(g.lead_timezone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight + timedelta(hours=g.window_start_hour)
    end = midnight + timedelta(hours=g.window_end_hour)  # 24 means "until midnight"
    if local < start:
        return GateDecision(Verdict.DEFER, "OUTSIDE_CALLING_WINDOW", start.astimezone(g.now_utc.tzinfo))
    if local >= end:
        nxt = start + timedelta(days=1)
        return GateDecision(Verdict.DEFER, "OUTSIDE_CALLING_WINDOW", nxt.astimezone(g.now_utc.tzinfo))
    return GateDecision(Verdict.ALLOW, "CLEARED")
