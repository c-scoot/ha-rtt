"""Test tracking decisions against realistic RTT station events."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.realtime_trains.models import ServiceLocation
from custom_components.realtime_trains.tracking import TrackedCall, evaluate

from .rtt_fixtures import DESTINATION, SERVICE_ID, location_call

SCHEDULED = "2026-11-20T16:45:00Z"
NOW = datetime(2026, 11, 20, 16, 30, tzinfo=UTC)


def clock(value):
    return datetime.fromisoformat(value)


def tracked_call(*, direction="departure", scheduled=SCHEDULED, identity=SERVICE_ID):
    return TrackedCall(
        unique_identity=identity,
        station="CLJ",
        direction=direction,
        scheduled=scheduled,
        label="London Waterloo",
    )


def check_call(call, *locations, **kwargs):
    return evaluate(
        call, [ServiceLocation.from_dict(item) for item in locations], **kwargs
    )


def test_departure_waits_for_actual_at_selected_station():
    selected = location_call(
        arrival={
            "scheduleAdvertised": "2026-11-20T16:43:00Z",
            "realtimeActual": "2026-11-20T16:44:00Z",
        },
        departure={
            "scheduleAdvertised": SCHEDULED,
            "realtimeForecast": "2026-11-20T16:48:00Z",
        },
        platform={"planned": "7"},
    )
    other = location_call(
        station=DESTINATION, departure={"realtimeActual": "2026-11-20T16:50:00Z"}
    )
    now = clock("2026-11-20T16:51:00Z")
    result = check_call(tracked_call(), selected, other, now=now)
    assert result["status"] == "delayed"
    assert result["delay_minutes"] == 3
    assert result["platform"] == "7"
    assert result["actual"] is None
    assert not result["terminal"]

    actual = "2026-11-20T16:49:00Z"
    selected["temporalData"]["departure"]["realtimeActual"] = actual
    selected["locationMetadata"]["platform"]["actual"] = "9"
    result = check_call(tracked_call(), selected, other, now=now, previous=result)
    assert result["status"] == "departed"
    assert result["terminal"]
    assert result["delay_minutes"] == 4
    assert result["platform"] == "9"
    assert clock(result["actual"]) == clock(actual)
    assert result["next_poll"] is None


def test_arrival_can_finish_while_departure_is_forecast():
    actual = "2026-11-20T16:46:00Z"
    selected = location_call(
        arrival={"scheduleAdvertised": SCHEDULED, "realtimeActual": actual},
        departure={"realtimeForecast": "2026-11-20T16:50:00Z"},
    )
    result = check_call(tracked_call(direction="arrival"), selected, now=clock(actual))
    assert result["status"] == "arrived"
    assert result["terminal"]
    assert result["next_poll"] is None


@pytest.mark.parametrize(
    ("display_as", "cancelled", "status"),
    [
        ("CALL", True, "cancelled"),
        ("CANCELLED", False, "cancelled"),
        ("DIVERTED", False, "diverted"),
    ],
)
def test_terminal_disruptions(display_as, cancelled, status):
    selected = location_call(
        departure={"scheduleAdvertised": SCHEDULED, "isCancelled": cancelled},
        display_as=display_as,
    )
    result = check_call(tracked_call(), selected, now=NOW)
    assert result["status"] == status
    assert result["terminal"]
    assert result["next_poll"] is None


@pytest.mark.parametrize("forecast_data", [{}, {"realtimeNoReport": True}])
def test_no_realtime_data_never_implies_on_time_or_departure(forecast_data):
    selected = location_call(
        departure={"scheduleAdvertised": SCHEDULED, **forecast_data}
    )
    result = check_call(tracked_call(), selected, now=NOW + timedelta(minutes=25))
    assert result["status"] in {"scheduled", "unconfirmed", "no_report"}
    assert result["actual"] is None
    assert result["delay_minutes"] is None


def test_temporarily_missing_stop_is_not_terminal():
    result = check_call(tracked_call(), location_call(station=DESTINATION), now=NOW)
    assert result["status"] in {"scheduled", "unconfirmed"}
    assert not result["terminal"]
    assert result["next_poll"] is not None


def test_missing_call_keeps_last_forecast_but_never_confirms_departure():
    forecast = "2026-11-20T16:51:00Z"
    selected = location_call(
        departure={"scheduleAdvertised": SCHEDULED, "realtimeForecast": forecast}
    )
    call = tracked_call()
    previous = check_call(call, selected, now=NOW)

    missing = check_call(call, now=NOW + timedelta(minutes=30), previous=previous)
    assert missing["status"] == "delayed"
    assert clock(missing["expected"]) == clock(forecast)
    assert not missing["matched_call"]
    assert not missing["terminal"]

    expired = check_call(call, now=NOW + timedelta(days=2), previous=missing)
    assert expired["status"] == "unconfirmed"
    assert expired["actual"] is None
    assert expired["next_poll"] is None


@pytest.mark.parametrize(("minutes_away", "interval"), [(30, 60), (45, 300)])
def test_polling_cadence(minutes_away, interval):
    scheduled = (NOW + timedelta(minutes=minutes_away)).isoformat()
    selected = location_call(departure={"scheduleAdvertised": scheduled})
    result = check_call(tracked_call(scheduled=scheduled), selected, now=NOW)
    assert clock(result["next_poll"]) == NOW + timedelta(seconds=interval)


@pytest.mark.parametrize(
    ("scheduled", "forecast", "delay"),
    [
        ("2026-11-20T23:58:00Z", "2026-11-21T00:07:00Z", 9),
        ("2026-10-25T01:55:00+01:00", "2026-10-25T01:05:00+00:00", 10),
        ("2026-03-29T00:55:00+00:00", "2026-03-29T02:05:00+01:00", 10),
    ],
    ids=["midnight", "fall-back", "spring-forward"],
)
def test_delay_uses_absolute_timestamps(scheduled, forecast, delay):
    now = clock(scheduled) - timedelta(minutes=10)
    selected = location_call(
        departure={"scheduleAdvertised": scheduled, "realtimeForecast": forecast}
    )
    result = check_call(tracked_call(scheduled=scheduled), selected, now=now)
    assert result["status"] == "delayed"
    assert result["delay_minutes"] == delay


def test_service_identity_round_trips_distinct_service_dates():
    original = tracked_call()
    following_day = tracked_call(identity="gb-nr:L01525:2026-11-21")
    assert original.key != following_day.key
    assert TrackedCall.from_dict(json.loads(json.dumps(original.to_dict()))) == original


def test_repeated_clock_times_match_the_correct_stop_on_the_same_service():
    first_time = "2026-10-25T01:10:00+01:00"
    second_time = "2026-10-25T01:10:00+00:00"
    first = location_call(
        departure={"scheduleAdvertised": first_time, "realtimeActual": first_time}
    )
    second = location_call(departure={"scheduleAdvertised": second_time})
    call = tracked_call(scheduled=second_time)

    result = check_call(call, first, second, now=clock(second_time))
    assert not result["terminal"]
    assert result["actual"] is None

    second["temporalData"]["departure"]["realtimeActual"] = second_time
    terminal = check_call(call, first, second, now=clock(second_time))
    restored = json.loads(json.dumps(terminal))
    assert clock(restored["actual"]) == clock(second_time)
    assert (
        check_call(
            call, now=clock(second_time) + timedelta(minutes=10), previous=restored
        )
        == terminal
    )
