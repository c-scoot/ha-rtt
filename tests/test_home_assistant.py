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
        and state.attributes.get("direction") == direction
    ]
    assert len(candidates) == 1
    return candidates[0]


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

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_track"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DIRECTION: direction,
            CONF_WINDOW_START: (now - timedelta(minutes=10)).isoformat(),
        },
    )
    assert result["step_id"] == "choose_track"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRACK: key}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options[CONF_TRACKS][0]["unique_identity"] == service_id

    async def refresh():
        coordinator = entry.runtime_data.coordinator
        # Avoid waiting for Home Assistant's own request debounce timer.
        monkeypatch.setattr(
            coordinator, "async_request_refresh", coordinator.async_refresh
        )
        await coordinator.async_refresh_tracked()
        await hass.async_block_till_done()

    await refresh()
    state = status_sensor(hass, direction)
    assert state.state == "delayed"
    assert state.attributes["stale"] is False

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
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_track"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRACK: [key]}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert not entry.options[CONF_TRACKS]
    assert hass.states.get(entity_id) is None
    assert registry.async_get(entity_id) is None
