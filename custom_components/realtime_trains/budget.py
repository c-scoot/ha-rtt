"""Shared rolling request limits for a single RTT credential."""

from __future__ import annotations

from bisect import bisect_right, insort
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .models import RateLimitSnapshot

# Duration in seconds, followed by the maximum number of physical requests.
WINDOWS = {
    "minute": (60, 10),
    "hour": (3600, 100),
    "day": (86400, 1000),
    "week": (604800, 10000),
}


class BudgetDeferred(Exception):
    """The request can be reconsidered at the given UTC instant."""

    def __init__(self, next_attempt: datetime) -> None:
        self.next_attempt = next_attempt
        super().__init__(f"Request budget exhausted until {next_attempt.isoformat()}")


class RequestBudget:
    """Reserve before each HTTP attempt, including failed attempts and token exchanges.

    All mutations run on the HA event loop. Admission contains no suspension point,
    so concurrent callers cannot reserve the same slot.
    """

    def __init__(self, state: Mapping[str, Any] | None = None) -> None:
        state = state or {}
        self._requests: list[float] = sorted(state.get("requests", []))
        self._server_limits: dict[str, int] = dict(state.get("server_limits", {}))
        self._server_windows: dict[str, dict[str, Any]] = {
            dimension: dict(window)
            for dimension, window in state.get("server_windows", {}).items()
        }
        self._retry_until: float = state.get("retry_until", 0)

    def dump(self) -> dict[str, Any]:
        """Return a detached JSON snapshot suitable for persistent storage."""
        return {
            "requests": list(self._requests),
            "server_limits": dict(self._server_limits),
            "server_windows": {
                dimension: dict(window)
                for dimension, window in self._server_windows.items()
            },
            "retry_until": self._retry_until,
        }

    async def async_acquire(self, *, now: datetime | None = None) -> None:
        """Reserve one slot atomically or raise without consuming quota."""
        instant = (now or datetime.now(UTC)).timestamp()
        del self._requests[: bisect_right(self._requests, instant - WINDOWS["week"][0])]
        earliest = self._retry_until
        for dimension, (duration, ceiling) in WINDOWS.items():
            limit = min(ceiling, self._server_limits.get(dimension, ceiling))
            if len(self._requests) >= limit:
                earliest = max(earliest, self._requests[-limit] + duration)
            server_window = self._server_windows.get(dimension)
            if server_window and server_window["remaining"] == 0:
                earliest = max(earliest, server_window["expires"])
        if earliest > instant:
            raise BudgetDeferred(datetime.fromtimestamp(earliest, UTC))

        insort(self._requests, instant)
        for window in self._server_windows.values():
            if window["expires"] > instant:
                window["remaining"] -= 1

    def observe(
        self, headers: Mapping[str, str], *, now: datetime | None = None
    ) -> None:
        """Account for stricter server quotas, including usage outside HA."""
        instant = (now or datetime.now(UTC)).timestamp()
        snapshot = RateLimitSnapshot.from_headers(
            {key.lower(): value for key, value in headers.items()}
        )
        retry_at = instant + (snapshot.retry_after or 0)
        self._retry_until = max(self._retry_until, retry_at)
        for dimension, (duration, _) in WINDOWS.items():
            entry = getattr(snapshot, dimension)
            if entry is None:
                continue
            if entry.limit is not None and entry.limit > 0:
                self._server_limits[dimension] = entry.limit
            if entry.remaining is None:
                continue
            remaining = max(entry.remaining, 0)
            # No reset time is documented. Allow one full dimension for the
            # advertised allowance to replenish unless RTT gives a retry time.
            expires = instant + duration
            if remaining == 0 and retry_at > instant:
                expires = retry_at
            self._server_windows[dimension] = {
                "remaining": remaining,
                "expires": expires,
            }
