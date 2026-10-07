"""Guardrails for the escalation agent.

Input  - caller words are untrusted: control chars stripped, length capped, PII
         redacted, prompt-injection attempts neutralised before they reach a model.
Output - the plan must parse into the schema, choose only policy-allowed
         actions, cite only numbers/carriers present in the grounded facts, and
         contain no pricing promises or personal data.
Any violation is recorded and the deterministic plan is used instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

MAX_EVIDENCE_CHARS = 200

INJECTION_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"ignore (all |any |the )?(previous|prior|above) (instructions|rules|prompts?)",
        r"disregard (all |any |the )?(previous|prior|above)",
        r"\bsystem prompt\b",
        r"\byou are now\b",
        r"\bact as (an?|the)\b",
        r"\bnew instructions?\b",
        r"\bdeveloper mode\b",
        r"</?(system|assistant|user)>",
        r"\bjailbreak\b",
    )
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

PRICING = re.compile(
    r"(\d+\s*%\s*(off|discount))|\bdiscount|\bfree\b|\bguarantee|\bper line\b|\bper month\b|"
    r"\bprice (match|lock)|\bwaive|\bcredit of\b|\bcheaper than\b",
    re.I,
)
KNOWN_CARRIERS = ("verizon", "at&t", "att", "t-mobile", "tmobile", "sprint", "us cellular", "comcast",
                  "xfinity", "spectrum", "cricket", "boost", "visible", "mint")
NUMBER = re.compile(r"\$?\d[\d,]*(?:\.\d+)?(?:[kKmM](?![A-Za-z]))?")


@dataclass
class Finding:
    stage: Literal["input", "output", "action", "budget"]
    rule: str
    detail: str = ""


@dataclass
class SanitizedText:
    text: str
    findings: list[Finding] = field(default_factory=list)


def sanitize_untrusted(text: str | None) -> SanitizedText:
    """Clean one piece of caller-provided text before it is placed in a prompt."""
    if not text:
        return SanitizedText("")
    findings: list[Finding] = []
    t = CONTROL.sub(" ", text)
    t = " ".join(t.split())
    if len(t) > MAX_EVIDENCE_CHARS:
        t = t[:MAX_EVIDENCE_CHARS] + "..."
        findings.append(Finding("input", "TRUNCATED"))
    for pat in INJECTION_PATTERNS:
        if pat.search(t):
            findings.append(Finding("input", "PROMPT_INJECTION", pat.pattern[:60]))
            return SanitizedText("[removed: possible prompt injection]", findings)
    redacted = EMAIL.sub("[email]", t)
    redacted = SSN.sub("[id]", redacted)
    redacted = PHONE.sub("[phone]", redacted)
    if redacted != t:
        findings.append(Finding("input", "PII_REDACTED"))
    return SanitizedText(redacted, findings)


Channel = Literal["warm_transfer", "meeting", "callback"]


class EscalationPlan(BaseModel):
    """The only shape the escalation agent may produce."""

    priority: Literal["P1", "P2", "P3"]
    recommended_channel: Channel
    headline: str = Field(min_length=8, max_length=140)
    talking_points: list[str] = Field(min_length=2, max_length=4)
    risks: list[str] = Field(default_factory=list, max_length=3)
    next_step: str = Field(min_length=8, max_length=220)

    @field_validator("talking_points", "risks")
    @classmethod
    def _item_len(cls, v: list[str]) -> list[str]:
        out = [" ".join(x.split()) for x in v]
        if any(len(x) < 5 or len(x) > 220 for x in out):
            raise ValueError("each item must be 5-220 characters")
        return out

    def text_blob(self) -> str:
        return " ".join([self.headline, *self.talking_points, *self.risks, self.next_step])


def _norm_number(raw: str) -> float | None:
    s = raw.strip().lower().replace("$", "").replace(",", "").replace(" ", "")
    mult = 1.0
    if s.endswith("k"):
        mult, s = 1_000.0, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000.0, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def grounded_numbers(values: list[float]) -> set[float]:
    """Numbers the plan may cite: the facts, plus rounded forms of money values."""
    allowed: set[float] = set()
    for v in values:
        allowed.add(float(v))
        if v >= 10_000:
            allowed.add(round(v / 1_000) * 1_000.0)  # "$734K"
            allowed.add(round(v / 100_000) * 100_000.0)  # "$0.7M" style rounding
            allowed.add(round(v / 1_000_000, 1) * 1_000_000.0)
    return allowed


def validate_plan(
    raw_json: str,
    *,
    allowed_channels: set[str],
    fact_numbers: list[float],
    fact_carrier: str | None,
) -> tuple[EscalationPlan | None, list[Finding]]:
    findings: list[Finding] = []
    try:
        plan = EscalationPlan.model_validate_json(_extract_json(raw_json))
    except (ValidationError, ValueError) as exc:
        return None, [Finding("output", "SCHEMA_INVALID", str(exc)[:200])]

    if plan.recommended_channel not in allowed_channels:
        findings.append(Finding("action", "CHANNEL_NOT_ALLOWED", plan.recommended_channel))

    blob = plan.text_blob()
    allowed = grounded_numbers(fact_numbers)
    for m in NUMBER.finditer(blob):
        n = _norm_number(m.group(0))
        if n is None or n in (0, 1, 2, 3):  # list ordinals / "two options" style are harmless
            continue
        if not any(abs(n - a) <= max(0.5, a * 0.005) for a in allowed):
            findings.append(Finding("output", "UNGROUNDED_NUMBER", m.group(0).strip()))
            break
    low = blob.lower()
    fact_c = _alpha(fact_carrier or "")
    for carrier in KNOWN_CARRIERS:
        if re.search(rf"(?<![a-z]){re.escape(carrier)}(?![a-z])", low):
            c = _alpha(carrier)
            if not fact_c or (c not in fact_c and fact_c not in c):
                findings.append(Finding("output", "UNGROUNDED_CARRIER", carrier))
                break
    if PRICING.search(blob):
        findings.append(Finding("output", "PRICING_CLAIM", PRICING.search(blob).group(0)))  # type: ignore[union-attr]
    if EMAIL.search(blob) or PHONE.search(blob) or SSN.search(blob):
        findings.append(Finding("output", "PII_IN_OUTPUT"))
    return (None if findings else plan), findings


def _alpha(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _extract_json(text: str) -> str:
    """Accept a bare JSON object or one wrapped in a ```json fence."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return t[start : end + 1]
