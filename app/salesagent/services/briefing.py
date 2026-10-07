"""Assemble the salesperson briefing from the system of record."""

from __future__ import annotations

import uuid
from typing import Any

from ..container import Container
from ..domain.scheduling import spoken
from .errors import NotFound


async def build(c: Container, attempt_id: uuid.UUID) -> dict[str, Any]:
    async with c.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT a.*, l.first_name, l.last_name, l.company, l.phone_e164, l.email"
            " FROM call_attempt a JOIN lead l ON l.id=a.lead_id WHERE a.id=%s",
            (attempt_id,),
        )
        a = await cur.fetchone()
        if not a:
            raise NotFound("ATTEMPT_NOT_FOUND", "call attempt not found")
        cur = await conn.execute("SELECT name, value FROM qualification_slot WHERE attempt_id=%s", (attempt_id,))
        facts = {r["name"]: r["value"] for r in await cur.fetchall()}
        cur = await conn.execute("SELECT * FROM score_result WHERE attempt_id=%s", (attempt_id,))
        sc = await cur.fetchone() or {}
        cur = await conn.execute("SELECT * FROM meeting WHERE attempt_id=%s", (attempt_id,))
        mt = await cur.fetchone()
        cur = await conn.execute("SELECT plan, plan_source FROM campaign_lead WHERE last_attempt_id=%s"
                                 " AND plan IS NOT NULL LIMIT 1", (attempt_id,))
        esc = await cur.fetchone()

    valuation = sc.get("valuation") or {}
    contact = f"{a['first_name']} {a['last_name']}".strip()
    rep_name = None
    if mt:
        rep_name = next((r.display_name for r in c.settings.reps if r.upn == mt["rep_upn"]), mt["rep_upn"])
    return {
        "attempt_id": str(attempt_id),
        "channel": a["channel"],
        "contact_name": contact,
        "company": a["company"],
        "phone": a["phone_e164"],
        "email": a["email"],
        "outcome": a["outcome"] or ("meeting_booked" if mt else a["status"]),
        "summary": a["summary"] or "",
        "next_action": a["next_action"] or sc.get("recommended_action") or "",
        "score": sc.get("score"),
        "band": sc.get("band"),
        "reason_codes": sc.get("reason_codes") or [],
        "verify": sc.get("verify") or [],
        "mobile_lines": facts.get("mobile_lines"),
        "current_carrier": facts.get("current_carrier"),
        "contract_months_remaining": facts.get("contract_months_remaining"),
        "decision_role": facts.get("decision_role"),
        "pain_points": facts.get("pain_points") or [],
        "timeline_months": facts.get("timeline_months"),
        "contract_value": valuation.get("contract_value"),
        "annual_value": valuation.get("annual_value"),
        "meeting_start": mt["starts_at"] if mt else None,
        "meeting_when": spoken(mt["starts_at"], c.settings.tz) if mt else None,
        "join_url": mt["join_url"] if mt else None,
        "rep_upn": mt["rep_upn"] if mt else None,
        "rep_name": rep_name,
        "briefing_url": f"{c.settings.public_cockpit_url}/#/attempt/{attempt_id}",
        "plan": esc["plan"] if esc else None,
        "plan_source": esc["plan_source"] if esc else None,
    }


def rep_card_payload(b: dict[str, Any], kind: str) -> dict[str, Any]:
    """Flat JSON for the Power Automate flow (easy to bind in the adaptive card)."""
    money = f"${b['contract_value']:,.0f}" if b.get("contract_value") else "n/a"
    m = b.get("contract_months_remaining")
    renewal = "unknown" if m is None else "now" if m == 0 else f"{m} month{'' if m == 1 else 's'}"
    plan = b.get("plan") or {}
    titles = {"transfer": "Hot lead - live transfer incoming",
              "escalation": f"{plan.get('priority', 'P2')} escalation from campaign"}
    summary = b.get("summary") or ""
    next_action = b.get("next_action") or ""
    if plan:
        summary = plan.get("headline", "") + ". " + " ".join(f"- {t}" for t in plan.get("talking_points", []))
        next_action = plan.get("next_step") or next_action
    return {
        "kind": kind,  # "final" | "transfer" | "escalation"
        "title": titles.get(kind, "New AI-qualified opportunity"),
        "rep_upn": b.get("rep_upn") or "",
        "contact": b["contact_name"],
        "company": b["company"],
        "phone": b["phone"],
        "score": b.get("score") if b.get("score") is not None else 0,
        "band": (b.get("band") or "incomplete").upper(),
        "lines": b.get("mobile_lines") if b.get("mobile_lines") is not None else 0,
        "carrier": b.get("current_carrier") or "unknown",
        "renewal": renewal,
        "role": (b.get("decision_role") or "unknown").replace("_", " "),
        "pain_points": ", ".join(b.get("pain_points") or []) or "none stated",
        "estimated_value": money,
        "meeting": b.get("meeting_when") or "not booked",
        "join_url": b.get("join_url") or "",
        "summary": summary,
        "next_action": next_action,
        "reason_codes": ", ".join(b.get("reason_codes") or []),
        "verify": ", ".join(b.get("verify") or []),
        "briefing_url": b["briefing_url"],
    }


def should_notify(b: dict[str, Any], kind: str) -> bool:
    return kind in ("transfer", "escalation") or b.get("band") in ("hot", "qualified") or b.get("meeting_start") is not None
