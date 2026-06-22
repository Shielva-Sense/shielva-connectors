from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    FullStoryAuthError,
    FullStoryError,
    FullStoryNetworkError,
    FullStoryNotFoundError,
    FullStoryRateLimitError,
    FullStoryServerError,
)

FULLSTORY_BASE_URL = "https://api.fullstory.com"
DEFAULT_TIMEOUT_S = 30.0


class FullStoryHTTPClient:
    """Low-level async HTTP client for the FullStory REST API v2.

    Authentication:
    - All requests: Authorization: Bearer {api_key}

    Base URL: https://api.fullstory.com
    All endpoints are under /v2/...
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._api_key: str = cfg.get("api_key", "")
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
    ) -> Any:
        """Make an authenticated request to the FullStory API.

        Returns parsed JSON. Raises typed FullStoryError subclasses on non-2xx.
        """
        session = self._get_session()
        url = f"{FULLSTORY_BASE_URL}{path}"

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
                    "message", body.get("error", err_text or "Unknown FullStory error")
                )
                err_code = str(body.get("code", ""))
                self._raise_for_status(response.status, err_msg, err_code, path)

        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise FullStoryNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise FullStoryNetworkError(f"Network error: {exc}") from exc
        except (FullStoryError, FullStoryNetworkError):
            raise
        except Exception as exc:
            raise FullStoryNetworkError(f"Unexpected network error: {exc}") from exc

        # unreachable but satisfies type checker
        return {}

    def _raise_for_status(
        self, status: int, message: str, code: str, path: str
    ) -> None:
        """Map HTTP status codes to typed FullStoryError exceptions."""
        if status in (401, 403):
            raise FullStoryAuthError(
                f"Authentication failed: {message}", status, code
            )
        if status == 404:
            raise FullStoryNotFoundError("resource", path)
        if status == 429:
            raise FullStoryRateLimitError(f"Rate limited: {message}")
        if status >= 500:
            raise FullStoryServerError(
                f"FullStory server error {status}: {message}", status, code
            )
        raise FullStoryError(f"FullStory error {status}: {message}", status, code)

    # ── Organization (health check) ───────────────────────────────────────────

    async def get_org(self) -> dict[str, Any]:
        """GET /v2/org — retrieve organization info.

        Used as the health-check endpoint to validate credentials.
        """
        return await self._request("GET", "/v2/org")

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def get_sessions(
        self,
        uid: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /v2/sessions — retrieve session recordings.

        Args:
            uid:    Optional FullStory user UID to filter sessions.
            limit:  Maximum number of sessions to return.
            cursor: nextPageToken from a previous response for pagination.
        """
        params: dict[str, Any] = {"limit": limit}
        if uid:
            params["uid"] = uid
        if cursor:
            params["pageToken"] = cursor
        return await self._request("GET", "/v2/sessions", params=params)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """GET /v2/sessions/{session_id} — retrieve a single session recording."""
        return await self._request("GET", f"/v2/sessions/{session_id}")

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(
        self,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /v2/users — retrieve FullStory users.

        Args:
            limit:  Maximum number of users to return.
            cursor: nextPageToken from a previous response for pagination.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["pageToken"] = cursor
        return await self._request("GET", "/v2/users", params=params)

    async def get_user(self, uid: str) -> dict[str, Any]:
        """GET /v2/users/{uid} — retrieve a single FullStory user."""
        return await self._request("GET", f"/v2/users/{uid}")

    # ── Segments ──────────────────────────────────────────────────────────────

    async def get_segments(self, limit: int = 100) -> dict[str, Any]:
        """GET /v2/segments — retrieve all user segments.

        Args:
            limit: Maximum number of segments to return.
        """
        params: dict[str, Any] = {"limit": limit}
        return await self._request("GET", "/v2/segments", params=params)

    # ── Events ────────────────────────────────────────────────────────────────

    async def get_events(self, uid: str, limit: int = 100) -> dict[str, Any]:
        """GET /v2/events?uid={uid} — retrieve custom events for a user.

        Args:
            uid:   FullStory user UID whose events to retrieve.
            limit: Maximum number of events to return.
        """
        params: dict[str, Any] = {"uid": uid, "limit": limit}
        return await self._request("GET", "/v2/events", params=params)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> FullStoryHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
