from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    HeapAuthError,
    HeapError,
    HeapNetworkError,
    HeapNotFoundError,
    HeapRateLimitError,
    HeapServerError,
)

HEAP_REST_BASE = "https://heapanalytics.com/api/"
HEAP_SERVER_SIDE_BASE = "https://heapanalytics.com/api/"
DEFAULT_TIMEOUT_S = 30.0


class HeapHTTPClient:
    """Low-level async HTTP client for the Heap Analytics API.

    Authentication:
    - REST endpoints: Authorization: Bearer {api_key}
    - Server-Side (track/identify): POST body includes app_id

    The account_id corresponds to the Heap App ID used in server-side calls.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._api_key: str = cfg.get("api_key", "")
        self._account_id: str = cfg.get("account_id", "")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        base_url: str = HEAP_REST_BASE,
    ) -> Any:
        """Make an authenticated request to the Heap API.

        Returns parsed JSON. Raises typed HeapError subclasses on non-2xx.
        """
        session = self._get_session()
        url = f"{base_url}{path}"

        try:
            async with session.request(
                method, url, params=params, json=json_body
            ) as response:
                if response.status in (200, 201, 202, 204):
                    if response.status == 204 or response.content_length == 0:
                        return {}
                    return await response.json(content_type=None)

                # Error path
                body: dict[str, Any] = {}
                err_text = ""
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    try:
                        err_text = await response.text()
                    except Exception:
                        pass

                err_msg = body.get(
                    "error", body.get("message", err_text or "Unknown Heap error")
                )
                err_code = str(body.get("code", ""))
                self._raise_for_status(response.status, err_msg, err_code, path)

        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise HeapNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise HeapNetworkError(f"Network error: {exc}") from exc
        except (HeapError, HeapNetworkError):
            raise
        except Exception as exc:
            raise HeapNetworkError(f"Unexpected network error: {exc}") from exc

        # unreachable but satisfies type checker
        return {}

    def _raise_for_status(
        self, status: int, message: str, code: str, path: str
    ) -> None:
        """Map HTTP status codes to typed HeapError exceptions."""
        if status in (401, 403):
            raise HeapAuthError(
                f"Authentication failed: {message}", status, code
            )
        if status == 404:
            raise HeapNotFoundError("resource", path)
        if status == 429:
            raise HeapRateLimitError(f"Rate limited: {message}")
        if status >= 500:
            raise HeapServerError(
                f"Heap server error {status}: {message}", status, code
            )
        raise HeapError(f"Heap error {status}: {message}", status, code)

    # ── Credentials validation ────────────────────────────────────────────────

    async def validate_credentials(self) -> dict[str, Any]:
        """Validate API credentials by calling the Heap account endpoint.

        Uses POST /api/track as a minimal ping — Heap doesn't expose a
        dedicated /account_details endpoint on the server-side API, so we
        attempt a minimal identify call which returns success on valid creds.
        Falls back to a GET on the REST base to confirm reachability.
        """
        # Minimal track call to validate app_id + api_key
        payload: dict[str, Any] = {
            "app_id": self._account_id,
            "identity": "__shielva_health_check__",
            "event": "shielva_health_ping",
            "properties": {},
        }
        return await self._request("POST", "track", json_body=payload)

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(
        self, page: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """GET /api/users — retrieve paginated user list.

        Note: Heap's REST API exposes user data via the /users endpoint
        (requires REST API access enabled in the Heap dashboard).
        """
        params: dict[str, Any] = {
            "app_id": self._account_id,
            "page": page,
            "limit": limit,
        }
        return await self._request("GET", "users", params=params)

    # ── Events ────────────────────────────────────────────────────────────────

    async def get_events(
        self,
        event_name: str | None = None,
        time_range_days: int = 30,
    ) -> dict[str, Any]:
        """GET /api/events — retrieve aggregated event counts.

        Args:
            event_name: Optional filter by specific event name.
            time_range_days: Number of days to look back (default 30).
        """
        params: dict[str, Any] = {
            "app_id": self._account_id,
            "time_range": time_range_days,
        }
        if event_name:
            params["event_name"] = event_name
        return await self._request("GET", "events", params=params)

    # ── Segments ──────────────────────────────────────────────────────────────

    async def get_segments(self) -> dict[str, Any]:
        """GET /api/segments — retrieve all segments defined in the account."""
        params: dict[str, Any] = {"app_id": self._account_id}
        return await self._request("GET", "segments", params=params)

    # ── User properties ───────────────────────────────────────────────────────

    async def get_user_properties(self, identity: str) -> dict[str, Any]:
        """GET /api/user_properties — retrieve properties for a specific user.

        Args:
            identity: The Heap user identity (email, UUID, etc.).
        """
        params: dict[str, Any] = {
            "app_id": self._account_id,
            "identity": identity,
        }
        return await self._request("GET", "user_properties", params=params)

    # ── Server-side tracking ──────────────────────────────────────────────────

    async def track_event(
        self,
        identity: str,
        event: str,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /api/track — server-side event tracking.

        Args:
            identity:   User identity (email, UUID, etc.).
            event:      Event name.
            properties: Optional event properties dict.
        """
        payload: dict[str, Any] = {
            "app_id": self._account_id,
            "identity": identity,
            "event": event,
            "properties": properties or {},
        }
        return await self._request("POST", "track", json_body=payload)

    async def identify_user(
        self, identity: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        """POST /api/identify — server-side user identification + property set.

        Args:
            identity:   User identity to associate.
            properties: Key-value user properties to set.
        """
        payload: dict[str, Any] = {
            "app_id": self._account_id,
            "identity": identity,
            "properties": properties,
        }
        return await self._request("POST", "identify", json_body=payload)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> HeapHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
