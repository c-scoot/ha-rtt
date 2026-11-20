"""In-tree HTTP client for the Realtime Trains next-generation API.

This module is HA-agnostic: it accepts an ``aiohttp.ClientSession``
(which HA provides via ``async_get_clientsession``) and returns typed
dataclass models from ``models.py``. The coordinator and config-flow
layers translate the typed exceptions raised here into the right HA
exceptions (``ConfigEntryNotReady``, ``UpdateFailed`` etc.).

The client handles both token types supported by RTT:

* **Long-life access tokens** are used directly.
* **Refresh tokens** are exchanged for short-life access tokens via
  ``/api/get_access_token``. The access token and its ``validUntil``
  are cached and refreshed ``TOKEN_REFRESH_LEAD_TIME`` seconds before
  expiry.

Detection is lazy: the supplied token is first sent to ``/api/info``
as a Bearer. If RTT returns 401/403 the client treats the token as a
refresh token and calls ``/api/get_access_token`` to fetch an access
token. From that point on, all requests carry the access token.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .budget import RequestBudget
from .const import API_VERSION, BASE_URL, TOKEN_REFRESH_LEAD_TIME
from .models import (
    AccessTokenResponse,
    ApiInfo,
    Location,
    LocationLineUpResponse,
    LocationsUngroupedResponse,
    NetworkRailLocationLineUpResponse,
    NetworkRailServiceDetail,
    RateLimitSnapshot,
    ServiceDetail,
    Stop,
    StopsResponse,
)

_LOGGER = logging.getLogger(__name__)

# HTTP statuses that have a JSON body we should attach to the exception.
_NO_CONTENT_STATUS = 204
_AUTH_STATUSES = {401, 403}
_RATE_LIMIT_STATUS = 429
_NOT_FOUND_STATUS = 404
_BAD_REQUEST_STATUS = 400
_ERROR_STATUS_THRESHOLD = 400


class RttError(Exception):
    """Base class for all Realtime Trains API errors."""


class RttAuthError(RttError):
    """Token is missing, invalid, expired or lacks entitlement."""


class RttRateLimitError(RttError):
    """Rate limit exceeded. ``retry_after`` carries the API's hint (seconds)."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        """Store the API's Retry-After hint."""
        super().__init__(message)
        self.retry_after = retry_after


class RttNotFoundError(RttError):
    """The requested resource does not exist."""


class RttBadRequestError(RttError):
    """The request parameters were invalid (HTTP 400)."""


class RttConnectionError(RttError):
    """Network or timeout error reaching data.rtt.io."""


# Endpoint paths used by the client. Held here so tests can match
# against them without re-deriving strings.
PATH_INFO = "/api/info"
PATH_ACCESS_TOKEN = "/api/get_access_token"
PATH_STOPS = "/data/stops"
PATH_LOCATIONS_UNGROUPED = "/data/locations_ungrouped"
PATH_LOCATION_GENERIC = "/rtt/location"
PATH_SERVICE_GENERIC = "/rtt/service"
PATH_LOCATION_GB_NR = "/gb-nr/location"
PATH_SERVICE_GB_NR = "/gb-nr/service"


class RealtimeTrainsApi:
    """HTTP client for the Realtime Trains next-generation API.

    A single instance is held by the account coordinator. Subentry
    coordinators access the API through this instance so all outbound
    requests share the same rate-limit awareness and token-refresh state.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        api_version: str = API_VERSION,
        base_url: str = BASE_URL,
        *,
        budget: RequestBudget | None = None,
        on_budget_change: Callable[[], Awaitable[None]] | None = None,
        is_refresh_token: bool | None = None,
    ) -> None:
        """Set up the client.

        ``token`` is the value pasted by the user. The client lazily
        classifies it as a long-life access token or a refresh token
        based on the response from the first ``/api/info`` call.
        """
        self._session = session
        self._supplied_token = token
        self._api_version = api_version
        self._base_url = base_url.rstrip("/")
        self.budget = budget if budget is not None else RequestBudget()
        self._on_budget_change = on_budget_change
        self._request_lock = asyncio.Lock()
        self._token_lock = asyncio.Lock()

        self._access_token: str | None = None
        self._access_token_valid_until: datetime | None = None
        # Detected on first /api/info failure; ``None`` means "still unknown".
        self._is_refresh_token = is_refresh_token
        self._last_response_headers: dict[str, str] = {}

        # Invariant: populated by ``_capture_rate_limits`` on every response.
        self.rate_limits: RateLimitSnapshot = RateLimitSnapshot()

    # --- Public read-only state -------------------------------------------

    @property
    def last_response_headers(self) -> Mapping[str, str]:
        """Lower-cased headers from the most recent response."""
        return self._last_response_headers

    @property
    def is_refresh_token(self) -> bool | None:
        """Whether the supplied token was classified as a refresh token.

        ``None`` until the first /api/info call has classified the token;
        ``True`` if the token requires a /api/get_access_token exchange
        before each API call; ``False`` if it is a long-life access token.
        """
        return self._is_refresh_token

    # --- Auth & request helpers -------------------------------------------

    async def _bearer_token(self) -> str:
        """Return the access token to use for the next API request.

        Implements the refresh-token fallback: if the supplied token
        has been identified as a refresh token, ensure we hold a
        non-expiring access token before each call.
        """
        async with self._token_lock:
            if self._access_token is None or self._access_token_needs_refresh():
                await self._maybe_refresh_access_token()
        if self._access_token is not None:
            return self._access_token
        # First call before we know which kind of token we have; try
        # the supplied value as an access token first.
        return self._supplied_token

    def _access_token_needs_refresh(self) -> bool:
        if self._access_token_valid_until is None:
            return True
        now = datetime.now(UTC)
        lead = timedelta(seconds=TOKEN_REFRESH_LEAD_TIME)
        return self._access_token_valid_until - lead <= now

    async def _maybe_refresh_access_token(self) -> None:
        """If running as refresh-token mode, fetch a fresh access token."""
        if self._is_refresh_token is False:
            return
        if self._is_refresh_token is None:
            # Don't trigger a refresh before we have classified the token;
            # classification is a side effect of the first /api/info call.
            return
        await self.async_get_access_token()

    async def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, str | int | bool] | None = None,
        *,
        allow_404: bool = False,
        token_override: str | None = None,
    ) -> dict[str, Any] | None:
        """Send a request and return decoded JSON (or None for 204/404)."""
        token = token_override or await self._bearer_token()
        url = f"{self._base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Version": self._api_version,
        }
        # ``aiohttp`` requires all query values to be strings. Coerce ints and
        # bools here rather than at each call site.
        coerced: dict[str, str] | None = None
        if params is not None:
            coerced = {
                k: ("true" if v is True else "false" if v is False else str(v))
                for k, v in params.items()
                if v is not None
            }

        try:
            async with self._request_lock:
                await self.budget.async_acquire()
                if self._on_budget_change is not None:
                    await self._on_budget_change()

                first_send = True

                async def account_retry(request, handler):
                    nonlocal first_send
                    # aiohttp can retry a broken keep-alive connection inside
                    # one request() call. Its client middleware runs per send.
                    if not first_send:
                        await self.budget.async_acquire()
                        if self._on_budget_change is not None:
                            await self._on_budget_change()
                    first_send = False
                    return await handler(request)

                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=coerced,
                    timeout=aiohttp.ClientTimeout(total=20),
                    allow_redirects=False,
                    middlewares=(account_retry,),
                ) as resp:
                    self._capture_rate_limits(resp.headers)
                    observed_headers = dict(resp.headers)
                    if (
                        resp.status == _RATE_LIMIT_STATUS
                        and not self.rate_limits.retry_after
                    ):
                        observed_headers["Retry-After"] = "300"
                    self.budget.observe(observed_headers)
                    if self._on_budget_change is not None:
                        await self._on_budget_change()
                    self._last_response_headers = {
                        k.lower(): v
                        for k, v in resp.headers.items()
                        if k.lower().startswith("x-ratelimit-")
                        or k.lower() == "retry-after"
                    }
                    return await self._handle_response(resp, allow_404=allow_404)
        except aiohttp.ClientResponseError as err:
            # Status raised manually via raise_for_status: should not happen
            # (we read status ourselves) but defensively remap to typed errors.
            await self._raise_for_status(
                err.status, "HTTP request failed", headers=None
            )
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise RttConnectionError("Unable to reach Realtime Trains") from None

    async def _handle_response(
        self, resp: aiohttp.ClientResponse, *, allow_404: bool
    ) -> dict[str, Any] | None:
        status = resp.status
        if status == _NO_CONTENT_STATUS:
            return None
        if status == _NOT_FOUND_STATUS and allow_404:
            return None
        if 300 <= status < 400:
            raise RttError("Unexpected redirect from Realtime Trains")
        # 2xx returns JSON. 4xx/5xx raises a typed exception with the
        # body attached if any is available.
        if status >= _ERROR_STATUS_THRESHOLD:
            await self._raise_for_status_from_response(resp)
        try:
            body: Any = await resp.json(content_type=None)
        except ValueError:
            raise RttError("Unexpected non-JSON response") from None
        if not isinstance(body, dict):
            raise RttError(f"Expected JSON object, got {type(body).__name__}")
        return body

    async def _raise_for_status_from_response(
        self, resp: aiohttp.ClientResponse
    ) -> None:
        status = resp.status
        # Server text can echo credentials; exceptions must never include it.
        message = f"HTTP {status} from Realtime Trains"
        await self._raise_for_status(
            status, message, headers={k.lower(): v for k, v in resp.headers.items()}
        )

    async def _raise_for_status(
        self,
        status: int,
        message: str,
        *,
        headers: Mapping[str, str] | None,
    ) -> None:
        if status in _AUTH_STATUSES:
            raise RttAuthError(message)
        if status == _RATE_LIMIT_STATUS:
            retry_after_raw = (
                (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
                if headers is not None
                else None
            )
            try:
                retry_after = (
                    int(retry_after_raw) if retry_after_raw is not None else None
                )
            except (TypeError, ValueError):
                retry_after = None
            raise RttRateLimitError(message, retry_after=retry_after)
        if status == _NOT_FOUND_STATUS:
            raise RttNotFoundError(message)
        if status == _BAD_REQUEST_STATUS:
            raise RttBadRequestError(message)
        raise RttError(message)

    def _capture_rate_limits(self, headers: Mapping[str, str]) -> None:
        lower: dict[str, str] = {k.lower(): v for k, v in headers.items()}
        self.rate_limits = RateLimitSnapshot.from_headers(lower)
        if self.rate_limits.retry_after is not None:
            _LOGGER.debug(
                "RTT rate limit hit: retry-after=%ss",
                self.rate_limits.retry_after,
            )

    # --- Endpoint wrappers ------------------------------------------------

    async def async_get_info(self) -> ApiInfo:
        """Call ``/api/info`` and return typed credentials + API version.

        If the supplied token is rejected as a Bearer (401/403), the
        client assumes it is a refresh token, calls
        ``/api/get_access_token`` to mint a short-life access token, and
        retries ``/api/info`` with the access token. From that point on,
        the client refreshes the access token before expiry.
        """
        try:
            data = await self._request("GET", PATH_INFO, token_override=None)
        except RttAuthError:
            # First request rejected — the supplied token might be a
            # refresh token rather than a long-life access token. Try
            # the refresh flow exactly once before propagating the auth
            # error.
            if self._is_refresh_token is True:
                # We were already in refresh mode and the refresh token
                # itself was rejected. Surface the original auth failure.
                raise
            await self.async_get_access_token()
            data = await self._request("GET", PATH_INFO, token_override=None)
        if data is None:
            # Per RTT spec /api/info always returns 200; treat 204 as an
            # empty-credentials result rather than crashing.
            data = {}
        # If the supplied token was accepted directly as an access token,
        # cache it so future requests can short-circuit the refresh check.
        if self._access_token is None and self._is_refresh_token is None:
            self._access_token = self._supplied_token
            # No expiry known; treat as long-life (no refresh attempted).
            self._access_token_valid_until = None
            self._is_refresh_token = False
        return _decode(ApiInfo, data)

    async def async_get_access_token(self) -> AccessTokenResponse:
        """Call ``/api/get_access_token`` using the supplied refresh token.

        Caches the returned access token + ``validUntil`` so subsequent
        requests use it directly.
        """
        data = await self._request(
            "GET",
            PATH_ACCESS_TOKEN,
            token_override=self._supplied_token,
        )
        if data is None:
            raise RttAuthError("Refresh-token exchange returned no body")
        token_response = _decode(AccessTokenResponse, data)
        self._access_token = token_response.token
        self._access_token_valid_until = token_response.valid_until
        # We are definitively in refresh-token mode now.
        self._is_refresh_token = True
        return token_response

    async def async_get_stops(self) -> list[Stop]:
        """Fetch the passenger-stops list from ``/data/stops``."""
        data = await self._request("GET", PATH_STOPS)
        if data is None:
            return []
        return _decode(StopsResponse, data).stops

    async def async_get_locations_ungrouped(self) -> list[Location]:
        """Fetch the per-location-list from ``/data/locations_ungrouped``."""
        data = await self._request("GET", PATH_LOCATIONS_UNGROUPED)
        if data is None:
            return []
        return _decode(LocationsUngroupedResponse, data).locations

    async def async_get_location(
        self,
        code: str,
        *,
        filter_from: str | None = None,
        filter_to: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        time_window: int | None = None,
        time_tolerance: bool | None = None,
        detailed: bool | None = None,
        stp_filter: str | None = None,
        namespace: str | None = None,
    ) -> LocationLineUpResponse | NetworkRailLocationLineUpResponse:
        """Fetch a departure board.

        Dispatches to ``/rtt/location`` (generic) or ``/gb-nr/location``
        (Network Rail) based on the ``namespace`` parameter.

        The ``namespace`` may be embedded in ``code`` (e.g.
        ``gb-nr:CLPHMJN``); passing ``namespace="gb-nr"`` with a bare
        ``code`` is equivalent.
        """
        path = PATH_LOCATION_GB_NR if namespace == "gb-nr" else PATH_LOCATION_GENERIC
        params = self._location_params(
            code=code,
            filter_from=filter_from,
            filter_to=filter_to,
            time_from=time_from,
            time_to=time_to,
            time_window=time_window,
            time_tolerance=time_tolerance,
            detailed=detailed,
            stp_filter=stp_filter,
            namespace=namespace,
        )
        data = await self._request("GET", path, params=params)
        if data is None:
            # 204 — empty board. Return a response object with no services.
            empty_cls = (
                NetworkRailLocationLineUpResponse
                if namespace == "gb-nr"
                else LocationLineUpResponse
            )
            return empty_cls.from_dict({})
        if namespace == "gb-nr":
            return _decode(NetworkRailLocationLineUpResponse, data)
        return _decode(LocationLineUpResponse, data)

    def _location_params(
        self,
        *,
        code: str,
        filter_from: str | None,
        filter_to: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
        time_window: int | None,
        time_tolerance: bool | None,
        detailed: bool | None,
        stp_filter: str | None,
        namespace: str | None,
    ) -> dict[str, str | int | bool]:
        params: dict[str, str | int | bool] = {"code": code}
        if filter_from is not None:
            params["filterFrom"] = filter_from
        if filter_to is not None:
            params["filterTo"] = filter_to
        if time_from is not None:
            params["timeFrom"] = _format_dt(time_from)
        if time_to is not None:
            params["timeTo"] = _format_dt(time_to)
        if time_window is not None:
            params["timeWindow"] = time_window
        if time_tolerance is not None:
            params["timeTolerance"] = time_tolerance
        if detailed is not None:
            params["detailed"] = detailed
        if stp_filter is not None:
            params["stpFilter"] = stp_filter
        # ``_`` placed here so callers don't need to brace for the unused
        # ``namespace`` parameter; it's already encoded by the path choice.
        _ = namespace
        return params

    async def async_get_service(
        self,
        unique_identity: str | None = None,
        *,
        identity: str | None = None,
        departure_date: str | None = None,
        namespace: str | None = None,
    ) -> ServiceDetail | NetworkRailServiceDetail:
        """Fetch a single service.

        Dispatches to ``/rtt/service`` (generic) or ``/gb-nr/service``
        (Network Rail, includes allocation/KYT data).

        ``unique_identity`` takes precedence over the
        ``namespace``+``identity``+``departureDate`` triple. RTT expects
        a bare ``uniqueIdentity`` parameter on the Network Rail endpoint
        but accepts ``namespace:identity:date`` form as well.
        """
        path = PATH_SERVICE_GB_NR if namespace == "gb-nr" else PATH_SERVICE_GENERIC
        params: dict[str, str | int | bool] = {}
        if unique_identity is not None:
            params["uniqueIdentity"] = unique_identity
        else:
            if identity is not None:
                params["identity"] = identity
            if departure_date is not None:
                params["departureDate"] = departure_date
            # The generic /rtt/service endpoint accepts a namespace
            # query param; /gb-nr/service does not (the namespace is
            # baked into the path). Sending it there causes a 400.
            if namespace is not None and namespace != "gb-nr":
                params["namespace"] = namespace

        data = await self._request("GET", path, params=params, allow_404=True)
        if data is None:
            raise RttNotFoundError("Service not found")
        if namespace == "gb-nr":
            return _decode(NetworkRailServiceDetail, data)
        return _decode(ServiceDetail, data)


def _decode(model: Any, data: dict[str, Any]) -> Any:
    """Keep malformed nested API data inside the client's typed error boundary."""
    try:
        return model.from_dict(data)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RttError("Unexpected Realtime Trains response structure") from None


def _format_dt(dt: datetime) -> str:
    """Format a datetime as ISO 8601 with the original offset preserved."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()
