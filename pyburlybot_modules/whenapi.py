"""Public When schedule queries and current availability (no API key needed)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from json import dumps
from math import isfinite
from re import fullmatch
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from util.http import HTTPError, http
from util.types import BotLike

OPTIONS = {
    "site_url": (
        str,
        "When frontend origin used for schedule links.",
        "https://when.nyanya.org",
    ),
    "api_url": (
        str,
        "When Convex deployment origin.",
        "https://when-convex.nyanya.org",
    ),
}


class WhenAPIError(HTTPError):
    """When could not supply a valid schedule snapshot."""


@dataclass(frozen=True, slots=True)
class ScheduleLink:
    schedule_id: str
    url: str


# A full weekly cycle plus a day covers ordinary recurring availability runs.
# Continuous schedules are reported as a lower bound instead of inventing an end.
DURATION_SCAN_DAYS = 8
MAX_LOOKAHEAD_HOURS = 168
Interval = tuple[float, float]


@dataclass(frozen=True, slots=True)
class AvailabilityWindow:
    name: str
    starts_in_hours: float
    duration_hours: float
    duration_is_lower_bound: bool = False


@dataclass(frozen=True, slots=True)
class Availability:
    available: tuple[AvailabilityWindow, ...]


def parse_link(bot: BotLike, url: str) -> ScheduleLink:
    """Only accept schedule links on the configured When origin."""
    origin = bot.getOption("site_url", module="whenapi").rstrip("/")
    parsed = urlsplit(url)
    expected = urlsplit(origin)
    match = fullmatch(r"/schedule/([a-z0-9]{32})/?", parsed.path)
    if (
        any(char.isspace() or ord(char) < 32 for char in url)
        or parsed.scheme != expected.scheme
        or parsed.netloc.lower() != expected.netloc.lower()
        or parsed.username is not None
        or not match
    ):
        raise ValueError("Use a schedule URL from %s/schedule/<ID>." % origin)
    schedule_id = match[1]
    return ScheduleLink(schedule_id, "%s/schedule/%s" % (origin, schedule_id))


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Expected a nonempty string")
    return value


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Expected a list of objects")
    return value


def _merge(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _subtract(intervals: list[Interval], exclusions: list[Interval]) -> list[Interval]:
    output: list[Interval] = []
    excluded = _merge(exclusions)
    index = 0
    for interval_start, end in _merge(intervals):
        start = interval_start
        while index < len(excluded) and excluded[index][1] <= start:
            index += 1
        while index < len(excluded) and excluded[index][0] < end:
            left, right = excluded[index]
            if left > start:
                output.append((start, left))
            start = max(start, right)
            if start >= end:
                break
            index += 1
        if start < end:
            output.append((start, end))
    return output


def _slot_intervals(
    row: dict[str, Any], start: datetime, end: datetime, recurring: bool, timezone: str
) -> list[Interval]:
    zone = ZoneInfo(timezone)
    slot = _text(row["timeSlot"])
    if not fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", slot):
        raise ValueError("Invalid half-hour slot")
    day = _text(row["dayKey"])
    exception = row.get("isException", False)
    if not isinstance(exception, bool):
        raise ValueError("Invalid exception flag")
    if recurring and not exception:
        if day not in {"0", "1", "2", "3", "4", "5", "6"}:
            raise ValueError("Invalid weekday")
        first = start.astimezone(zone).date() - timedelta(days=1)
        first += timedelta(days=(int(day) - first.isoweekday() % 7) % 7)
        last = end.astimezone(zone).date()
        dates = [
            first + timedelta(days=offset)
            for offset in range(0, (last - first).days + 1, 7)
        ]
    else:
        dates = [date.fromisoformat(_text(row["exceptionDate"]) if exception else day)]
    intervals: list[Interval] = []
    for candidate in dates:
        wall = datetime.combine(candidate, time.fromisoformat(slot))
        for fold in (0, 1):
            occurrence = wall.replace(tzinfo=zone, fold=fold)
            # Ignore nonexistent spring-forward times; include both fall-back
            # occurrences. UTC intervals measure elapsed, not wall-clock, hours.
            if occurrence.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != wall:
                continue
            left = max(start.timestamp(), occurrence.timestamp())
            right = min(end.timestamp(), occurrence.timestamp() + 30 * 60)
            if left < right:
                intervals.append((left, right))
    return _merge(intervals)


def _availability(
    schedule: dict[str, Any], now: datetime, lookahead_hours: float
) -> Availability:
    schedule_type = schedule["type"]
    if schedule_type not in {"recurring", "one-off"}:
        raise ValueError("Invalid schedule type")
    recurring = schedule_type == "recurring"
    timezone = _text(schedule["creatorTimezone"])
    zone = ZoneInfo(timezone)
    now = now.astimezone(UTC)
    cutoff = now + timedelta(hours=lookahead_hours)
    horizon = cutoff + timedelta(days=DURATION_SCAN_DAYS)
    start, end = now, horizon
    first = schedule.get("recurringStartDate" if recurring else "dateRangeStart")
    last = None if recurring else schedule.get("dateRangeEnd")
    if first:
        start = max(
            start,
            datetime.combine(date.fromisoformat(_text(first)), time(), zone).astimezone(
                UTC
            ),
        )
    if last:
        end = min(
            end,
            datetime.combine(
                date.fromisoformat(_text(last)) + timedelta(days=1), time(), zone
            ).astimezone(UTC),
        )
    if start >= end or start > cutoff:
        return Availability(())
    disallowed = [
        interval
        for row in _rows(schedule.get("disallowedSlots", []))
        for interval in _slot_intervals(row, start, end, recurring, timezone)
    ]
    blocked = schedule["blockedProfileIds"]
    if not isinstance(blocked, list) or any(
        not isinstance(item, str) for item in blocked
    ):
        raise ValueError("Invalid blocked profiles")
    profiles = {
        _text(row["_id"]): _text(row["displayName"])
        for row in _rows(schedule["profiles"])
    }
    # Per participant: base can-do, base negative, exception can-do, exception negative.
    selections: dict[
        str, tuple[list[Interval], list[Interval], list[Interval], list[Interval]]
    ] = {}
    for row in _rows(schedule["selections"]):
        profile_id = _text(row["profileId"])
        state = row["state"]
        if state not in {"can-do", "cant-do", "maybe"}:
            raise ValueError("Invalid selection state")
        intervals = _slot_intervals(row, start, end, recurring, _text(row["timezone"]))
        buckets = selections.setdefault(profile_id, ([], [], [], []))
        bucket = (2 if recurring and row.get("isException") else 0) + (
            state != "can-do"
        )
        buckets[bucket].extend(intervals)
    windows: list[AvailabilityWindow] = []
    for profile_id, (
        base_yes,
        base_no,
        exception_yes,
        exception_no,
    ) in selections.items():
        if profile_id not in profiles or profile_id in blocked:
            continue
        base = _subtract(base_yes, base_no + exception_yes + exception_no)
        exceptions = _subtract(exception_yes, exception_no)
        available = _subtract(base + exceptions, disallowed)
        # Show each participant's current run, or their next run in the lookahead.
        if available and available[0][0] <= cutoff.timestamp():
            left, right = available[0]
            windows.append(
                AvailabilityWindow(
                    profiles[profile_id],
                    (left - now.timestamp()) / 3600,
                    (right - left) / 3600,
                    right == horizon.timestamp(),
                )
            )
    return Availability(
        tuple(
            sorted(
                windows,
                key=lambda window: (window.starts_in_hours, window.name.casefold()),
            )
        )
    )


def get_availability(
    bot: BotLike,
    link: ScheduleLink,
    *,
    now: datetime | None = None,
    lookahead_hours: float = 1.7,
) -> Availability | None:
    """Return current/next availability runs, or None for a missing schedule.

    schedules:get already merges linked saved availability and calendar rows.
    The helper resolves per-profile date exceptions over recurring base rows.
    """
    if not isfinite(lookahead_hours) or not 0 <= lookahead_hours <= MAX_LOOKAHEAD_HOURS:
        raise ValueError("lookahead_hours must be between 0 and 168.")
    if now is None:
        now = datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    endpoint = bot.getOption("api_url", module="whenapi").rstrip("/") + "/api/query"
    payload = http.request(
        "POST",
        endpoint,
        headers={"Content-Type": "application/json"},
        body=dumps(
            {
                "path": "schedules:get",
                "args": {"scheduleId": link.schedule_id},
                "format": "json",
            }
        ).encode("utf-8"),
    ).json()
    try:
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError("Query did not succeed")
        schedule = payload["value"]
        if schedule is None:
            return None
        if not isinstance(schedule, dict) or schedule.get("_id") != link.schedule_id:
            raise ValueError("Unexpected schedule")
        return _availability(schedule, now, lookahead_hours)
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise WhenAPIError("When returned an invalid schedule response.") from error


def init(bot: BotLike) -> bool:
    return True
