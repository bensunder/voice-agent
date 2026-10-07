"""Pure slot-finding logic (no I/O), so availability rules are unit-testable."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

Interval = tuple[datetime, datetime]


@dataclass(frozen=True)
class Candidate:
    rep_upn: str
    rep_name: str
    start: datetime  # UTC
    end: datetime  # UTC

    @property
    def slot_id(self) -> str:
        return f"{self.rep_upn}|{self.start.strftime('%Y-%m-%dT%H:%MZ')}"


def parse_slot_id(slot_id: str) -> tuple[str, datetime]:
    rep, _, stamp = slot_id.partition("|")
    if not rep or not stamp:
        raise ValueError("invalid slot_id")
    start = datetime.strptime(stamp, "%Y-%m-%dT%H:%MZ").replace(tzinfo=ZoneInfo("UTC"))
    return rep, start


def _overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def find_candidates(
    *,
    now_utc: datetime,
    tz: ZoneInfo,
    reps: list[tuple[str, str]],  # (upn, display name), already in preferred order
    busy: dict[str, list[Interval]],
    held: set[tuple[str, datetime]],
    days_ahead: int,
    work_start_hour: int,
    work_end_hour: int,
    minutes: int,
    min_lead_minutes: int = 60,
    want: int = 2,
) -> list[Candidate]:
    """Earliest free slot first, then the earliest free slot on a *different day*
    so the lead gets a genuine choice. Weekends are skipped."""
    step = timedelta(minutes=30)
    length = timedelta(minutes=minutes)
    earliest = now_utc + timedelta(minutes=min_lead_minutes)
    local_now = now_utc.astimezone(tz)
    found: list[Candidate] = []
    used_days: set[str] = set()

    for day_offset in range(0, days_ahead + 1):
        day = (local_now + timedelta(days=day_offset)).date()
        if day.weekday() >= 5:
            continue
        t = datetime(day.year, day.month, day.day, work_start_hour, 0, tzinfo=tz)
        day_end = datetime(day.year, day.month, day.day, work_end_hour, 0, tzinfo=tz)
        while t + length <= day_end:
            start_utc = t.astimezone(ZoneInfo("UTC"))
            if start_utc >= earliest and day.isoformat() not in used_days:
                for upn, name in reps:
                    window = (start_utc, start_utc + length)
                    if (upn, start_utc) in held:
                        continue
                    if any(_overlaps(window, b) for b in busy.get(upn, [])):
                        continue
                    found.append(Candidate(upn, name, start_utc, start_utc + length))
                    used_days.add(day.isoformat())
                    break
                if len(found) >= want:
                    return found
            t += step
    return found


def spoken(dt_utc: datetime, tz: ZoneInfo) -> str:
    """Human phrasing the voice agent can read aloud, e.g. 'Thursday, October 8 at 10:30 AM MDT'."""
    local = dt_utc.astimezone(tz)
    hour = local.strftime("%I").lstrip("0")
    return f"{local.strftime('%A, %B')} {local.day} at {hour}:{local.strftime('%M %p %Z')}"
