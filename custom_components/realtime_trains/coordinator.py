"""Coordinate tracked calls and the shared API request budget."""

from __future__ import annotations

import logging
from asyncio import Lock
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import (
    RealtimeTrainsApi,
    RttAuthError,
    RttError,
    RttNotFoundError,
    RttRateLimitError,
)
from .budget import BudgetDeferred, RequestBudget
from .const import (
    BOARD_CACHE_TTL,
    CONF_IS_REFRESH_TOKEN,
    CONF_STATION,
    CONF_STATION_NAME,
    CONF_STATIONS,
    CONF_TOKEN,
    CONF_TRACKS,
    DOMAIN,
    ERROR_RETRY_INTERVAL,
    NAME,
    STATIONS_CACHE_TTL,
    STORE_VERSION,
)
from .models import (
    LocationLineUp,
    NetworkRailLocationLineUp,
    NetworkRailServiceDetail,
    ServiceDetail,
)
from .recurrence import (
    RETRY_INTERVAL,
    SEARCH_GRACE,
    SEARCH_LEAD,
    next_week_target,
    resolve_weekly_call,
)
from .stations import station_labels
from .tracking import TrackedCall, evaluate

_LOGGER = logging.getLogger(__name__)

TrackData = dict[str, dict[str, Any]]
Board = list[LocationLineUp | NetworkRailLocationLineUp]


@dataclass(frozen=True, slots=True)
class _WeeklyLookup:
    target: datetime
    next_lookup: datetime
    status: str = "waiting"

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> _WeeklyLookup:
        return cls(
            datetime.fromisoformat(data["target"]),
            datetime.fromisoformat(data["next_lookup"]),
            data["status"],
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "target": self.target.isoformat(),
            "next_lookup": self.next_lookup.isoformat(),
            "status": self.status,
        }

    @property
    def deadline(self) -> datetime:
        return self.target + SEARCH_GRACE


class BudgetLedger:
    """Persist usage from setup, polling and reauthentication under one budget."""

    def __init__(self, store: Store[dict[str, Any]], saved: dict[str, Any]) -> None:
        self.budget = RequestBudget(saved)
        self._store = store
        self._saved = saved
        self._lock = Lock()

    @classmethod
    async def async_load(cls, hass: HomeAssistant) -> BudgetLedger:
        store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.budget")
        return cls(store, await store.async_load() or {})

    async def async_save(self) -> None:
        """Commit physical request reservations before the request goes out."""
        async with self._lock:
            payload = self.budget.dump()
            if payload != self._saved:
                await self._store.async_save(payload)
                self._saved = payload


def read_tracks(entry: ConfigEntry) -> dict[str, TrackedCall]:
    """Restore configured tracks by their immutable call key."""
    tracks = (TrackedCall.from_dict(raw) for raw in entry.options.get(CONF_TRACKS, []))
    return {track.key: track for track in tracks}


def read_stations(entry: ConfigEntry) -> dict[str, str]:
    """Read configured stations, including entries created with one home station."""
    if stations := entry.options.get(CONF_STATIONS):
        return dict(stations)
    station = entry.data[CONF_STATION]
    return {station: entry.data.get(CONF_STATION_NAME, station)}


class RealtimeTrainsCoordinator(DataUpdateCoordinator[TrackData]):
    """Refresh due services and fetch station boards only when requested."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: Store[dict[str, Any]],
        saved: dict[str, Any],
        ledger: BudgetLedger,
    ) -> None:
        super().__init__(hass, _LOGGER, name=NAME, config_entry=entry)
        self.stations = read_stations(entry)
        self.configured_tracks = read_tracks(entry)
        stored_occurrences = saved.get("occurrences", {})
        self.tracks = {
            key: self._restore_occurrence(rule, stored_occurrences.get(key))
            for key, rule in self.configured_tracks.items()
        }
        self._weekly = {
            key: _WeeklyLookup.from_dict(state)
            for key, state in saved.get("weekly", {}).items()
            if key in self.tracks and self.tracks[key].repeat_weekly
        }
        self.ledger = ledger
        self.budget = ledger.budget
        self._store = store
        self._saved = saved
        self._save_lock = Lock()
        self._update_lock = Lock()
        self._board_lock = Lock()
        self._closed = False
        self._force_refresh = False
        self._board_cache: dict[tuple[str, datetime], tuple[datetime, Board]] = {}
        self._stations_cache: tuple[datetime, dict[str, str]] | None = None
        self.api = RealtimeTrainsApi(
            async_get_clientsession(hass),
            entry.data[CONF_TOKEN],
            budget=self.budget,
            on_budget_change=ledger.async_save,
            is_refresh_token=entry.data.get(CONF_IS_REFRESH_TOKEN),
        )
        now = datetime.now(UTC)
        saved_tracks = saved.get("tracks", {})
        self.data = {
            key: saved_tracks.get(key) or self._initial_snapshot(track, now)
            for key, track in self.tracks.items()
        }
        self._apply_weekly_metadata(self.data, self.tracks, self._weekly, now)
        self._set_update_interval(self.data, now)

    @staticmethod
    def _restore_occurrence(
        rule: TrackedCall,
        saved: dict[str, Any] | None,
    ) -> TrackedCall:
        if saved is None:
            return rule
        return replace(
            TrackedCall.from_dict(saved),
            tracking_id=rule.key,
            repeat_weekly=rule.repeat_weekly,
            recurrence_match=rule.recurrence_match,
        )

    @staticmethod
    def _initial_snapshot(track: TrackedCall, now: datetime) -> dict[str, Any]:
        # Fetch once even if someone selects a historical service.
        scheduled = datetime.fromisoformat(track.scheduled)
        snapshot = evaluate(track, [], now=min(now, scheduled))
        snapshot.update(
            stale=True, last_success=None, last_error=None, next_poll=now.isoformat()
        )
        return snapshot

    async def async_save(self) -> None:
        """Persist track snapshots across reloads and restarts."""
        async with self._save_lock:
            payload = {
                "tracks": self.data,
                "occurrences": {
                    key: call.to_dict() for key, call in self.tracks.items()
                },
                "weekly": {
                    key: lookup.to_dict() for key, lookup in self._weekly.items()
                },
            }
            if payload != self._saved:
                await self._store.async_save(payload)
                self._saved = payload

    async def async_get_board(
        self, start: datetime, station: str | None = None
    ) -> Board:
        """Return the station line-up from the cache or a new request."""
        if station is None:
            station = next(iter(self.stations))
        if station not in self.stations:
            raise ValueError("Station is not configured")
        return await self._async_get_station_board(start, station)

    async def _async_get_station_board(self, start: datetime, station: str) -> Board:
        # Saved trackers stay active after their station leaves the picker.
        async with self._board_lock:
            if self._closed:
                raise RttError("The integration is reloading")
            now = datetime.now(UTC)
            key = (station, start)
            self._board_cache = {
                cached_key: (expires, services)
                for cached_key, (expires, services) in self._board_cache.items()
                if now < expires
            }
            if cached := self._board_cache.get(key):
                return cached[1]
            board = await self.api.async_get_location(
                station, time_from=start, time_window=60
            )
            self._board_cache[key] = (
                datetime.now(UTC) + BOARD_CACHE_TTL,
                board.services,
            )
            return board.services

    async def async_get_stations(self) -> dict[str, str]:
        """Fetch the passenger station directory when the options flow needs it."""
        async with self._board_lock:
            if self._closed:
                raise RttError("The integration is reloading")
            if self._stations_cache is not None:
                expires, stations = self._stations_cache
                if datetime.now(UTC) < expires:
                    return dict(stations)
            stations = station_labels(await self.api.async_get_stops())
            if stations:
                self._stations_cache = (
                    datetime.now(UTC) + STATIONS_CACHE_TTL,
                    stations,
                )
            return dict(stations)

    async def async_refresh_tracked(self) -> None:
        """Request an early refresh of active trackers through the shared budget."""
        self._force_refresh = True
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        """Quiesce outbound operations before a replacement coordinator can load."""
        self._closed = True
        await super().async_shutdown()
        async with self._update_lock, self._board_lock:
            await self.async_save()

    def _set_update_interval(self, snapshots: TrackData, now: datetime) -> None:
        due_times = [
            datetime.fromisoformat(snapshot["next_poll"])
            for snapshot in snapshots.values()
            if snapshot.get("next_poll") and not snapshot["terminal"]
        ]
        due_times.extend(
            datetime.fromisoformat(snapshot["weekly_next_check"])
            for snapshot in snapshots.values()
            if snapshot.get("weekly_next_check")
        )
        self.update_interval = (
            max(timedelta(seconds=1), min(due_times) - now) if due_times else None
        )

    async def _async_update_data(self) -> TrackData:
        async with self._update_lock:
            if self._closed:
                return self.data
            force = self._force_refresh
            self._force_refresh = False
            snapshots = dict(self.data)
            services: dict[
                str,
                tuple[
                    datetime,
                    ServiceDetail | NetworkRailServiceDetail | None | Exception,
                ],
            ] = {}
            deferred_until: datetime | None = None
            cycle_start = datetime.now(UTC)
            near_cutoff = cycle_start + timedelta(minutes=30)
            due_services = {
                self.tracks[key].unique_identity
                for key, snapshot in snapshots.items()
                if not snapshot["terminal"]
                and (
                    force
                    or not snapshot.get("next_poll")
                    or datetime.fromisoformat(snapshot["next_poll"]) <= cycle_start
                )
            }

            def priority(key: str) -> tuple[bool, str]:
                snapshot = snapshots[key]
                relevant_time = min(
                    snapshot["scheduled"],
                    snapshot.get("expected") or snapshot["scheduled"],
                )
                far = datetime.fromisoformat(relevant_time) > near_cutoff
                return far, snapshot.get("last_attempt", "")

            for key in sorted(self.tracks, key=priority):
                if self._closed:
                    break
                track = self.tracks[key]
                previous = snapshots[key]
                identity = track.unique_identity
                if previous["terminal"] or identity not in due_services:
                    continue
                now = datetime.now(UTC)
                try:
                    if identity not in services:
                        if deferred_until is not None:
                            raise BudgetDeferred(deferred_until)
                        try:
                            detail = await self.api.async_get_service(identity)
                        except RttNotFoundError:
                            detail = None
                        except (RttError, BudgetDeferred, ValueError) as err:
                            detail = err
                        services[identity] = (datetime.now(UTC), detail)
                    received_at, detail = services[identity]
                    if isinstance(detail, Exception):
                        raise detail
                    snapshot = evaluate(
                        track,
                        detail.locations if detail else (),
                        reasons=detail.reasons if detail else (),
                        now=received_at,
                        previous=previous,
                    )
                    if snapshot["matched_call"]:
                        snapshot.update(
                            stale=snapshot["status"] == "no_report",
                            last_success=received_at.isoformat(),
                            last_error=None,
                        )
                    else:
                        snapshot.update(
                            stale=True,
                            last_error="call_not_found"
                            if detail
                            else "service_not_found",
                        )
                    snapshot["last_attempt"] = received_at.isoformat()
                except BudgetDeferred as err:
                    deferred_until = err.next_attempt
                    snapshot = self._deferred_snapshot(
                        track, previous, deferred_until, "rate_limited"
                    )
                except RttRateLimitError as err:
                    deferred_until = datetime.now(UTC) + (
                        timedelta(seconds=err.retry_after)
                        if err.retry_after is not None
                        else ERROR_RETRY_INTERVAL
                    )
                    snapshot = self._deferred_snapshot(
                        track, previous, deferred_until, "rate_limited"
                    )
                    snapshot["last_attempt"] = now.isoformat()
                except RttAuthError as err:
                    for active_key, active in snapshots.items():
                        if not active["terminal"]:
                            snapshots[active_key] = {
                                **active,
                                "stale": True,
                                "last_error": "invalid_auth",
                                "next_poll": now.isoformat(),
                            }
                    self.data = snapshots
                    await self.async_save()
                    raise ConfigEntryAuthFailed(
                        "RTT rejected the configured token"
                    ) from err
                except (RttError, ValueError):
                    snapshot = self._deferred_snapshot(
                        track,
                        previous,
                        datetime.now(UTC) + ERROR_RETRY_INTERVAL,
                        "request_failed",
                    )
                    snapshot["last_attempt"] = now.isoformat()
                snapshots[key] = snapshot

            occurrences = dict(self.tracks)
            weekly = dict(self._weekly)
            self._apply_weekly_metadata(
                snapshots, occurrences, weekly, datetime.now(UTC)
            )
            try:
                await self._async_roll_weekly(snapshots, occurrences, weekly)
            except RttAuthError as err:
                await self._async_commit(snapshots, occurrences, weekly)
                raise ConfigEntryAuthFailed(
                    "RTT rejected the configured token"
                ) from err
            await self._async_commit(snapshots, occurrences, weekly)
            return snapshots

    @staticmethod
    def _apply_weekly_metadata(
        snapshots: TrackData,
        occurrences: dict[str, TrackedCall],
        weekly: dict[str, _WeeklyLookup],
        now: datetime,
    ) -> None:
        for key, call in occurrences.items():
            snapshot = snapshots[key]
            target = None
            next_check = None
            state = None
            if call.repeat_weekly and call.recurrence_match:
                if not snapshot["terminal"]:
                    weekly.pop(key, None)
                lookup = weekly.get(key)
                if lookup is None:
                    target_time = next_week_target(
                        datetime.fromisoformat(call.scheduled), now
                    )
                    if snapshot["terminal"]:
                        lookup = weekly[key] = _WeeklyLookup(
                            target_time, target_time - SEARCH_LEAD
                        )
                else:
                    target_time = lookup.target
                target = target_time.isoformat()
                state = lookup.status if lookup else "tracking"
                next_check = lookup.next_lookup.isoformat() if lookup else None
            else:
                weekly.pop(key, None)
                if call.repeat_weekly:
                    state = "missing_match"

            snapshots[key] = {
                **snapshot,
                "repeat_weekly": call.repeat_weekly,
                "weekly_next_scheduled": target,
                "weekly_next_check": next_check,
                "weekly_state": state,
            }

    async def _async_roll_weekly(
        self,
        snapshots: TrackData,
        occurrences: dict[str, TrackedCall],
        weekly: dict[str, _WeeklyLookup],
    ) -> None:
        for key, lookup in tuple(weekly.items()):
            if self._closed:
                return
            now = datetime.now(UTC)
            if now > lookup.deadline:
                target = next_week_target(lookup.target, now)
                lookup = weekly[key] = _WeeklyLookup(target, target - SEARCH_LEAD)
            if lookup.next_lookup > now:
                continue

            call = occurrences[key]
            try:
                board = await self._async_get_station_board(
                    lookup.target - timedelta(minutes=1),
                    call.station,
                )
                match = resolve_weekly_call(
                    call,
                    lookup.target,
                    board,
                    station_name=self.stations.get(call.station),
                )
            except BudgetDeferred as err:
                retry_at, status = err.next_attempt, "rate_limited"
            except RttRateLimitError as err:
                retry_at = datetime.now(UTC) + (
                    timedelta(seconds=err.retry_after)
                    if err.retry_after is not None
                    else RETRY_INTERVAL
                )
                status = "rate_limited"
            except RttAuthError:
                weekly[key] = replace(lookup, status="invalid_auth")
                raise
            except (RttError, ValueError):
                retry_at, status = datetime.now(UTC) + RETRY_INTERVAL, "request_failed"
            else:
                if match is not None:
                    occurrences[key] = match
                    snapshots[key] = self._initial_snapshot(match, datetime.now(UTC))
                    del weekly[key]
                    continue
                retry_at, status = datetime.now(UTC) + RETRY_INTERVAL, "no_match"

            weekly[key] = replace(
                lookup,
                next_lookup=min(lookup.deadline, retry_at),
                status=status,
            )

    async def _async_commit(
        self,
        snapshots: TrackData,
        occurrences: dict[str, TrackedCall],
        weekly: dict[str, _WeeklyLookup],
    ) -> None:
        now = datetime.now(UTC)
        self._apply_weekly_metadata(snapshots, occurrences, weekly, now)
        self._set_update_interval(snapshots, now)
        # A saved snapshot must refer to the dated occurrence saved alongside it.
        self.tracks, self._weekly, self.data = occurrences, weekly, snapshots
        await self.async_save()

    @staticmethod
    def _deferred_snapshot(
        track: TrackedCall,
        previous: dict[str, Any],
        retry_at: datetime,
        error: str,
    ) -> dict[str, Any]:
        snapshot = evaluate(track, [], now=datetime.now(UTC), previous=previous)
        if not snapshot["terminal"]:
            expires_at = datetime.fromisoformat(snapshot["expires_at"])
            snapshot["next_poll"] = min(expires_at, retry_at).isoformat()
        snapshot.update(stale=True, last_error=error)
        return snapshot


def entry_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
    """Return the persistent store associated with one config entry."""
    return Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
