from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    WebexAuthError,
    WebexError,
    WebexNetworkError,
    WebexNotFoundError,
    WebexRateLimitError,
)

WEBEX_BASE_URL = "https://webexapis.com/v1/"
DEFAULT_TIMEOUT_S = 30.0


class WebexHTTPClient:
    """Low-level async HTTP client for the Webex REST API."""

    def __init__(
        self,
        access_token: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=WEBEX_BASE_URL,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        session = self._get_session()
        # aiohttp base_url requires paths without leading slash to resolve correctly
        normalized_path = path.lstrip("/")
        try:
            async with session.request(method, normalized_path, params=params) as response:
                return await self._raise_for_status(response)
        except (WebexError,):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise WebexNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise WebexNetworkError(f"Network error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise WebexNetworkError(f"Client error: {exc}") from exc

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status in (200, 201, 204):
            if status == 204:
                return {}
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_msg = (
            body.get("message")
            or body.get("error")
            or body.get("errors", [{}])[0].get("description", "")
            or "Unknown Webex error"
        )
        err_code = str(body.get("errorCode", ""))

        if status == 401:
            raise WebexAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if status == 403:
            raise WebexAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if status == 404:
            raise WebexNotFoundError(err_msg or "resource", str(response.url))
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise WebexRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if status >= 500:
            raise WebexError(
                f"Webex server error {status}: {err_msg}",
                status,
                err_code,
            )

        raise WebexError(
            f"Webex error {status}: {err_msg}",
            status,
            err_code,
        )

    # ── Auth probe ────────────────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """Probe endpoint: GET /people/me — used for install and health check."""
        return await self._request("GET", "/people/me")

    # ── Rooms (Spaces) ────────────────────────────────────────────────────────

    async def get_rooms(
        self,
        max: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /rooms — list spaces/rooms with optional cursor pagination."""
        params: dict[str, Any] = {"max": max}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/rooms", params=params)

    async def get_room(self, room_id: str) -> dict[str, Any]:
        """GET /rooms/{room_id} — retrieve a single room."""
        return await self._request("GET", f"/rooms/{room_id}")

    # ── Messages ──────────────────────────────────────────────────────────────

    async def get_messages(
        self,
        room_id: str,
        max: int = 100,
        before_message: str | None = None,
    ) -> dict[str, Any]:
        """GET /messages?roomId={room_id} — list messages in a room."""
        params: dict[str, Any] = {"roomId": room_id, "max": max}
        if before_message:
            params["beforeMessage"] = before_message
        return await self._request("GET", "/messages", params=params)

    # ── Meetings ──────────────────────────────────────────────────────────────

    async def get_meetings(
        self,
        cursor: str | None = None,
        from_date: str | None = None,
    ) -> dict[str, Any]:
        """GET /meetings — list meetings with optional cursor and date filter."""
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if from_date:
            params["from"] = from_date
        return await self._request("GET", "/meetings", params=params)

    # ── People ────────────────────────────────────────────────────────────────

    async def get_people(
        self,
        cursor: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """GET /people — list people with optional cursor and email filter."""
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if email:
            params["email"] = email
        return await self._request("GET", "/people", params=params)

    # ── Memberships ───────────────────────────────────────────────────────────

    async def get_memberships(
        self,
        room_id: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /memberships — list room memberships."""
        params: dict[str, Any] = {}
        if room_id:
            params["roomId"] = room_id
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/memberships", params=params)

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> WebexHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
