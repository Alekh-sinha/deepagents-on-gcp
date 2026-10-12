"""Cron schedules: validation and "when next". Five fields (minute hour day-of-month month day-of-week), evaluated in the schedule's own time zone."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from pinboard.engine.errors import BadRequest

LOOKAHEAD = 40  # how many upcoming firings are examined to find the shortest gap


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise BadRequest(f"unknown time zone {name!r}; use a name like Europe/Berlin or America/New_York") from exc


def next_after(cron: str, tz_name: str, after: datetime) -> datetime:
    """The next firing strictly after ``after`` (any time zone), as UTC."""
    tz = zone(tz_name)
    local = after.astimezone(tz)
    return croniter(cron, local).get_next(datetime).astimezone(timezone.utc)


def validate(cron: str, tz_name: str, min_interval_minutes: int, now: datetime) -> datetime:
    """Raises BadRequest for an unusable schedule; returns the first firing time (UTC)."""
    expr = " ".join((cron or "").split())
    if len(expr.split(" ")) != 5 or not croniter.is_valid(expr):
        raise BadRequest("the schedule must be a five-field cron expression, e.g. '0 9 * * 1-5' (9:00 on weekdays)")
    tz = zone(tz_name)
    it = croniter(expr, now.astimezone(tz))
    times = [it.get_next(datetime) for _ in range(LOOKAHEAD)]
    gap = min((b - a for a, b in zip(times, times[1:])), default=timedelta(days=1))
    if gap < timedelta(minutes=min_interval_minutes):
        raise BadRequest(f"that schedule can fire every {int(gap.total_seconds() // 60)} minute(s); the minimum interval is {min_interval_minutes} minutes")
    return times[0].astimezone(timezone.utc)
