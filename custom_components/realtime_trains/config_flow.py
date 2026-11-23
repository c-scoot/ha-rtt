"""Set up home stations and choose individual service calls to track."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    RttNotFoundError,
    RttRateLimitError,
)
from .budget import BudgetDeferred
from .const import (
    CONF_DIRECTION,
    CONF_IS_REFRESH_TOKEN,
    CONF_REPEAT_WEEKLY,
    CONF_STATION,
    CONF_STATION_NAME,
    CONF_STATIONS,
    CONF_TOKEN,
    CONF_TRACK,
    CONF_TRACKS,
    CONF_WINDOW_START,
    DOMAIN,
    NAME,
    UK_TIME_ZONE,
)
from .coordinator import Board, BudgetLedger, read_stations, read_tracks
from .models import LocationCallType, ModeType
from .recurrence import route_fingerprint
from .stations import station_labels
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
        """Return the flow for managing home stations and trackers."""
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
                    self._stations = station_labels(stops)
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
        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema(
                {vol.Required(CONF_STATION): _dropdown(self._stations)}
            ),
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


@dataclass(frozen=True, slots=True)
class _Candidate:
    track: TrackedCall
    picker_label: str


def _candidate_calls(
    board: Board, station: str, direction: str, station_name: str
) -> dict[str, _Candidate]:
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
        label_parts = [
            station_name,
            date_label,
            route,
            metadata.identity or metadata.unique_identity,
        ]
        label = " · ".join(filter(None, label_parts))
        track = TrackedCall(
            metadata.unique_identity,
            station,
            direction,
            scheduled.isoformat(),
            label,
            recurrence_match=route_fingerprint(row),
        )
        picker_label = label
        if platform and platform.actual:
            picker_label += f" · Platform {platform.actual} (confirmed)"
        if platform and platform.planned and platform.planned != platform.actual:
            picker_label += f" · Platform {platform.planned} (planned)"
        calls.append(_Candidate(track, picker_label))
    return {
        candidate.track.key: candidate
        for candidate in sorted(
            calls, key=lambda item: (item.track.scheduled, item.track.label)
        )
    }


class RealtimeTrainsOptionsFlow(OptionsFlowWithReload):
    """Manage persistent dated trackers without requiring a custom dashboard."""

    def __init__(self) -> None:
        self._candidates: dict[str, _Candidate] = {}
        self._station_directory: dict[str, str] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer station management and adding or removing train trackers."""
        menu_options = ["add_track", "manage_stations"]
        if read_tracks(self.config_entry):
            menu_options.extend(["weekly_repeats", "remove_track"])
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_manage_stations(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the stations available in the train picker."""
        errors: dict[str, str] = {}
        current = read_stations(self.config_entry)
        if self._station_directory is None:
            runtime = getattr(self.config_entry, "runtime_data", None)
            if runtime is None:
                return self.async_abort(reason="integration_unavailable")
            try:
                directory = await runtime.coordinator.async_get_stations()
            except RttAuthError:
                self.config_entry.async_start_reauth(self.hass)
                errors["base"] = "invalid_auth"
            except (RttError, BudgetDeferred, ValueError) as err:
                errors["base"] = _flow_error(err)
            else:
                if directory:
                    self._station_directory = directory
                else:
                    errors["base"] = "no_stations"

        schema: dict[Any, Any] = {}
        if self._station_directory is not None:
            choices = {**current, **self._station_directory}
            if user_input is not None and CONF_STATIONS in user_input:
                selected = user_input[CONF_STATIONS]
                if not selected:
                    errors[CONF_STATIONS] = "select_station"
                elif any(station not in choices for station in selected):
                    errors[CONF_STATIONS] = "invalid_selection"
                else:
                    return self.async_create_entry(
                        title="",
                        data={
                            **self.config_entry.options,
                            CONF_STATIONS: {
                                station: choices[station] for station in selected
                            },
                        },
                    )
            schema[vol.Required(CONF_STATIONS, default=list(current))] = _dropdown(
                choices,
                multiple=True,
            )
        return self.async_show_form(
            step_id="manage_stations",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

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
        stations = read_stations(self.config_entry)
        station = next(iter(stations))
        if user_input is not None:
            direction = user_input[CONF_DIRECTION]
            now = user_input[CONF_WINDOW_START]
            station = user_input.get(CONF_STATION, station)
            if station not in stations:
                errors[CONF_STATION] = "invalid_selection"
                station = next(iter(stations))
        if user_input is not None and not errors:
            runtime = getattr(self.config_entry, "runtime_data", None)
            if runtime is None:
                return self.async_abort(reason="integration_unavailable")
            try:
                start = _start_time(now)
            except ValueError:
                errors[CONF_WINDOW_START] = "invalid_window"
            else:
                try:
                    board = await runtime.coordinator.async_get_board(start, station)
                except RttAuthError:
                    self.config_entry.async_start_reauth(self.hass)
                    errors["base"] = "invalid_auth"
                except RttBadRequestError:
                    errors[CONF_WINDOW_START] = "invalid_window"
                except (RttError, BudgetDeferred, ValueError) as err:
                    errors["base"] = _flow_error(err)
                else:
                    occupied = self._tracked_call_keys()
                    self._candidates = {
                        key: candidate
                        for key, candidate in _candidate_calls(
                            board, station, direction, stations[station]
                        ).items()
                        if key not in occupied
                    }
                    if self._candidates:
                        return await self.async_step_choose_track()
                    errors["base"] = "no_services"
        schema: dict[Any, Any] = {}
        if len(stations) > 1:
            schema[vol.Optional(CONF_STATION, default=station)] = _dropdown(stations)
        schema.update(
            {
                vol.Required(
                    CONF_DIRECTION, default=direction
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["departure", "arrival"], translation_key="direction"
                    ),
                ),
                vol.Required(
                    CONF_WINDOW_START, default=now
                ): selector.DateTimeSelector(),
            }
        )
        return self.async_show_form(
            step_id="add_track",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"station": stations[station]},
        )

    async def async_step_choose_track(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save the selected train's immutable call details."""
        errors = {}
        repeat_weekly = False
        if user_input is not None:
            repeat_weekly = user_input.get(CONF_REPEAT_WEEKLY, False)
            key = user_input[CONF_TRACK]
            candidate = self._candidates.get(key)
            tracks = read_tracks(self.config_entry)
            if (
                candidate
                and key not in self._tracked_call_keys()
                and candidate.track.station in read_stations(self.config_entry)
            ):
                if repeat_weekly and candidate.track.recurrence_match is None:
                    errors["base"] = "cannot_repeat"
                else:
                    tracks[key] = replace(candidate.track, repeat_weekly=repeat_weekly)
                    return self._save_tracks(tracks)
            else:
                errors[CONF_TRACK] = "invalid_selection"
        return self.async_show_form(
            step_id="choose_track",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRACK): _dropdown(
                        {
                            key: candidate.picker_label
                            for key, candidate in self._candidates.items()
                        }
                    ),
                    vol.Optional(CONF_REPEAT_WEEKLY, default=repeat_weekly): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_weekly_repeats(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enable or stop weekly repeats without replacing existing tracker IDs."""
        tracks = read_tracks(self.config_entry)
        enabled = [key for key, track in tracks.items() if track.repeat_weekly]
        errors: dict[str, str] = {}
        runtime = getattr(self.config_entry, "runtime_data", None)
        if user_input is not None:
            enabled = user_input.get(CONF_TRACK, [])
            if any(key not in tracks for key in enabled):
                errors[CONF_TRACK] = "invalid_selection"
            else:
                fingerprints = {}
                revised = {}
                try:
                    for key, seed in tracks.items():
                        fingerprint = seed.recurrence_match
                        if key in enabled and fingerprint is None:
                            if runtime is None:
                                return self.async_abort(
                                    reason="integration_unavailable"
                                )
                            current = runtime.coordinator.tracks.get(key, seed)
                            identity = current.unique_identity
                            if identity not in fingerprints:
                                detail = await runtime.api.async_get_service(identity)
                                fingerprints[identity] = route_fingerprint(detail)
                            fingerprint = fingerprints[identity]
                            if fingerprint is None:
                                errors["base"] = "cannot_repeat"
                                break
                        revised[key] = replace(
                            seed,
                            repeat_weekly=key in enabled,
                            recurrence_match=fingerprint,
                        )
                except RttAuthError:
                    self.config_entry.async_start_reauth(self.hass)
                    errors["base"] = "invalid_auth"
                except (RttNotFoundError, RttBadRequestError):
                    errors["base"] = "cannot_repeat"
                except (RttError, BudgetDeferred, ValueError) as err:
                    errors["base"] = _flow_error(err)
                if not errors:
                    return self._save_tracks(revised)
        current_tracks = runtime.coordinator.tracks if runtime else tracks
        return self.async_show_form(
            step_id="weekly_repeats",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TRACK, default=enabled): _dropdown(
                        {
                            key: f"{current_tracks.get(key, track).label} · {track.direction}"
                            for key, track in tracks.items()
                        },
                        multiple=True,
                    ),
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

    def _tracked_call_keys(self) -> set[str]:
        """Exclude a recurring rule's current dated occurrence as well as its seed."""
        keys = set(read_tracks(self.config_entry))
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is not None:
            keys.update(
                replace(call, tracking_id=None).key
                for call in runtime.coordinator.tracks.values()
            )
        return keys
