from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    DialpadAuthError,
    DialpadError,
    DialpadNetworkError,
    DialpadNotFoundError,
    DialpadRateLimitError,
)

DIALPAD_BASE_URL = "https://dialpad.com"
DIALPAD_OAUTH_TOKEN_URL = "https://dialpad.com/oauth2/token"
DEFAULT_TIMEOUT_S = 30.0


class DialpadHTTPClient:
    """Low-level async HTTP client for the Dialpad REST API v2."""

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
                base_url=DIALPAD_BASE_URL,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        session = self._get_session()
        try:
            async with session.request(method, path, **kwargs) as response:
                return await self._raise_for_status(response, path)
        except DialpadError:
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise DialpadNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            raise DialpadNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise DialpadNetworkError(f"Network error: {exc}") from exc

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse, path: str
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
            or body.get("error_description")
            or str(await response.text() if not body else "")
            or "Unknown Dialpad error"
        )
        err_code = str(body.get("code", ""))

        if status == 401:
            raise DialpadAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if status == 403:
            raise DialpadAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if status == 404:
            raise DialpadNotFoundError(body.get("message", "resource"), str(path))
        if status == 429:
            retry_after_raw = response.headers.get("Retry-After", "0")
            try:
                retry_after = float(retry_after_raw)
            except ValueError:
                retry_after = 0.0
            raise DialpadRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if status >= 500:
            raise DialpadError(
                f"Dialpad server error {status}: {err_msg}",
                status,
                err_code,
            )

        raise DialpadError(
            f"Dialpad error {status}: {err_msg}",
            status,
            err_code,
        )

    # ── Auth probe ───────────────────────────────────────────────────────────

    async def get_user(self) -> dict[str, Any]:
        """GET /api/v2/users/me — current authenticated user."""
        return await self._request("GET", "/api/v2/users/me")

    # ── Users ────────────────────────────────────────────────────────────────

    async def get_users(self, cursor: str | None = None) -> dict[str, Any]:
        """GET /api/v2/users — paginated list of users."""
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/api/v2/users", params=params)

    # ── Call logs ────────────────────────────────────────────────────────────

    async def get_call_logs(
        self,
        cursor: str | None = None,
        started_after: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/call — paginated call logs."""
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if started_after:
            params["started_after"] = started_after
        return await self._request("GET", "/api/v2/call", params=params)

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def get_contacts(self, cursor: str | None = None) -> dict[str, Any]:
        """GET /api/v2/contacts — paginated contacts."""
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/api/v2/contacts", params=params)

    # ── Departments ──────────────────────────────────────────────────────────

    async def get_departments(self, cursor: str | None = None) -> dict[str, Any]:
        """GET /api/v2/departments — paginated departments."""
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/api/v2/departments", params=params)

    # ── Phone numbers ────────────────────────────────────────────────────────

    async def get_numbers(self) -> dict[str, Any]:
        """GET /api/v2/numbers — phone numbers assigned to the account."""
        return await self._request("GET", "/api/v2/numbers")

    # ── Token refresh ────────────────────────────────────────────────────────

    async def refresh_token(
        self,
        client_id: str,
        client_secret: str,
        refresh_token_value: str,
    ) -> dict[str, Any]:
        """POST /oauth2/token to exchange a refresh_token for a new access_token."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_value,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        # Token endpoint does not require Bearer header — use a plain session
        async with aiohttp.ClientSession(
            base_url=DIALPAD_BASE_URL,
            timeout=self._timeout,
        ) as session:
            async with session.post("/oauth2/token", data=data) as response:
                return await self._raise_for_status(response, "/oauth2/token")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> DialpadHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
