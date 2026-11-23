"""Identity and lifecycle of one advertised arrival or departure at a station."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from .models import (
    GeographicLocation,
    IndividualTemporalData,
    LocationDisplayAs,
    NetworkRailServiceLocation,
    ReasonBlock,
    ReasonType,
    ServiceLocation,
)

CallLocation = ServiceLocation | NetworkRailServiceLocation
_TERMINAL = {"arrived", "departed", "cancelled", "diverted", "unconfirmed"}
_NEAR_INTERVAL = timedelta(seconds=60)
_FAR_INTERVAL = timedelta(seconds=300)
_NEAR_WINDOW = timedelta(minutes=30)


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("A railway event requires an offset-aware time")
    return value.astimezone(UTC)


class RouteFingerprint(TypedDict):
    """Timetable attributes that must agree before a weekly train can be selected."""

    operator: str
    origin: list[str]
    destination: list[str]


@dataclass(frozen=True, slots=True)
class TrackedCall:
    """The selection survives relabelling, midnight and Home Assistant restarts."""

    unique_identity: str
    station: str
    direction: str
    scheduled: str
    label: str
    repeat_weekly: bool = False
    tracking_id: str | None = None
    recurrence_match: RouteFingerprint | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        if self.direction not in {"arrival", "departure"}:
            raise ValueError("Direction must be arrival or departure")
        scheduled = _utc(datetime.fromisoformat(self.scheduled))
        object.__setattr__(self, "scheduled", scheduled.isoformat())

    @property
    def key(self) -> str:
        """Stable Home Assistant identity, independent of the human-facing label."""
        if self.tracking_id:
            return self.tracking_id
        namespace, _, code = self.station.upper().rpartition(":")
        identity = (
            self.unique_identity,
            namespace or "GB-NR",
            code,
            self.direction,
            self.scheduled,
        )
        return hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode()
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible configuration."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrackedCall:
        """Read the selected fields from storage or config entry data."""
        return cls(
            unique_identity=data["unique_identity"],
            station=data["station"],
            direction=data["direction"],
            scheduled=data["scheduled"],
            label=data["label"],
            repeat_weekly=data.get("repeat_weekly", False),
            tracking_id=data.get("tracking_id"),
            recurrence_match=data.get("recurrence_match"),
        )


def _station_matches(station: str, location: GeographicLocation | None) -> bool:
    if location is None:
        return False
    namespace, _, code = station.upper().rpartition(":")
    if namespace and namespace != location.namespace.upper():
        return False
    return any(
        item.upper() == code for item in (*location.short_codes, *location.long_codes)
    )


def _event(call: TrackedCall, location: CallLocation) -> IndividualTemporalData | None:
    temporal = location.temporal_data
    if temporal is None:
        return None
    return temporal.arrival if call.direction == "arrival" else temporal.departure


def _match(
    call: TrackedCall, locations: Iterable[CallLocation], scheduled: datetime
) -> CallLocation | None:
    visits = [
        visit for visit in locations if _station_matches(call.station, visit.location)
    ]
    for visit in visits:
        event = _event(call, visit)
        if (
            event
            and event.schedule_advertised
            and _utc(event.schedule_advertised) == scheduled
        ):
            return visit
    # A cancelled event can lose its scheduled time. Only an unambiguous
    # visit without a contradictory advertised time can be used in that case.
    if len(visits) == 1:
        event = _event(call, visits[0])
        if event is None or event.schedule_advertised is None:
            return visits[0]
    return None


def _apply_visit(
    result: dict[str, Any],
    call: TrackedCall,
    visit: CallLocation,
    scheduled: datetime,
    reasons: Iterable[ReasonBlock],
) -> None:
    temporal = visit.temporal_data
    if temporal is None:
        return
    event = _event(call, visit)
    actual = event.realtime_actual if event else None
    expected = (event.realtime_forecast or event.realtime_estimate) if event else None
    reported = actual or expected
    delay = int((_utc(reported) - scheduled).total_seconds() / 60) if reported else None

    if temporal.display_as == LocationDisplayAs.DIVERTED:
        status = "diverted"
    elif (
        temporal.display_as == LocationDisplayAs.CANCELLED
        or (event and event.is_cancelled)
        or (
            call.direction == "arrival"
            and temporal.display_as == LocationDisplayAs.STARTS
        )
        or (
            call.direction == "departure"
            and temporal.display_as == LocationDisplayAs.TERMINATES
        )
    ):
        status = "cancelled"
    elif actual:
        status = "arrived" if call.direction == "arrival" else "departed"
    elif event and (
        event.realtime_no_report
        or (event.realtime_estimate and not event.realtime_forecast)
    ):
        status = "no_report"
    elif delay is None:
        status = "scheduled"
    elif delay > 0:
        status = "delayed"
    elif delay < 0:
        status = "early"
    else:
        status = "on_time"

    platform = visit.location_metadata.platform if visit.location_metadata else None
    reason = event.cancellation_reason_code if event else None
    kind = (
        ReasonType.CANCEL if status in {"cancelled", "diverted"} else ReasonType.DELAY
    )
    for block in reasons:
        if block.type == kind and (block.long_text or block.short_text):
            reason = block.long_text or block.short_text
            break

    result.update(
        status=status,
        actual=_utc(actual).isoformat() if actual else None,
        expected=_utc(expected).isoformat() if expected else None,
        delay_minutes=delay,
        reason=reason,
        platform=(platform.actual or platform.planned) if platform else None,
        platform_source=None,
        planned_platform=platform.planned if platform else None,
        confirmed_platform=platform.actual if platform else None,
    )
    if result["platform"] is not None:
        result["platform_source"] = "actual" if platform.actual else "planned"


def evaluate(
    call: TrackedCall,
    locations: Iterable[CallLocation],
    *,
    now: datetime | None = None,
    previous: Mapping[str, Any] | None = None,
    reasons: Iterable[ReasonBlock] = (),
) -> dict[str, Any]:
    """Resolve the selected station event and the next time it needs checking."""
    if previous and previous.get("terminal"):
        return dict(previous)

    instant = _utc(now or datetime.now(UTC))
    scheduled = datetime.fromisoformat(call.scheduled)
    result: dict[str, Any] = {
        "scheduled": call.scheduled,
        "expected": None,
        "actual": None,
        "status": "scheduled",
        "terminal": False,
        "delay_minutes": None,
        "platform": None,
        "platform_source": None,
        "planned_platform": None,
        "confirmed_platform": None,
        "reason": None,
        "next_poll": None,
    }
    if previous:
        result.update(previous)
    visit = _match(call, locations, scheduled)
    result["matched_call"] = visit is not None
    if visit is not None:
        _apply_visit(result, call, visit, scheduled, reasons)

    expected = (
        datetime.fromisoformat(result["expected"]) if result["expected"] else None
    )
    expiry = scheduled + timedelta(days=1)
    if expected:
        expiry = max(expiry, expected + _NEAR_INTERVAL)
    result["expires_at"] = expiry.isoformat()
    if result["status"] not in _TERMINAL and instant >= expiry:
        result["status"] = "unconfirmed"
    result["terminal"] = result["status"] in _TERMINAL
    if result["terminal"]:
        result["next_poll"] = None
    else:
        near_from = min(scheduled, expected or scheduled) - _NEAR_WINDOW
        if instant >= near_from:
            next_poll = instant + _NEAR_INTERVAL
        else:
            next_poll = min(instant + _FAR_INTERVAL, near_from)
        result["next_poll"] = min(next_poll, expiry).isoformat()
    return result


def platform_status(snapshot: Mapping[str, Any]) -> str:
    """Describe platform certainty without inferring confirmation from equality."""
    if not snapshot.get("platform"):
        return "not_available"
    source = snapshot.get("platform_source")
    if source == "actual":
        return "confirmed"
    if source == "planned":
        return "planned"
    return "unconfirmed"
