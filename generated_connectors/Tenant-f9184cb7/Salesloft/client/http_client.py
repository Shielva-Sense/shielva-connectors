from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    SalesloftAuthError,
    SalesloftError,
    SalesloftNetworkError,
    SalesloftNotFoundError,
    SalesloftRateLimitError,
)

SALESLOFT_BASE_URL = "https://api.salesloft.com"
SALESLOFT_TOKEN_URL = "https://accounts.salesloft.com/oauth/token"
DEFAULT_TIMEOUT_S = 30.0


class SalesloftHTTPClient:
    """Low-level async HTTP client for the Salesloft REST API v2.

    All requests inject ``Authorization: Bearer {access_token}`` headers.
    Salesloft wraps paginated responses in::

        {"data": [...], "metadata": {"paging": {"next_page": N, "total_pages": N}}}

    Single-resource responses return ``{"data": {...}}``.
    """

    def __init__(
        self,
        access_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=SALESLOFT_BASE_URL,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._session

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        """Parse response and raise typed exceptions for error status codes."""
        if response.status in (200, 201, 204):
            if response.status == 204:
                return {}
            try:
                return await response.json(content_type=None)  # type: ignore[return-value]
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)  # type: ignore[assignment]
        except Exception:
            pass

        err_msg = (
            body.get("error_description")
            or body.get("error")
            or body.get("message")
            or f"HTTP {response.status}"
        )

        if response.status == 401:
            raise SalesloftAuthError(
                f"Authentication failed: {err_msg}", 401, "unauthorized"
            )
        if response.status == 403:
            raise SalesloftAuthError(
                f"Forbidden: {err_msg}", 403, "forbidden"
            )
        if response.status == 404:
            raise SalesloftNotFoundError("resource", str(response.url))
        if response.status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise SalesloftRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status >= 500:
            raise SalesloftError(
                f"Salesloft server error {response.status}: {err_msg}",
                response.status,
                "server_error",
            )

        raise SalesloftError(
            f"Salesloft error {response.status}: {err_msg}",
            response.status,
        )

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        session = self._get_session()
        try:
            async with session.request(method, path, **kwargs) as response:
                return await self._raise_for_status(response)
        except (SalesloftError,):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise SalesloftNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise SalesloftNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise SalesloftNetworkError(f"Network error: {exc}") from exc

    # ── Auth probe ──────────────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """GET /v2/me.json — returns authenticated user info."""
        return await self._request("GET", "/v2/me.json")

    # ── People ──────────────────────────────────────────────────────────────

    async def get_people(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v2/people.json — paginated list of people (contacts/leads)."""
        return await self._request(
            "GET",
            "/v2/people.json",
            params={"page": page, "per_page": per_page},
        )

    # ── Cadences ────────────────────────────────────────────────────────────

    async def get_cadences(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v2/cadences.json — paginated list of cadences (sequences/workflows)."""
        return await self._request(
            "GET",
            "/v2/cadences.json",
            params={"page": page, "per_page": per_page},
        )

    # ── Calls ────────────────────────────────────────────────────────────────

    async def get_activities_calls(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v2/activities/calls.json — paginated list of call activities."""
        return await self._request(
            "GET",
            "/v2/activities/calls.json",
            params={"page": page, "per_page": per_page},
        )

    # ── Emails ───────────────────────────────────────────────────────────────

    async def get_emails(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v2/activities/emails.json — paginated list of email activities."""
        return await self._request(
            "GET",
            "/v2/activities/emails.json",
            params={"page": page, "per_page": per_page},
        )

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def get_accounts(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v2/accounts.json — paginated list of accounts (companies)."""
        return await self._request(
            "GET",
            "/v2/accounts.json",
            params={"page": page, "per_page": per_page},
        )

    # ── Token refresh ────────────────────────────────────────────────────────

    async def refresh_token(self, refresh_token_value: str) -> dict[str, Any]:
        """POST to OAuth token URL to refresh the access token."""
        # Use a separate session without Bearer auth for token exchange
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(
                SALESLOFT_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token_value,
                },
            ) as response:
                return await self._raise_for_status(response)

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> SalesloftHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
