"""Set up a home station and choose individual service calls to track."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    RealtimeTrainsApi,
    RttAuthError,
    RttBadRequestError,
    RttError,
    RttRateLimitError,
)
from .budget import BudgetDeferred
from .const import (
    CONF_DIRECTION,
    CONF_IS_REFRESH_TOKEN,
    CONF_STATION,
    CONF_STATION_NAME,
    CONF_TOKEN,
    CONF_TRACK,
    CONF_TRACKS,
    CONF_WINDOW_START,
    DOMAIN,
    NAME,
    UK_TIME_ZONE,
)
from .coordinator import Board, BudgetLedger, read_tracks
from .models import LocationCallType, ModeType
from .tracking import TrackedCall


def _token_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_TOKEN): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


def _dropdown(
    options: dict[str, str], *, multiple: bool = False
) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=key, label=label)
                for key, label in options.items()
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
            multiple=multiple,
        )
    )


def _flow_error(error: Exception) -> str:
    if isinstance(error, RttAuthError):
        return "invalid_auth"
    if isinstance(error, (BudgetDeferred, RttRateLimitError)):
        return "rate_limited"
    return "cannot_connect"


class RealtimeTrainsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Validate a user's API credential and ask for a UK home station."""

    VERSION = 1

    def __init__(self) -> None:
        self._credential_data: dict[str, Any] = {}
        self._stations: dict[str, str] = {}
        self._ledger: BudgetLedger | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        """Return the flow for adding and removing trackers."""
        return RealtimeTrainsOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and validate the setup token."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            if not token:
                errors[CONF_TOKEN] = "invalid_auth"
            else:
                if self._ledger is None:
                    self._ledger = await BudgetLedger.async_load(self.hass)
                api = RealtimeTrainsApi(
                    async_get_clientsession(self.hass),
                    token,
                    budget=self._ledger.budget,
                    on_budget_change=self._ledger.async_save,
                )
                try:
                    await api.async_get_info()
                    stops = await api.async_get_stops()
                except (RttError, BudgetDeferred, ValueError) as err:
                    errors["base"] = _flow_error(err)
                else:
                    self._stations = {
                        stop.unique_identity or f"gb-nr:{stop.short_code}": (
                            f"{stop.description} ({stop.short_code})"
                            if stop.description
                            else stop.short_code
                        )
                        for stop in stops
                        if stop.namespace == "gb-nr" and stop.short_code
                    }
                    if self._stations:
                        self._credential_data = {
                            CONF_TOKEN: token,
                            CONF_IS_REFRESH_TOKEN: api.is_refresh_token,
                        }
                        return await self.async_step_station()
                    errors["base"] = "no_stations"
        return self.async_show_form(
            step_id="user", data_schema=_token_schema(), errors=errors
        )

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display the searchable station list."""
        errors = {}
        if user_input is not None:
            station = user_input[CONF_STATION]
            if station in self._stations:
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=NAME,
                    data={
                        **self._credential_data,
                        CONF_STATION: station,
                        CONF_STATION_NAME: self._stations[station],
                    },
                )
            errors[CONF_STATION] = "invalid_selection"
        stations = dict(
            sorted(self._stations.items(), key=lambda option: option[1].casefold())
        )
        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema({vol.Required(CONF_STATION): _dropdown(stations)}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start credential replacement for the existing station and tracks."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement credentials while preserving the shared budget."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            token = user_input[CONF_TOKEN].strip()
            if not token:
                errors[CONF_TOKEN] = "invalid_auth"
            else:
                runtime = getattr(entry, "runtime_data", None)
                ledger = (
                    runtime.coordinator.ledger
                    if runtime
                    else await BudgetLedger.async_load(self.hass)
                )
                api = RealtimeTrainsApi(
                    async_get_clientsession(self.hass),
                    token,
                    budget=ledger.budget,
                    on_budget_change=ledger.async_save,
                )
                try:
                    await api.async_get_info()
                except (RttError, BudgetDeferred, ValueError) as err:
                    errors["base"] = _flow_error(err)
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_TOKEN: token,
                            CONF_IS_REFRESH_TOKEN: api.is_refresh_token,
                        },
                    )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_token_schema(), errors=errors
        )


def _start_time(value: str) -> datetime:
    start = datetime.fromisoformat(value)
    if start.tzinfo is not None:
        return start.astimezone(UTC)
    stamp = start.replace(tzinfo=UK_TIME_ZONE).astimezone(UTC)
    if stamp.astimezone(UK_TIME_ZONE).replace(tzinfo=None) != start:
        raise ValueError("Invalid local time")
    return stamp


def _candidate_calls(
    board: Board, station: str, direction: str
) -> dict[str, TrackedCall]:
    excluded_call_type = (
        LocationCallType.ADVERTISED_SET_DOWN
        if direction == "departure"
        else LocationCallType.ADVERTISED_PICK_UP
    )
    calls = []
    for row in board:
        metadata = row.schedule_metadata
        timing = row.temporal_data
        if (
            metadata is None
            or not metadata.unique_identity
            or metadata.in_passenger_service is False
            or metadata.mode_type not in (None, ModeType.TRAIN)
            or timing is None
            or timing.scheduled_call_type
            in (LocationCallType.OPERATIONAL_ONLY, excluded_call_type)
        ):
            continue
        activity = timing.arrival if direction == "arrival" else timing.departure
        if activity is None or activity.schedule_advertised is None:
            continue
        scheduled = activity.schedule_advertised.astimezone(UTC)
        destinations = row.origin if direction == "arrival" else row.destination
        route = " / ".join(
            dict.fromkeys(
                pair.location.description
                for pair in destinations
                if pair.location and pair.location.description
            )
        )
        date_label = scheduled.astimezone(UK_TIME_ZONE).strftime("%d %b %Y %H:%M %Z")
        platform = row.location_metadata.platform if row.location_metadata else None
        platform_value = (platform.actual or platform.planned) if platform else None
        label_parts = [date_label, route, metadata.identity or metadata.unique_identity]
        if platform_value:
            label_parts.append(platform_value)
        label = " · ".join(filter(None, label_parts))
        calls.append(
            TrackedCall(
                metadata.unique_identity,
                station,
                direction,
                scheduled.isoformat(),
                label,
            )
        )
    return {
        call.key: call
        for call in sorted(calls, key=lambda call: (call.scheduled, call.label))
    }


class RealtimeTrainsOptionsFlow(OptionsFlowWithReload):
    """Manage persistent dated trackers without requiring a custom dashboard."""

    def __init__(self) -> None:
        self._candidates: dict[str, TrackedCall] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer add and remove operations."""
        menu_options = ["add_track"]
        if read_tracks(self.config_entry):
            menu_options.append("remove_track")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_add_track(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Query the one-hour station board for the selected date and time."""
        errors: dict[str, str] = {}
        now = (
            datetime.now(UK_TIME_ZONE)
            .replace(
                second=0,
                microsecond=0,
                tzinfo=None,
            )
            .isoformat(sep=" ")
        )
        direction = "departure"
        if user_input is not None:
            direction = user_input[CONF_DIRECTION]
            now = user_input[CONF_WINDOW_START]
            runtime = getattr(self.config_entry, "runtime_data", None)
            if runtime is None:
                return self.async_abort(reason="integration_unavailable")
            try:
                start = _start_time(now)
            except ValueError:
                errors[CONF_WINDOW_START] = "invalid_window"
            else:
                try:
                    board = await runtime.coordinator.async_get_board(start)
                except RttAuthError:
                    self.config_entry.async_start_reauth(self.hass)
                    errors["base"] = "invalid_auth"
                except RttBadRequestError:
                    errors[CONF_WINDOW_START] = "invalid_window"
                except (RttError, BudgetDeferred, ValueError) as err:
                    errors["base"] = _flow_error(err)
                else:
                    station = self.config_entry.data[CONF_STATION]
                    tracks = read_tracks(self.config_entry)
                    self._candidates = {
                        key: call
                        for key, call in _candidate_calls(
                            board, station, direction
                        ).items()
                        if key not in tracks
                    }
                    if self._candidates:
                        return await self.async_step_choose_track()
                    errors["base"] = "no_services"
        return self.async_show_form(
            step_id="add_track",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DIRECTION, default=direction
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["departure", "arrival"],
                            translation_key="direction",
                        ),
                    ),
                    vol.Required(
                        CONF_WINDOW_START, default=now
                    ): selector.DateTimeSelector(),
                }
            ),
            errors=errors,
            description_placeholders={
                "station": self.config_entry.data[CONF_STATION_NAME]
            },
        )

    async def async_step_choose_track(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save the selected train's immutable call details."""
        errors = {}
        if user_input is not None:
            key = user_input[CONF_TRACK]
            track = self._candidates.get(key)
            tracks = read_tracks(self.config_entry)
            if track and key not in tracks:
                tracks[key] = track
                return self._save_tracks(tracks)
            errors[CONF_TRACK] = "invalid_selection"
        return self.async_show_form(
            step_id="choose_track",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRACK): _dropdown(
                        {key: track.label for key, track in self._candidates.items()}
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_remove_track(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove one or more saved trackers."""
        tracks = read_tracks(self.config_entry)
        if user_input is not None:
            removed = set(user_input[CONF_TRACK])
            return self._save_tracks(
                {key: track for key, track in tracks.items() if key not in removed}
            )
        return self.async_show_form(
            step_id="remove_track",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRACK): _dropdown(
                        {
                            key: f"{track.label} · {track.direction}"
                            for key, track in tracks.items()
                        },
                        multiple=True,
                    )
                }
            ),
        )

    def _save_tracks(self, tracks: dict[str, TrackedCall]) -> ConfigFlowResult:
        return self.async_create_entry(
            title="",
            data={
                **self.config_entry.options,
                CONF_TRACKS: [track.to_dict() for track in tracks.values()],
            },
        )
