from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from salesagent.domain import compliance as gate
from salesagent.domain.qualification import Band, PriceBook, SlotUpdate, score, value_opportunity
from salesagent.domain.scheduling import find_candidates, parse_slot_id, spoken
from salesagent.security import TokenError, mint_call_token, verify_call_token

UTC = timezone.utc
DEN = ZoneInfo("America/Denver")


def S(**vals):
    return {k: {"value": v, "confidence": 0.9} for k, v in vals.items()}


# ---------------------------------------------------------------- scoring
@pytest.mark.parametrize(
    "slots, expected_score, band",
    [
        (S(mobile_lines=600, contract_months_remaining=4, decision_role="decision_maker",
           pain_points=["cost"], timeline_months=3), 100, Band.HOT),
        (S(mobile_lines=600, contract_months_remaining=4, decision_role="influencer"), 65, Band.QUALIFIED),
        (S(mobile_lines=150, contract_months_remaining=10, decision_role="influencer"), 45, Band.NURTURE),
        (S(mobile_lines=150, contract_months_remaining=24, decision_role="neither"), 25, Band.DISQUALIFIED),
        (S(mobile_lines=40, contract_months_remaining=1, decision_role="decision_maker"), 0, Band.DISQUALIFIED),
        (S(mobile_lines=600), 30, Band.INCOMPLETE),
    ],
)
def test_score_bands(slots, expected_score, band):
    r = score(slots)
    assert r.score == expected_score
    assert r.band is band
    assert r.rubric_version == "wireless-v1"


def test_low_confidence_is_flagged_not_dropped():
    slots = S(mobile_lines=600, contract_months_remaining=4, decision_role="decision_maker")
    slots["mobile_lines"]["confidence"] = 0.4
    r = score(slots)
    assert r.verify == ["mobile_lines"] and r.score == 75


@pytest.mark.parametrize(
    "raw, ok",
    [
        ({"mobile_lines": {"value": "1,200"}}, 1200),
        ({"decision_role": {"value": "Decision Maker"}}, "decision_maker"),
        ({"budget_confirmed": {"value": "yes"}}, True),
        ({"pain_points": {"value": ["Cost", "weird"]}}, ["cost", "other"]),
    ],
)
def test_slot_coercion(raw, ok):
    upd = SlotUpdate(slots=raw)
    assert next(iter(upd.slots.values())).value == ok


@pytest.mark.parametrize(
    "raw",
    [
        {"mobile_lines": {"value": "about six hundred"}},
        {"unknown_slot": {"value": 1}},
        {"decision_role": {"value": "ceo"}},
        {"mobile_lines": {"value": True}},
    ],
)
def test_slot_rejects_bad_values(raw):
    with pytest.raises(ValueError):
        SlotUpdate(slots=raw)


def test_valuation_tiers():
    book = PriceBook(tier1=38, tier2=34, tier3=30, term_months=36)
    v = value_opportunity(S(mobile_lines=600), book)
    assert v.arpu == 34 and v.monthly_value == 20400 and v.contract_value == 734400
    assert value_opportunity(S(mobile_lines=50), book) is None
    assert value_opportunity(S(mobile_lines=2500), book).arpu == 30


# ---------------------------------------------------------------- compliance
def g(**kw):
    base = dict(phone_e164="+18015550123", has_consent=True, opted_out=False, attempts_today=0,
                lead_timezone=DEN, now_utc=datetime(2026, 10, 7, 17, 0, tzinfo=UTC),  # 11:00 MDT
                demo_mode=True, allowlist=frozenset({"+18015550123"}), window_start_hour=8, window_end_hour=21)
    base.update(kw)
    return gate.evaluate(gate.GateInput(**base))


@pytest.mark.parametrize(
    "kw, verdict, reason",
    [
        ({}, gate.Verdict.ALLOW, "CLEARED"),
        ({"opted_out": True}, gate.Verdict.DENY, "OPTED_OUT"),
        ({"has_consent": False}, gate.Verdict.DENY, "NO_CONSENT"),
        ({"phone_e164": "+18015550199"}, gate.Verdict.DENY, "DEMO_NOT_ALLOWLISTED"),
        ({"demo_mode": False, "phone_e164": "+18015550199"}, gate.Verdict.ALLOW, "CLEARED"),
        ({"attempts_today": 3}, gate.Verdict.DENY, "ATTEMPT_CAP"),
        ({"now_utc": datetime(2026, 10, 7, 13, 0, tzinfo=UTC)}, gate.Verdict.DEFER, "OUTSIDE_CALLING_WINDOW"),
        ({"now_utc": datetime(2026, 10, 8, 4, 0, tzinfo=UTC)}, gate.Verdict.DEFER, "OUTSIDE_CALLING_WINDOW"),
        ({"phone_e164": "8015550123"}, gate.Verdict.DENY, "INVALID_PHONE"),
    ],
)
def test_gate(kw, verdict, reason):
    d = g(**kw)
    assert (d.verdict, d.reason) == (verdict, reason)


def test_gate_defer_gives_next_window_start():
    d = g(now_utc=datetime(2026, 10, 8, 4, 0, tzinfo=UTC))  # 22:00 MDT
    assert d.retry_at_utc == datetime(2026, 10, 8, 14, 0, tzinfo=UTC)  # 08:00 MDT next day


def test_gate_window_until_midnight():
    assert g(window_end_hour=24, now_utc=datetime(2026, 10, 8, 5, 30, tzinfo=UTC)).allowed  # 23:30 MDT


@pytest.mark.parametrize("raw, out", [("(801) 555-0123", "+18015550123"), ("1-801-555-0123", "+18015550123")])
def test_normalize_phone(raw, out):
    assert gate.normalize_us_phone(raw) == out


@pytest.mark.parametrize("raw", ["555-0123", "+44 20 7946 0958", "(011) 555-0123"])
def test_normalize_phone_rejects(raw):
    with pytest.raises(ValueError):
        gate.normalize_us_phone(raw)


# ---------------------------------------------------------------- call tokens
def test_token_roundtrip_and_tamper():
    lead, att = uuid.uuid4(), uuid.uuid4()
    t = mint_call_token("s" * 40, lead, att, 60)
    claims = verify_call_token("s" * 40, t)
    assert (claims.lead_id, claims.attempt_id) == (lead, att)
    with pytest.raises(TokenError):
        verify_call_token("x" * 40, t)
    v, body, sig = t.split(".")
    with pytest.raises(TokenError):
        verify_call_token("s" * 40, f"{v}.{body}x.{sig}")
    with pytest.raises(TokenError):
        verify_call_token("s" * 40, t, now=time.time() + 120)
    with pytest.raises(TokenError):
        verify_call_token("s" * 40, "garbage")


# ---------------------------------------------------------------- scheduling
REPS = [("a@x.com", "Ana"), ("b@x.com", "Ben")]


def cands(now, busy=None, held=None, **kw):
    return find_candidates(now_utc=now, tz=DEN, reps=REPS, busy=busy or {}, held=held or set(),
                           days_ahead=5, work_start_hour=9, work_end_hour=17, minutes=30, **kw)


def test_offers_two_slots_on_different_days():
    now = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)  # Wed 10:00 MDT
    c = cands(now)
    assert len(c) == 2
    assert c[0].start == datetime(2026, 10, 7, 17, 0, tzinfo=UTC)  # 11:00 (60 min lead)
    assert c[1].start.astimezone(DEN).date() != c[0].start.astimezone(DEN).date()


def test_skips_weekend():
    now = datetime(2026, 10, 10, 16, 0, tzinfo=UTC)  # Saturday
    c = cands(now)
    assert all(x.start.astimezone(DEN).weekday() < 5 for x in c)


def test_busy_and_held_fall_through_to_next_rep():
    now = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    first = datetime(2026, 10, 7, 17, 0, tzinfo=UTC)
    c = cands(now, busy={"a@x.com": [(first, first + timedelta(hours=1))]})
    assert c[0].rep_upn == "b@x.com" and c[0].start == first
    c = cands(now, busy={"a@x.com": [(first, first + timedelta(hours=1))]}, held={("b@x.com", first)})
    assert c[0].start == first + timedelta(minutes=30) and c[0].rep_upn == "b@x.com"


def test_slot_id_roundtrip_and_spoken():
    c = cands(datetime(2026, 10, 7, 16, 0, tzinfo=UTC))[0]
    assert parse_slot_id(c.slot_id) == (c.rep_upn, c.start)
    assert spoken(c.start, DEN) == "Wednesday, October 7 at 11:00 AM MDT"
