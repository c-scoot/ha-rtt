"""Find the next weekly occurrence of an advertised station call."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .const import UK_TIME_ZONE
from .models import (
    IndividualTemporalData,
    LocationCallType,
    LocationLineUp,
    LocationPair,
    ModeType,
    NetworkRailLocationLineUp,
    NetworkRailServiceDetail,
    ServiceDetail,
)
from .tracking import RouteFingerprint, TrackedCall

BoardRow = LocationLineUp | NetworkRailLocationLineUp
SEARCH_LEAD = timedelta(hours=24)
SEARCH_GRACE = timedelta(hours=1)
RETRY_INTERVAL = timedelta(hours=6)
_WEEK = timedelta(weeks=1)


def next_week_target(previous: datetime, now: datetime) -> datetime:
    """Advance at least one London calendar week, skipping expired search windows."""
    if previous.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("Weekly scheduling requires offset-aware datetimes")
    local = previous.astimezone(UK_TIME_ZONE).replace(tzinfo=None, fold=0)
    now = now.astimezone(UTC)
    oldest_day = (now - SEARCH_GRACE).astimezone(UK_TIME_ZONE).date()
    elapsed_weeks = (oldest_day - local.date()).days // 7
    candidate = local + max(1, elapsed_weeks) * _WEEK
    while True:
        # An autumn clock change has two occurrences: choose the first. A
        # spring gap has no occurrence and cannot match an advertised time.
        target = candidate.replace(tzinfo=UK_TIME_ZONE, fold=0).astimezone(UTC)
        if (
            target + SEARCH_GRACE >= now
            and target.astimezone(UK_TIME_ZONE).replace(tzinfo=None) == candidate
        ):
            return target
        candidate += _WEEK


def _endpoint_codes(pairs: Iterable[LocationPair]) -> list[str]:
    endpoints = set()
    for pair in pairs:
        location = pair.location
        if location is None:
            return []
        # Prefer public station codes over timetable location codes, which
        # can differ between a station's platforms or lines.
        codes = location.short_codes or location.long_codes
        if not codes or not all(code.strip() for code in codes):
            return []
        endpoints.update(
            f"{location.namespace}:{code.strip().upper()}" for code in codes
        )
    return sorted(endpoints)


def route_fingerprint(
    service: BoardRow | ServiceDetail | NetworkRailServiceDetail,
) -> RouteFingerprint | None:
    """Require an operator code and identifiable locations for every endpoint."""
    metadata = service.schedule_metadata
    if metadata is None or metadata.operator is None:
        return None
    operator = (metadata.operator.code or "").strip().upper()
    if not operator:
        return None
    origin = _endpoint_codes(service.origin)
    destination = _endpoint_codes(service.destination)
    if not origin or not destination:
        return None
    return {
        "operator": operator,
        "origin": origin,
        "destination": destination,
    }


def advertised_event(row: BoardRow, direction: str) -> IndividualTemporalData | None:
    """Return the advertised arrival or departure that a passenger can select."""
    metadata = row.schedule_metadata
    timing = row.temporal_data
    if (
        metadata is None
        or not metadata.unique_identity
        or metadata.in_passenger_service is False
        or metadata.mode_type not in (None, ModeType.TRAIN)
        or timing is None
    ):
        return None
    if direction == "arrival":
        excluded_call = LocationCallType.ADVERTISED_PICK_UP
        activity = timing.arrival
    elif direction == "departure":
        excluded_call = LocationCallType.ADVERTISED_SET_DOWN
        activity = timing.departure
    else:
        raise ValueError("Direction must be arrival or departure")
    if (
        timing.scheduled_call_type in (excluded_call, LocationCallType.OPERATIONAL_ONLY)
        or activity is None
        or activity.schedule_advertised is None
    ):
        return None
    return activity


def call_label(
    row: BoardRow, station_name: str, direction: str, scheduled: datetime
) -> str:
    """Describe this dated occurrence using the route advertised on its board."""
    endpoints = row.origin if direction == "arrival" else row.destination
    route = " / ".join(
        dict.fromkeys(
            pair.location.description
            for pair in endpoints
            if pair.location and pair.location.description
        )
    )
    metadata = row.schedule_metadata
    identity = (metadata.identity or metadata.unique_identity) if metadata else None
    when = scheduled.astimezone(UK_TIME_ZONE).strftime("%d %b %Y %H:%M %Z")
    return " · ".join(filter(None, (station_name, when, route, identity)))


def resolve_weekly_call(
    seed: TrackedCall,
    target: datetime,
    board: Iterable[BoardRow],
    *,
    station_name: str | None = None,
) -> TrackedCall | None:
    """Select a unique matching dated service from the tracked station's board."""
    if not seed.repeat_weekly or not seed.recurrence_match:
        return None
    if target.utcoffset() is None:
        raise ValueError("Weekly timetable matching requires an offset-aware target")
    target = target.astimezone(UTC)
    # Only resolve dates belonging to this rule, including after a restart
    # from a saved target. An older dated service cannot become a new occurrence.
    previous = datetime.fromisoformat(seed.scheduled)
    if target <= previous or next_week_target(previous, target) != target:
        return None
    matches: dict[str, BoardRow] = {}
    for row in board:
        activity = advertised_event(row, seed.direction)
        if activity is None or activity.schedule_advertised.astimezone(UTC) != target:
            continue
        if (
            row.schedule_metadata.unique_identity != seed.unique_identity
            and route_fingerprint(row) == seed.recurrence_match
        ):
            matches[row.schedule_metadata.unique_identity] = row
    if len(matches) != 1:
        return None
    identity, row = next(iter(matches.items()))
    return replace(
        seed,
        unique_identity=identity,
        scheduled=target.isoformat(),
        label=call_label(row, station_name or seed.station, seed.direction, target),
        tracking_id=seed.key,
    )
