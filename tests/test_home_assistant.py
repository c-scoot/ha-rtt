"""Exercise the real Home Assistant flows, coordinator, store and entities."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest

from custom_components.realtime_trains.const import (
    CONF_DIRECTION,
    CONF_STATION,
    CONF_TOKEN,
    CONF_TRACK,
    CONF_TRACKS,
    CONF_WINDOW_START,
    DOMAIN,
)
from custom_components.realtime_trains.tracking import TrackedCall

from .rtt_fixtures import (
    CREDENTIAL_MARKER,
    DESTINATION,
    INFO,
    STOPS,
    board_response,
    location_call,
    response,
    service_response,
)

pytest.importorskip("pytest_homeassistant_custom_component")


@pytest.fixture
def session(enable_custom_integrations, monkeypatch):
    """Keep every HTTP attempt at the session boundary so HA stays local."""
    from custom_components.realtime_trains import config_flow, coordinator

    client = MagicMock()
    monkeypatch.setattr(config_flow, "async_get_clientsession", lambda _: client)
    monkeypatch.setattr(coordinator, "async_get_clientsession", lambda _: client)
    return client


async def create_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: CREDENTIAL_MARKER}
    )
    assert result["step_id"] == "station"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STATION: "gb-nr:CLJ"}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    return result["result"]


def status_sensor(hass, direction):
    candidates = [
        state
        for state in hass.states.async_all("sensor")
        if state.entity_id.endswith("_status")
        and not state.entity_id.endswith("_platform_status")
        and state.attributes.get("direction") == direction
    ]
    assert len(candidates) == 1
    return candidates[0]


def track_sensor(hass, entry, key, description):
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{key}_{description}"
    )
    assert entity_id is not None
    return hass.states.get(entity_id)


async def open_options(hass, entry, step):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def refresh_tracks(hass, entry, monkeypatch):
    coordinator = entry.runtime_data.coordinator
    # Avoid waiting for Home Assistant's own request debounce timer.
    monkeypatch.setattr(coordinator, "async_request_refresh", coordinator.async_refresh)
    await coordinator.async_refresh_tracked()
    await hass.async_block_till_done()


async def test_config_flow_reports_invalid_auth(hass, session):
    session.request.return_value = response(401)[0]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: CREDENTIAL_MARKER}
    )
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize("direction", ["arrival", "departure"])
async def test_track_from_options_recover_from_timeout_and_restore(
    hass, session, direction, monkeypatch
):
    now = datetime.now(UTC).replace(microsecond=0)
    scheduled = now - timedelta(minutes=1)
    actual = now
    service_id = f"gb-nr:L01525:{scheduled.date().isoformat()}"
    timed_event = {
        "scheduleAdvertised": scheduled.isoformat(),
        "realtimeForecast": (scheduled + timedelta(minutes=4)).isoformat(),
    }
    predicted_call = location_call(
        **{direction: timed_event}, platform={"planned": "5"}
    )
    completed_call = location_call(
        **{
            direction: {
                "scheduleAdvertised": scheduled.isoformat(),
                "realtimeActual": actual.isoformat(),
            }
        },
        platform={"planned": "5", "actual": "6"},
    )
    board = board_response(predicted_call, service_ids=[service_id])
    details = [
        response(200, service_response(predicted_call, unique_identity=service_id))[0],
        TimeoutError("Synthetic timeout"),
        response(200, service_response(completed_call, unique_identity=service_id))[0],
    ]

    def request(_method, url, **_kwargs):
        path = urlsplit(url).path
        if path.endswith("/service"):
            reply = details.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        payload = {"/api/info": INFO, "/data/stops": STOPS, "/rtt/location": board}[
            path
        ]
        return response(200, payload)[0]

    session.request.side_effect = request
    entry = await create_entry(hass)
    key = TrackedCall(
        service_id, "gb-nr:CLJ", direction, scheduled.isoformat(), "train"
    ).key

    result = await open_options(hass, entry, "add_track")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DIRECTION: direction,
            CONF_WINDOW_START: (now - timedelta(minutes=10)).isoformat(),
        },
    )
    assert result["step_id"] == "choose_track"
    train_selector = result["data_schema"].schema[CONF_TRACK]
    (candidate,) = train_selector.config["options"]
    assert "Platform 5 (planned)" in candidate["label"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRACK: key}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options[CONF_TRACKS][0]["unique_identity"] == service_id
    assert "Platform" not in entry.options[CONF_TRACKS][0]["label"]

    async def refresh():
        await refresh_tracks(hass, entry, monkeypatch)

    await refresh()
    state = status_sensor(hass, direction)
    assert state.state == "delayed"
    assert state.attributes["stale"] is False
    assert track_sensor(hass, entry, key, "platform_status").state == "planned"

    await refresh()
    state = status_sensor(hass, direction)
    assert state.state == "delayed"
    assert state.attributes["stale"] is True

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = status_sensor(hass, direction)
    assert state.state == "delayed"
    assert state.attributes["stale"] is True

    await refresh()
    expected = "arrived" if direction == "arrival" else "departed"
    assert status_sensor(hass, direction).state == expected
    assert track_sensor(hass, entry, key, "platform_status").state == "confirmed"
    platform = track_sensor(hass, entry, key, "platform")
    assert platform.state == "6"
    assert platform.attributes["planned_platform"] == "5"
    assert platform.attributes["confirmed_platform"] == "6"
    assert entry.runtime_data.coordinator.update_interval is None

    http_count = session.request.call_count
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert status_sensor(hass, direction).state == expected
    await refresh()
    assert session.request.call_count == http_count

    from homeassistant.helpers import entity_registry as er

    entity_id = status_sensor(hass, direction).entity_id
    registry = er.async_get(hass)
    assert registry.async_get(entity_id) is not None
    result = await open_options(hass, entry, "remove_track")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRACK: [key]}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert not entry.options[CONF_TRACKS]
    assert hass.states.get(entity_id) is None
    assert registry.async_get(entity_id) is None


async def test_legacy_entry_adds_a_second_station_and_polls_one_shared_service(
    hass, session
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.realtime_trains.const import CONF_STATION_NAME, CONF_STATIONS

    clapham, waterloo = "gb-nr:CLJ", "gb-nr:WAT"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_TOKEN: CREDENTIAL_MARKER,
            CONF_STATION: clapham,
            CONF_STATION_NAME: "Clapham Junction (CLJ)",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    session.request.assert_not_called()

    now = datetime.now(UTC).replace(microsecond=0)
    service_id = f"gb-nr:L01525:{now.date().isoformat()}"
    start = now - timedelta(minutes=35)
    departure = (now - timedelta(minutes=25)).isoformat()
    arrival = (now - timedelta(minutes=10)).isoformat()
    clapham_call = location_call(
        departure={"scheduleAdvertised": departure, "realtimeActual": departure}
    )
    waterloo_call = location_call(
        station=DESTINATION,
        arrival={"scheduleAdvertised": arrival, "realtimeActual": arrival},
    )
    boards = {
        clapham: board_response(clapham_call, service_ids=[service_id]),
        waterloo: board_response(
            waterloo_call, service_ids=[service_id], station=DESTINATION
        ),
    }
    paths = []

    def request(_method, url, **kwargs):
        path = urlsplit(url).path
        paths.append(path)
        if path.endswith("/location"):
            payload = boards[kwargs["params"]["code"]]
        elif path.endswith("/service"):
            assert kwargs["params"]["uniqueIdentity"] == service_id
            payload = service_response(
                clapham_call, waterloo_call, unique_identity=service_id
            )
        else:
            assert path == "/data/stops"
            payload = STOPS
        return response(200, payload)[0]

    session.request.side_effect = request
    result = await open_options(hass, entry, "manage_stations")
    assert result["step_id"] == "manage_stations"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STATIONS: []}
    )
    assert result["errors"] == {CONF_STATIONS: "select_station"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STATIONS: [clapham, waterloo]}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert set(entry.options[CONF_STATIONS]) == {clapham, waterloo}
    assert entry.data[CONF_STATION] == clapham
    assert paths == ["/data/stops"]

    coordinator = entry.runtime_data.coordinator
    for _ in range(2):
        (first,) = await coordinator.async_get_board(start, clapham)
        (second,) = await coordinator.async_get_board(start, waterloo)
        assert (
            first.temporal_data.departure.schedule_advertised.isoformat() == departure
        )
        assert second.temporal_data.arrival.schedule_advertised.isoformat() == arrival
    assert paths.count("/rtt/location") == 2
    with pytest.raises(ValueError):
        await coordinator.async_get_board(start, "gb-nr:WOK")
    assert paths.count("/rtt/location") == 2

    async def add_track(station, direction, scheduled):
        result = await open_options(hass, entry, "add_track")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_DIRECTION: direction,
                CONF_STATION: station,
                CONF_WINDOW_START: start.isoformat(),
            },
        )
        assert result["step_id"] == "choose_track"
        key = TrackedCall(service_id, station, direction, scheduled, "train").key
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_TRACK: key}
        )
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
        return key

    first_key = await add_track(clapham, "departure", departure)
    first_entity_id = track_sensor(hass, entry, first_key, "status").entity_id
    second_key = await add_track(waterloo, "arrival", arrival)
    coordinator = entry.runtime_data.coordinator
    coordinator.data[first_key]["next_poll"] = (now + timedelta(minutes=5)).isoformat()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert paths.count("/rtt/service") == 1
    assert len(entry.runtime_data.budget.dump()["requests"]) == len(paths)
    assert track_sensor(hass, entry, first_key, "status").state == "departed"
    assert track_sensor(hass, entry, second_key, "status").state == "arrived"
    assert track_sensor(hass, entry, first_key, "status").entity_id == first_entity_id

    result = await open_options(hass, entry, "manage_stations")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STATIONS: [waterloo]}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert set(entry.options[CONF_STATIONS]) == {waterloo}
    assert track_sensor(hass, entry, first_key, "status").entity_id == first_entity_id
    assert track_sensor(hass, entry, first_key, "status").state == "departed"
    assert paths.count("/rtt/service") == 1


async def test_legacy_terminal_platform_remains_confirmed_without_refetch(
    hass, session
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.realtime_trains.const import CONF_STATION_NAME, CONF_STATIONS
    from custom_components.realtime_trains.coordinator import entry_store

    scheduled = "2026-11-20T16:45:00Z"
    call = TrackedCall(
        "gb-nr:L01525:2026-11-20", "gb-nr:CLJ", "departure", scheduled, "Train"
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TOKEN: CREDENTIAL_MARKER,
            CONF_STATION: call.station,
            CONF_STATION_NAME: "Clapham Junction (CLJ)",
        },
        options={CONF_TRACKS: [call.to_dict()]},
    )
    entry.add_to_hass(hass)
    await entry_store(hass, entry).async_save(
        {
            "tracks": {
                call.key: {
                    "status": "departed",
                    "terminal": True,
                    "scheduled": scheduled,
                    "platform": "5",
                    "platform_source": "actual",
                    "next_poll": None,
                }
            }
        }
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    platform = track_sensor(hass, entry, call.key, "platform")
    assert platform.state == "5"
    assert platform.attributes["confirmed_platform"] == "5"
    assert platform.attributes["planned_platform"] is None
    assert track_sensor(hass, entry, call.key, "platform_status").state == "confirmed"
    session.request.assert_not_called()

    session.request.return_value = response(200, STOPS)[0]
    result = await open_options(hass, entry, "manage_stations")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STATIONS: [call.station, "gb-nr:WAT"]}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    restored = track_sensor(hass, entry, call.key, "platform")
    assert restored.entity_id == platform.entity_id
    assert restored.state == "5"
    assert track_sensor(hass, entry, call.key, "platform_status").state == "confirmed"
    assert [urlsplit(args.args[1]).path for args in session.request.call_args_list] == [
        "/data/stops"
    ]


@pytest.mark.parametrize(
    ("initial_result", "next_date"),
    [("departed", "2026-03-29"), ("cancelled", "2026-04-26")],
    ids=["next-week-dst", "cancelled-then-missed-weeks"],
)
async def test_weekly_tracker_rolls_over_once_and_can_become_dated_again(
    hass, session, freezer, monkeypatch, initial_result, next_date
):
    initial_id = "gb-nr:L01525:2026-03-22"
    initial_time = "2026-03-22T09:15:00Z"
    next_id = f"gb-nr:C30815:{next_date}"
    next_time = f"{next_date}T09:15:00+01:00"
    lookup_time = datetime.fromisoformat(f"{next_date}T07:00:00Z")
    seed = location_call(departure={"scheduleAdvertised": initial_time})
    upcoming = location_call(departure={"scheduleAdvertised": next_time})
    completed = location_call(
        departure={
            "scheduleAdvertised": initial_time,
            "realtimeActual": initial_time if initial_result == "departed" else None,
            "isCancelled": initial_result == "cancelled",
        },
        display_as="CANCELLED" if initial_result == "cancelled" else "CALL",
        platform={"planned": "5", "actual": "6"},
    )
    board_queries = []
    detail_queries = []
    cancel_next = False

    def request(_method, url, **kwargs):
        path = urlsplit(url).path
        params = kwargs["params"]
        if path == "/rtt/location":
            board_queries.append(datetime.fromisoformat(params["timeFrom"]))
            if len(board_queries) == 1:
                payload = board_response(seed, service_ids=[initial_id])
            elif len(board_queries) == 2:
                return response(429, headers={"Retry-After": "900"})[0]
            else:
                payload = board_response(upcoming, service_ids=[next_id])
        elif path == "/rtt/service":
            current_id = params["uniqueIdentity"]
            detail_queries.append(current_id)
            current = completed if current_id == initial_id else upcoming
            if current_id == next_id and cancel_next:
                current = location_call(
                    departure={"scheduleAdvertised": next_time}, display_as="CANCELLED"
                )
            payload = service_response(current, unique_identity=current_id)
        else:
            payload = {"/api/info": INFO, "/data/stops": STOPS}[path]
        return response(200, payload)[0]

    freezer.move_to("2026-03-22T09:20:00Z")
    session.request.side_effect = request
    entry = await create_entry(hass)
    result = await open_options(hass, entry, "add_track")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DIRECTION: "departure", CONF_WINDOW_START: "2026-03-22T09:00:00Z"},
    )
    assert result["step_id"] == "choose_track"
    key = TrackedCall(initial_id, "gb-nr:CLJ", "departure", initial_time, "train").key
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRACK: key, "repeat_weekly": True}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options[CONF_TRACKS][0]["repeat_weekly"] is True

    async def refresh():
        await refresh_tracks(hass, entry, monkeypatch)

    await refresh()
    state = track_sensor(hass, entry, key, "status")
    entity_id = state.entity_id
    assert state.state == initial_result
    assert state.attributes["repeat_weekly"] is True
    assert datetime.fromisoformat(
        state.attributes["weekly_next_scheduled"]
    ) == datetime.fromisoformat("2026-03-29T08:15:00Z")
    assert track_sensor(hass, entry, key, "platform").state == "6"
    for _ in range(2):
        await refresh()
    assert detail_queries == [initial_id]
    assert len(board_queries) == 1

    freezer.move_to(lookup_time)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await refresh()
    assert len(board_queries) == 2
    assert board_queries[-1].date().isoformat() == next_date
    assert detail_queries == [initial_id]

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await refresh()
    assert len(board_queries) == 2
    assert track_sensor(hass, entry, key, "status").state == initial_result

    freezer.move_to(lookup_time + timedelta(minutes=16))
    await refresh()
    assert len(board_queries) == 3
    state = track_sensor(hass, entry, key, "status")
    assert state.entity_id == entity_id
    assert state.state == "scheduled"
    assert state.attributes["service_identity"] == next_id
    assert track_sensor(hass, entry, key, "platform_status").state == "not_available"
    assert track_sensor(hass, entry, key, "actual").state == "unknown"

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored = track_sensor(hass, entry, key, "status")
    assert restored.entity_id == entity_id
    assert restored.attributes["service_identity"] == next_id
    await refresh()
    assert detail_queries == [initial_id, next_id]

    result = await open_options(hass, entry, "add_track")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DIRECTION: "departure", CONF_WINDOW_START: f"{next_date}T08:00:00Z"},
    )
    assert result["step_id"] == "add_track"
    assert result["errors"] == {"base": "no_services"}
    assert len(entry.options[CONF_TRACKS]) == 1
    assert len(board_queries) == 4

    result = await open_options(hass, entry, "weekly_repeats")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRACK: []}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert track_sensor(hass, entry, key, "status").entity_id == entity_id
    effective = entry.runtime_data.coordinator.tracks[key]
    assert effective.unique_identity == next_id
    assert not effective.repeat_weekly
    assert datetime.fromisoformat(effective.scheduled) == datetime.fromisoformat(
        next_time
    )

    cancel_next = True
    await refresh()
    assert track_sensor(hass, entry, key, "status").state == "cancelled"
    assert detail_queries == [initial_id, next_id, next_id]
    freezer.move_to(lookup_time + timedelta(days=7))
    await refresh()
    assert len(board_queries) == 4
    assert detail_queries == [initial_id, next_id, next_id]


@pytest.mark.parametrize(
    "complete_route", [True, False], ids=["verified-route", "missing-route"]
)
async def test_enabling_repeat_on_legacy_track_checks_route_through_shared_budget(
    hass, session, freezer, complete_route
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.realtime_trains.const import CONF_STATION_NAME
    from custom_components.realtime_trains.coordinator import entry_store

    freezer.move_to("2026-03-22T09:20:00Z")
    dated_service = "gb-nr:L01525:2026-03-22"
    scheduled = "2026-03-22T09:15:00Z"
    legacy = {
        "unique_identity": dated_service,
        "station": "gb-nr:CLJ",
        "direction": "departure",
        "scheduled": scheduled,
        "label": "09:15 train to Waterloo",
    }
    key = TrackedCall.from_dict(legacy).key
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TOKEN: CREDENTIAL_MARKER,
            CONF_STATION: legacy["station"],
            CONF_STATION_NAME: "Clapham Junction (CLJ)",
        },
        options={CONF_TRACKS: [legacy]},
    )
    entry.add_to_hass(hass)
    await entry_store(hass, entry).async_save(
        {
            "tracks": {
                key: {
                    "status": "departed",
                    "terminal": True,
                    "scheduled": scheduled,
                    "next_poll": None,
                }
            }
        }
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = track_sensor(hass, entry, key, "status").entity_id
    session.request.assert_not_called()

    detail = service_response(
        location_call(
            departure={"scheduleAdvertised": scheduled, "realtimeActual": scheduled}
        ),
        unique_identity=dated_service,
    )
    if not complete_route:
        detail["service"]["origin"] = []
    session.request.return_value = response(200, detail)[0]
    result = await open_options(hass, entry, "weekly_repeats")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRACK: [key]}
    )
    await hass.async_block_till_done()
    session.request.assert_called_once()
    args = session.request.call_args
    assert urlsplit(args.args[1]).path == "/rtt/service"
    assert args.kwargs["params"] == {"uniqueIdentity": dated_service}
    assert len(entry.runtime_data.budget.dump()["requests"]) == 1

    saved = entry.options[CONF_TRACKS][0]
    assert track_sensor(hass, entry, key, "status").entity_id == entity_id
    if complete_route:
        assert result["type"] == "create_entry"
        assert saved["repeat_weekly"] is True
        assert saved["recurrence_match"] == {
            "operator": "SW",
            "origin": ["gb-nr:WOK"],
            "destination": ["gb-nr:WAT"],
        }
    else:
        assert result["errors"] == {"base": "cannot_repeat"}
        assert saved == legacy
