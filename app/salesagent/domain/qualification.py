"""Qualification slots, deterministic scoring and opportunity valuation.

The voice agent only *extracts facts* into typed slots. The score and the dollar
value are computed here, deterministically, from a versioned rubric and price
book. The language model never produces either number, so the forecast can be
audited back to the caller's own words (each slot keeps its evidence span).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

RUBRIC_VERSION = "wireless-v1"
LOW_CONFIDENCE = 0.6


class DecisionRole(StrEnum):
    DECISION_MAKER = "decision_maker"
    INFLUENCER = "influencer"
    NEITHER = "neither"


class PainPoint(StrEnum):
    COST = "cost"
    COVERAGE = "coverage"
    SERVICE = "service"
    DEVICES = "devices"
    OTHER = "other"


# slot name -> (python type, human label)
SLOT_SCHEMA: dict[str, tuple[type, str]] = {
    "mobile_lines": (int, "Mobile lines"),
    "current_carrier": (str, "Current carrier"),
    "contract_months_remaining": (int, "Contract renewal (months)"),
    "decision_role": (str, "Decision role"),
    "pain_points": (list, "Pain points"),
    "timeline_months": (int, "Buying timeline (months)"),
    "budget_confirmed": (bool, "Budget confirmed"),
}

REQUIRED_FOR_SCORE = ("mobile_lines", "contract_months_remaining", "decision_role")


class SlotValue(BaseModel):
    """One fact the agent heard, with its confidence and supporting quote."""

    value: Any
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    evidence: str | None = Field(default=None, max_length=500)


class SlotUpdate(BaseModel):
    """Payload of the `save_answers` tool. Unknown slot names are rejected."""

    slots: dict[str, SlotValue] = Field(min_length=1, max_length=len(SLOT_SCHEMA))

    @field_validator("slots")
    @classmethod
    def _validate(cls, slots: dict[str, SlotValue]) -> dict[str, SlotValue]:
        for name, sv in slots.items():
            if name not in SLOT_SCHEMA:
                raise ValueError(f"unknown slot '{name}'; allowed: {sorted(SLOT_SCHEMA)}")
            sv.value = coerce_slot(name, sv.value)
        return slots


def coerce_slot(name: str, value: Any) -> Any:
    """Normalise agent-provided values into the slot's declared type."""
    kind = SLOT_SCHEMA[name][0]
    if value is None:
        raise ValueError(f"slot '{name}' must not be null")
    if kind is int:
        if isinstance(value, bool):
            raise ValueError(f"slot '{name}' must be a number")
        if isinstance(value, str):
            digits = value.replace(",", "").strip()
            if not digits.lstrip("-").isdigit():
                raise ValueError(f"slot '{name}' must be a whole number, got '{value}'")
            value = int(digits)
        value = int(value)
        if value < 0 or value > 1_000_000:
            raise ValueError(f"slot '{name}' out of range")
        return value
    if kind is bool:
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"true", "yes", "y", "1"}:
                return True
            if v in {"false", "no", "n", "0"}:
                return False
            raise ValueError(f"slot '{name}' must be yes/no")
        return bool(value)
    if kind is list:
        items = value if isinstance(value, list) else [value]
        out: list[str] = []
        for item in items:
            key = str(item).strip().lower()
            out.append(PainPoint(key).value if key in PainPoint._value2member_map_ else PainPoint.OTHER.value)
        return sorted(set(out))
    # str
    text = str(value).strip()
    if not text:
        raise ValueError(f"slot '{name}' must not be empty")
    if name == "decision_role":
        key = text.lower().replace(" ", "_").replace("-", "_")
        if key not in DecisionRole._value2member_map_:
            raise ValueError(f"decision_role must be one of {[r.value for r in DecisionRole]}")
        return key
    return text[:120]


class Band(StrEnum):
    HOT = "hot"  # transfer to a live specialist if one is available
    QUALIFIED = "qualified"  # book a meeting
    NURTURE = "nurture"
    DISQUALIFIED = "disqualified"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class ScoreResult:
    score: int
    band: Band
    reason_codes: list[str]
    missing: list[str]
    verify: list[str]
    rubric_version: str = RUBRIC_VERSION
    recommended_action: str = ""


@dataclass(frozen=True)
class Valuation:
    lines: int
    arpu: float
    term_months: int
    monthly_value: float
    annual_value: float
    contract_value: float


@dataclass
class PriceBook:
    tier1: float
    tier2: float
    tier3: float
    term_months: int
    min_lines: int = 100

    def arpu_for(self, lines: int) -> float:
        if lines >= 2000:
            return self.tier3
        if lines >= 500:
            return self.tier2
        return self.tier1


def score(slots: dict[str, dict[str, Any]], min_lines: int = 100) -> ScoreResult:
    """Score a lead from its slots ({name: {"value":..,"confidence":..}})."""
    values = {k: v["value"] for k, v in slots.items()}
    verify = sorted(k for k, v in slots.items() if float(v.get("confidence", 1.0)) < LOW_CONFIDENCE)
    missing = [k for k in REQUIRED_FOR_SCORE if k not in values]
    reasons: list[str] = []
    points = 0

    lines = values.get("mobile_lines")
    if lines is not None:
        if lines < min_lines:
            return ScoreResult(
                score=0,
                band=Band.DISQUALIFIED,
                reason_codes=[f"LINES_BELOW_{min_lines}"],
                missing=missing,
                verify=verify,
                recommended_action="Thank the caller; route to self-serve business plans.",
            )
        if lines >= 500:
            points += 30
            reasons.append("LINES_500PLUS")
        else:
            points += 20
            reasons.append("LINES_100_499")

    months = values.get("contract_months_remaining")
    if months is not None:
        if months <= 6:
            points += 25
            reasons.append("RENEWAL_LE_6M")
        elif months <= 12:
            points += 15
            reasons.append("RENEWAL_7_12M")
        else:
            points += 5
            reasons.append("RENEWAL_GT_12M")

    role = values.get("decision_role")
    if role == DecisionRole.DECISION_MAKER:
        points += 20
        reasons.append("DECISION_MAKER")
    elif role == DecisionRole.INFLUENCER:
        points += 10
        reasons.append("INFLUENCER")

    pains = values.get("pain_points") or []
    if pains:
        points += 15
        reasons.extend(f"PAIN_{p.upper()}" for p in pains)

    timeline = values.get("timeline_months")
    if values.get("budget_confirmed") is True or (timeline is not None and timeline <= 6):
        points += 10
        reasons.append("TIMELINE_OR_BUDGET")

    points = max(0, min(100, points))
    if missing:
        band = Band.INCOMPLETE
        action = "Keep qualifying: ask about " + ", ".join(SLOT_SCHEMA[m][1].lower() for m in missing) + "."
    elif points >= 80:
        band = Band.HOT
        action = "Offer a live transfer to the enterprise specialist, otherwise book the earliest meeting."
    elif points >= 60:
        band = Band.QUALIFIED
        action = "Book a discovery meeting with the enterprise specialist."
    elif points >= 35:
        band = Band.NURTURE
        action = "Offer a callback near contract renewal; add to nurture."
    else:
        band = Band.DISQUALIFIED
        action = "Close politely; no follow-up needed."
    return ScoreResult(points, band, reasons, missing, verify, RUBRIC_VERSION, action)


def value_opportunity(slots: dict[str, dict[str, Any]], book: PriceBook) -> Valuation | None:
    lines = slots.get("mobile_lines", {}).get("value")
    if not isinstance(lines, int) or lines < book.min_lines:
        return None
    arpu = book.arpu_for(lines)
    monthly = round(lines * arpu, 2)
    return Valuation(
        lines=lines,
        arpu=arpu,
        term_months=book.term_months,
        monthly_value=monthly,
        annual_value=round(monthly * 12, 2),
        contract_value=round(monthly * book.term_months, 2),
    )


@dataclass
class Briefing:
    """What the salesperson receives."""

    lead_name: str
    company: str
    score: ScoreResult
    valuation: Valuation | None
    facts: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    next_action: str = ""
