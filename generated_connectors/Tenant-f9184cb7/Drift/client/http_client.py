from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    DriftAuthError,
    DriftError,
    DriftNetworkError,
    DriftNotFoundError,
    DriftRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
DRIFT_API_BASE: str = "https://driftapi.com"
DRIFT_AUTH_URL: str = "https://dev.drift.com/authorize"
DRIFT_TOKEN_URL: str = "https://driftapi.com/auth/token"


class DriftHTTPClient:
    """Low-level async HTTP client for the Drift REST API v1."""

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
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{DRIFT_API_BASE}{path}"
        headers = self._make_headers()
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise DriftNetworkError(f"Network error: {exc}") from exc
        except (
            DriftError,
            DriftAuthError,
            DriftRateLimitError,
            DriftNotFoundError,
            DriftNetworkError,
        ):
            raise
        except Exception as exc:
            raise DriftNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        status = response.status

        if status == 200:
            return await response.json()

        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        err_msg: str = (
            body.get("message", "")
            or body.get("error", "")
            or body.get("description", "")
            or f"HTTP {status}"
        )

        self._raise_for_status(status, body)
        raise DriftError(f"Drift error {status}: {err_msg}", status_code=status)

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Raise the appropriate typed exception for HTTP error status codes."""
        err_msg: str = (
            body.get("message", "")
            or body.get("error", "")
            or body.get("description", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise DriftAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise DriftNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(
                body.get("retryAfter", 0)
                or body.get("retry_after", 0)
                or 0
            )
            raise DriftRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise DriftNetworkError(
                f"Drift server error {status}: {err_msg}",
                status_code=status,
            )

    # ── Users (auth health check) ─────────────────────────────────────────────

    async def get_users(self) -> dict[str, Any]:
        """GET /users/list — verify credentials and return user list."""
        return await self._request("GET", "/users/list")

    # ── Conversations ──────────────────────────────────────────────────────────

    async def get_conversations(
        self,
        limit: int = 50,
        next_page_token: str | None = None,
    ) -> dict[str, Any]:
        """GET /conversations — paginated conversation listing with cursor pagination."""
        params: dict[str, Any] = {"limit": limit}
        if next_page_token:
            params["next_page_token"] = next_page_token
        return await self._request("GET", "/conversations", params=params)

    async def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        """GET /conversations/{id} — fetch a single conversation by ID."""
        return await self._request("GET", f"/conversations/{conversation_id}")

    async def get_conversation_messages(self, conversation_id: int) -> dict[str, Any]:
        """GET /conversations/{id}/messages — fetch all messages for a conversation."""
        return await self._request(
            "GET", f"/conversations/{conversation_id}/messages"
        )

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def get_contacts(
        self,
        limit: int = 100,
        next_page_token: str | None = None,
    ) -> dict[str, Any]:
        """GET /contacts — paginated contact listing with cursor pagination."""
        params: dict[str, Any] = {"limit": limit}
        if next_page_token:
            params["next_page_token"] = next_page_token
        return await self._request("GET", "/contacts", params=params)

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def get_accounts(self) -> dict[str, Any]:
        """GET /accounts — list all accounts."""
        return await self._request("GET", "/accounts")
