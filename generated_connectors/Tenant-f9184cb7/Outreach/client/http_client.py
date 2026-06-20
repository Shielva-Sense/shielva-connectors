from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    OutreachAuthError,
    OutreachError,
    OutreachNetworkError,
    OutreachNotFoundError,
    OutreachRateLimitError,
)

OUTREACH_BASE_URL: str = "https://api.outreach.io"
OUTREACH_AUTH_URL: str = "https://api.outreach.io/oauth/authorize"
OUTREACH_TOKEN_URL: str = "https://api.outreach.io/oauth/token"
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_PAGE_SIZE: int = 100

OUTREACH_SCOPES: str = (
    "prospects.all sequences.read accounts.read mailings.read calls.read"
)


class OutreachHTTPClient:
    """Low-level async HTTP client for the Outreach REST API (JSON:API format).

    All requests use Bearer token authentication.
    Outreach uses JSON:API — responses have ``data``, optional ``relationships``,
    and ``links.next`` for cursor-based pagination.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._config: dict[str, Any] = config or {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def _access_token(self) -> str:
        return self._config.get("access_token", "")

    def _make_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{OUTREACH_BASE_URL}{path}"
        headers = self._make_headers()
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise OutreachNetworkError(f"Network error: {exc}") from exc
        except (
            OutreachError,
            OutreachAuthError,
            OutreachRateLimitError,
            OutreachNotFoundError,
            OutreachNetworkError,
        ):
            raise
        except Exception as exc:
            raise OutreachNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        if status == 204:
            return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        self._raise_for_status(status, body)
        err_msg = self._extract_error_message(body, status)
        raise OutreachError(f"Outreach error {status}: {err_msg}", status_code=status)

    def _extract_error_message(
        self, body: dict[str, Any], status: int
    ) -> str:
        """Extract a human-readable error from a JSON:API error body."""
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            return (
                first.get("detail")
                or first.get("title")
                or str(first)
            )
        return (
            body.get("message")
            or body.get("error")
            or f"HTTP {status}"
        )

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Raise the appropriate typed exception for HTTP error status codes."""
        err_msg = self._extract_error_message(body, status)

        if status == 401:
            raise OutreachAuthError(
                f"Authentication failed (401): {err_msg}",
                status_code=401,
                code="unauthorized",
            )
        if status == 403:
            raise OutreachAuthError(
                f"Forbidden (403): {err_msg}",
                status_code=403,
                code="forbidden",
            )
        if status == 404:
            raise OutreachNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(
                body.get("retryAfter", 0)
                or body.get("retry_after", 0)
                or 0
            )
            raise OutreachRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise OutreachNetworkError(
                f"Outreach server error {status}: {err_msg}",
                status_code=status,
            )

    # ── Auth probe ────────────────────────────────────────────────────────────

    async def get_current_user(self) -> dict[str, Any]:
        """GET /api/v2/users/current — verify credentials and return the authed user."""
        return await self._request("GET", "/api/v2/users/current")

    # ── Prospects ─────────────────────────────────────────────────────────────

    async def get_prospects(
        self,
        cursor: str | None = None,
        count: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """GET /api/v2/prospects?page[size]=100 — paginated prospect listing.

        Outreach uses JSON:API cursor pagination — the next page URL is in
        ``links.next``.  Pass the full ``links.next`` URL as ``cursor`` to
        continue from where you left off.
        """
        if cursor:
            # cursor is a full URL — strip the base and use the path+query
            path_and_query = cursor.replace(OUTREACH_BASE_URL, "")
            return await self._request("GET", path_and_query)

        params: dict[str, Any] = {"page[size]": count}
        return await self._request("GET", "/api/v2/prospects", params=params)

    async def get_prospect(self, prospect_id: int | str) -> dict[str, Any]:
        """GET /api/v2/prospects/{prospect_id} — fetch a single prospect."""
        return await self._request("GET", f"/api/v2/prospects/{prospect_id}")

    # ── Sequences ─────────────────────────────────────────────────────────────

    async def get_sequences(
        self,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/sequences — paginated sequence listing."""
        if cursor:
            path_and_query = cursor.replace(OUTREACH_BASE_URL, "")
            return await self._request("GET", path_and_query)
        return await self._request("GET", "/api/v2/sequences")

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def get_accounts(
        self,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/accounts — paginated account listing."""
        if cursor:
            path_and_query = cursor.replace(OUTREACH_BASE_URL, "")
            return await self._request("GET", path_and_query)
        return await self._request("GET", "/api/v2/accounts")

    # ── Calls ─────────────────────────────────────────────────────────────────

    async def get_calls(
        self,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/calls — paginated call listing."""
        if cursor:
            path_and_query = cursor.replace(OUTREACH_BASE_URL, "")
            return await self._request("GET", path_and_query)
        return await self._request("GET", "/api/v2/calls")

    # ── Mailings ──────────────────────────────────────────────────────────────

    async def get_mailings(
        self,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/mailings — paginated mailing listing."""
        if cursor:
            path_and_query = cursor.replace(OUTREACH_BASE_URL, "")
            return await self._request("GET", path_and_query)
        return await self._request("GET", "/api/v2/mailings")

    # ── Token refresh ─────────────────────────────────────────────────────────

    async def refresh_token(self) -> dict[str, Any]:
        """POST /oauth/token — exchange refresh_token for a new access_token."""
        payload: dict[str, Any] = {
            "client_id": self._config.get("client_id", ""),
            "client_secret": self._config.get("client_secret", ""),
            "redirect_uri": self._config.get("redirect_uri", ""),
            "grant_type": "refresh_token",
            "refresh_token": self._config.get("refresh_token", ""),
        }
        return await self._request("POST", "/oauth/token", json=payload)

    async def refresh_access_token(self) -> dict[str, Any]:
        """Alias for refresh_token — required by Shielva connector interface."""
        return await self.refresh_token()

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        """POST /oauth/token — exchange authorization code for access/refresh tokens."""
        payload: dict[str, Any] = {
            "client_id": self._config.get("client_id", ""),
            "client_secret": self._config.get("client_secret", ""),
            "redirect_uri": self._config.get("redirect_uri", ""),
            "grant_type": "authorization_code",
            "code": code,
        }
        return await self._request("POST", "/oauth/token", json=payload)

    async def get_users(
        self,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/users — paginated user listing (used for health check)."""
        if cursor:
            path_and_query = cursor.replace(OUTREACH_BASE_URL, "")
            return await self._request("GET", path_and_query)
        return await self._request("GET", "/api/v2/users")

    async def aclose(self) -> None:
        """No persistent session to close — sessions are per-request."""

    async def __aenter__(self) -> OutreachHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
