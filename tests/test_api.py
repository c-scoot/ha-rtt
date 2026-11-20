"""Test API response handling and budget admission at the HTTP boundary."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest

from custom_components.realtime_trains.api import (
    RealtimeTrainsApi,
    RttAuthError,
    RttConnectionError,
    RttError,
    RttNotFoundError,
    RttRateLimitError,
)
from custom_components.realtime_trains.budget import BudgetDeferred, RequestBudget

from .rtt_fixtures import (
    CREDENTIAL_MARKER,
    INFO,
    SERVICE_ID,
    board_response,
    location_call,
    response,
    service_response,
)


async def test_empty_board_and_missing_service_do_not_read_a_json_body():
    no_content, empty_reply = response(204)
    not_found, missing_reply = response(404)
    session = MagicMock()
    session.request.side_effect = [no_content, not_found]
    api = RealtimeTrainsApi(session, CREDENTIAL_MARKER)

    board = await api.async_get_location("CLJ", namespace="gb-nr")
    assert board.services == []
    with pytest.raises(RttNotFoundError):
        await api.async_get_service(SERVICE_ID)

    empty_reply.json.assert_not_awaited()
    missing_reply.json.assert_not_awaited()


async def test_round_trip_realistic_board_and_service_payloads():
    scheduled = "2026-10-25T01:55:00+01:00"
    forecast = "2026-10-25T01:05:00+00:00"
    call = location_call(
        departure={"scheduleAdvertised": scheduled, "realtimeForecast": forecast},
        platform={"planned": "5"},
    )
    session = MagicMock()
    session.request.side_effect = [
        response(200, board_response(call))[0],
        response(200, service_response(call))[0],
    ]
    api = RealtimeTrainsApi(session, CREDENTIAL_MARKER)
    board = await api.async_get_location(
        "gb-nr:CLJ", time_from=datetime.fromisoformat(scheduled)
    )
    service = await api.async_get_service(SERVICE_ID)

    (board_item,) = board.services
    assert board_item.schedule_metadata.unique_identity == SERVICE_ID
    (detail,) = service.locations
    assert "CLJ" in detail.location.short_codes
    assert detail.temporal_data.departure.realtime_forecast == datetime.fromisoformat(
        forecast
    )
    request = session.request.call_args_list[0]
    assert urlsplit(request.args[1]).path == "/rtt/location"
    assert request.kwargs["params"] == {"code": "gb-nr:CLJ", "timeFrom": scheduled}
    assert request.kwargs["timeout"].total == 20
    assert request.kwargs["allow_redirects"] is False


async def test_each_auth_request_needs_a_separate_reserved_slot():
    budget = RequestBudget()
    save = AsyncMock()
    for _ in range(8):
        await budget.async_acquire()

    session = MagicMock()
    session.request.side_effect = [
        response(401)[0],
        response(
            200,
            {
                "token": "fake-short-lived-token",
                "validUntil": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )[0],
        response(200, INFO)[0],
    ]
    api = RealtimeTrainsApi(
        session, CREDENTIAL_MARKER, budget=budget, on_budget_change=save
    )

    with pytest.raises(BudgetDeferred):
        await api.async_get_info()

    paths = [urlsplit(call.args[1]).path for call in session.request.call_args_list]
    assert paths == ["/api/info", "/api/get_access_token"]
    assert save.await_count >= len(paths)
    assert len(budget.dump()["requests"]) == 10


async def test_rate_limit_blocks_manual_lookup_and_survives_new_api_instance():
    session = MagicMock()
    limited, _ = response(429, headers={"rEtRy-AfTeR": "90"})
    session.request.return_value = limited
    api = RealtimeTrainsApi(session, CREDENTIAL_MARKER)

    with pytest.raises(RttRateLimitError) as failure:
        await api.async_get_location("CLJ", namespace="gb-nr")
    assert failure.value.retry_after == 90

    restarted = RealtimeTrainsApi(
        session, CREDENTIAL_MARKER, budget=RequestBudget(api.budget.dump())
    )
    with pytest.raises(BudgetDeferred):
        await restarted.async_get_stops()
    session.request.assert_called_once()


async def test_implicit_transport_retry_must_reserve_its_own_slot():
    budget = RequestBudget()
    for _ in range(9):
        await budget.async_acquire()

    session = MagicMock()
    context, reply = response(200, INFO)
    send = AsyncMock(return_value=reply)

    def request(*_args, **kwargs):
        (middleware,) = kwargs["middlewares"]

        async def dispatch():
            transport_request = object()
            await middleware(transport_request, send)
            return await middleware(transport_request, send)

        context.__aenter__.side_effect = dispatch
        return context

    session.request.side_effect = request
    api = RealtimeTrainsApi(session, CREDENTIAL_MARKER, budget=budget)

    with pytest.raises(BudgetDeferred):
        await api.async_get_info()
    send.assert_awaited_once()


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "unknown"}])
async def test_rate_limit_without_retry_after_defers_other_requests(headers):
    session = MagicMock()
    session.request.return_value, _ = response(429, headers=headers)
    api = RealtimeTrainsApi(session, CREDENTIAL_MARKER)
    before = datetime.now(UTC)

    with pytest.raises(RttRateLimitError):
        await api.async_get_stops()
    with pytest.raises(BudgetDeferred) as deferred:
        await api.async_get_location("gb-nr:CLJ")

    assert deferred.value.next_attempt >= before + timedelta(minutes=5)
    session.request.assert_called_once()


@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (403, {"message": CREDENTIAL_MARKER}, RttAuthError),
        (200, ValueError(CREDENTIAL_MARKER), RttError),
        (200, {"services": [{"scheduleMetadata": CREDENTIAL_MARKER}]}, RttError),
        (302, {"message": CREDENTIAL_MARKER}, RttError),
        (503, {"message": CREDENTIAL_MARKER}, RttError),
    ],
)
async def test_errors_do_not_expose_server_text(status, body, error_type):
    session = MagicMock()
    session.request.return_value, _ = response(status, body)
    api = RealtimeTrainsApi(session, CREDENTIAL_MARKER)

    with pytest.raises(error_type) as failure:
        await api.async_get_location("CLJ", namespace="gb-nr")
    assert CREDENTIAL_MARKER not in str(failure.value)


async def test_network_failure_still_consumes_a_request():
    session = MagicMock()
    session.request.side_effect = TimeoutError(CREDENTIAL_MARKER)
    api = RealtimeTrainsApi(session, CREDENTIAL_MARKER)

    with pytest.raises(RttConnectionError) as failure:
        await api.async_get_stops()
    assert CREDENTIAL_MARKER not in str(failure.value)
    assert len(api.budget.dump()["requests"]) == 1
