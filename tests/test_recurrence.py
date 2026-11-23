"""Test UK local recurrence dates and conservative timetable matching."""

from datetime import datetime, timedelta

import pytest

from custom_components.realtime_trains.models import LocationLineUpResponse
from custom_components.realtime_trains.recurrence import (
    next_week_target,
    resolve_weekly_call,
    route_fingerprint,
)
from custom_components.realtime_trains.tracking import TrackedCall

from .rtt_fixtures import STATION, board_response, location_call

SEED_ID = "gb-nr:L01525:2026-03-22"
SEED_TIME = "2026-03-22T09:15:00Z"
NEXT_TIME = "2026-03-29T09:15:00+01:00"
NEXT_ID = "gb-nr:C30815:2026-03-29"


@pytest.mark.parametrize(
    ("previous", "now", "next_stamp"),
    [
        (SEED_TIME, SEED_TIME, "2026-03-29T08:15:00Z"),
        ("2026-10-18T09:15:00+01:00", "2026-10-18T10:15:00Z", "2026-10-25T09:15:00Z"),
        ("2026-10-18T01:30:00+01:00", "2026-10-18T02:30:00Z", "2026-10-25T00:30:00Z"),
        ("2026-03-22T01:30:00Z", SEED_TIME, "2026-04-05T00:30:00Z"),
        (SEED_TIME, "2026-04-26T07:00:00Z", "2026-04-26T08:15:00Z"),
        (SEED_TIME, "2026-04-26T09:00:00Z", "2026-04-26T08:15:00Z"),
        (SEED_TIME, "2026-04-26T10:00:00Z", "2026-05-03T08:15:00Z"),
    ],
    ids=[
        "spring",
        "autumn",
        "autumn-fold",
        "spring-gap",
        "missed-weeks",
        "within-grace",
        "outside-grace",
    ],
)
def test_weekly_occurrence_preserves_london_wall_time(previous, now, next_stamp):
    assert next_week_target(
        datetime.fromisoformat(previous), datetime.fromisoformat(now)
    ) == datetime.fromisoformat(next_stamp)


def row_payload(
    identity, scheduled, *, direction="departure", destination=None, operator=None
):
    call = location_call(**{direction: {"scheduleAdvertised": scheduled}})
    (row,) = board_response(call, service_ids=[identity])["services"]
    if destination is not None:
        row["destination"] = [{"location": destination}]
    if operator is not None:
        row["scheduleMetadata"]["operator"] = {"code": operator}
    return row


def parse_rows(*rows):
    return LocationLineUpResponse.from_dict({"services": list(rows)}).services


def weekly_seed():
    (row,) = parse_rows(row_payload(SEED_ID, SEED_TIME))
    return TrackedCall(
        SEED_ID,
        "gb-nr:CLJ",
        "departure",
        SEED_TIME,
        "Clapham Junction to Waterloo",
        repeat_weekly=True,
        recurrence_match=route_fingerprint(row),
    )


def test_weekly_lookup_requires_same_route_operator_direction_and_explicit_time():
    seed = weekly_seed()
    target = datetime.fromisoformat(NEXT_TIME)
    expected = row_payload(NEXT_ID, NEXT_TIME)
    wrong_route = row_payload("gb-nr:C30816:2026-03-29", NEXT_TIME, destination=STATION)
    wrong_operator = row_payload("gb-nr:C30817:2026-03-29", NEXT_TIME, operator="GW")
    wrong_event = row_payload("gb-nr:C30818:2026-03-29", NEXT_TIME, direction="arrival")
    wrong_time = row_payload(
        "gb-nr:C30819:2026-03-29", (target + timedelta(minutes=1)).isoformat()
    )
    distractors = [wrong_route, wrong_operator, wrong_event, wrong_time]

    assert resolve_weekly_call(seed, target, parse_rows(*distractors)) is None
    found = resolve_weekly_call(seed, target, parse_rows(*distractors, expected))
    assert found is not None
    assert found.unique_identity == NEXT_ID
    assert found.key == seed.key
    assert found.repeat_weekly
    assert datetime.fromisoformat(found.scheduled) == target


def test_weekly_lookup_refuses_to_choose_between_two_exact_matches():
    candidates = parse_rows(
        row_payload(NEXT_ID, NEXT_TIME),
        row_payload("gb-nr:C30999:2026-03-29", NEXT_TIME),
    )
    assert (
        resolve_weekly_call(
            weekly_seed(), datetime.fromisoformat(NEXT_TIME), candidates
        )
        is None
    )


def test_duplicate_board_rows_for_same_new_run_are_one_match():
    identity = "gb-nr:L01525:2026-03-29"
    row = row_payload(identity, NEXT_TIME)
    found = resolve_weekly_call(
        weekly_seed(), datetime.fromisoformat(NEXT_TIME), parse_rows(row, row)
    )
    assert found is not None
    assert found.unique_identity == identity


def test_fingerprint_requires_an_operator_and_both_route_endpoints():
    row = row_payload(SEED_ID, SEED_TIME)
    row["origin"] = []
    assert route_fingerprint(parse_rows(row)[0]) is None
    row = row_payload(SEED_ID, SEED_TIME)
    del row["scheduleMetadata"]["operator"]
    assert route_fingerprint(parse_rows(row)[0]) is None


def test_old_dated_configuration_keeps_existing_key_and_defaults_to_one_off():
    data = {
        "unique_identity": SEED_ID,
        "station": "gb-nr:CLJ",
        "direction": "departure",
        "scheduled": SEED_TIME,
        "label": "09:15 to Waterloo",
    }
    original_key = "14edd6f5a6f375728a27eceec82d8681d099a970cb8116fbc5d0e80aeb65cb01"
    one_off = TrackedCall.from_dict(data)

    assert not one_off.repeat_weekly
    assert one_off.key == original_key
    assert TrackedCall.from_dict(one_off.to_dict()) == one_off
