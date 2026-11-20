"""Coordinate tracked calls and the shared API request budget."""

from __future__ import annotations

import logging
from asyncio import Lock
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
    CONF_TOKEN,
    CONF_TRACKS,
    DOMAIN,
    ERROR_RETRY_INTERVAL,
    NAME,
    STORE_VERSION,
)
from .models import (
    LocationLineUp,
    NetworkRailLocationLineUp,
    NetworkRailServiceDetail,
    ServiceDetail,
)
from .tracking import TrackedCall, evaluate

_LOGGER = logging.getLogger(__name__)

TrackData = dict[str, dict[str, Any]]
Board = list[LocationLineUp | NetworkRailLocationLineUp]


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
        self.station: str = entry.data[CONF_STATION]
        self.tracks = read_tracks(entry)
        self.ledger = ledger
        self.budget = ledger.budget
        self._store = store
        self._saved = saved
        self._save_lock = Lock()
        self._update_lock = Lock()
        self._board_lock = Lock()
        self._closed = False
        self._force_refresh = False
        self._board_cache: tuple[datetime, datetime, Board] | None = None
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
        self._set_update_interval(self.data, now)

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
            payload = {"tracks": self.data}
            if payload != self._saved:
                await self._store.async_save(payload)
                self._saved = payload

    async def async_get_board(self, start: datetime) -> Board:
        """Return the station line-up from the cache or a new request."""
        async with self._board_lock:
            if self._closed:
                raise RttError("The integration is reloading")
            if self._board_cache is not None:
                cached_start, expires, services = self._board_cache
                if start == cached_start and datetime.now(UTC) < expires:
                    return services
            board = await self.api.async_get_location(
                self.station, time_from=start, time_window=60
            )
            self._board_cache = (
                start,
                datetime.now(UTC) + BOARD_CACHE_TTL,
                board.services,
            )
            return board.services

    async def async_refresh_tracked(self) -> None:
        """Request an early refresh of active trackers through the shared budget."""
        self._force_refresh = True
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        """Quiesce outbound operations before a replacement coordinator can load."""
        self._closed = True
        await super().async_shutdown()
        async with self._board_lock, self._update_lock:
            await self.async_save()

    def _set_update_interval(self, snapshots: TrackData, now: datetime) -> None:
        due_times = [
            datetime.fromisoformat(snapshot["next_poll"])
            for snapshot in snapshots.values()
            if snapshot.get("next_poll") and not snapshot["terminal"]
        ]
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
            services: dict[str, ServiceDetail | NetworkRailServiceDetail | None] = {}
            deferred_until: datetime | None = None
            near_cutoff = datetime.now(UTC) + timedelta(minutes=30)

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
                if previous["terminal"]:
                    continue
                now = datetime.now(UTC)
                next_poll = previous.get("next_poll")
                if not force and next_poll and datetime.fromisoformat(next_poll) > now:
                    continue
                try:
                    if track.unique_identity not in services:
                        if deferred_until is not None:
                            raise BudgetDeferred(deferred_until)
                        try:
                            services[
                                track.unique_identity
                            ] = await self.api.async_get_service(
                                track.unique_identity,
                            )
                        except RttNotFoundError:
                            services[track.unique_identity] = None
                    detail = services[track.unique_identity]
                    received_at = datetime.now(UTC)
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
                    snapshot["last_attempt"] = now.isoformat()
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

            self._set_update_interval(snapshots, datetime.now(UTC))
            # Update the store before returning the new state to listeners.
            self.data = snapshots
            await self.async_save()
            return snapshots

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
