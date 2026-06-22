from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    TidioAuthError,
    TidioError,
    TidioNetworkError,
    TidioNotFoundError,
    TidioRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
TIDIO_API_BASE: str = "https://api.tidio.co"


class TidioHTTPClient:
    """Low-level async HTTP client for the Tidio REST API v1."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._config: dict[str, Any] = config or {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def _api_key(self) -> str:
        return self._config.get("api_key", "")

    def _make_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{TIDIO_API_BASE}{path}"
        headers = self._make_headers()
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise TidioNetworkError(f"Network error: {exc}") from exc
        except (
            TidioError,
            TidioAuthError,
            TidioRateLimitError,
            TidioNotFoundError,
            TidioNetworkError,
        ):
            raise
        except Exception as exc:
            raise TidioNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        status = response.status

        if status == 200:
            return await response.json()

        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        self._raise_for_status(status, body)

        err_msg: str = (
            body.get("message", "")
            or body.get("error", "")
            or body.get("detail", "")
            or f"HTTP {status}"
        )
        raise TidioError(f"Tidio error {status}: {err_msg}", status_code=status)

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Raise the appropriate typed exception for HTTP error status codes."""
        err_msg: str = (
            body.get("message", "")
            or body.get("error", "")
            or body.get("detail", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise TidioAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise TidioNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(
                body.get("retry_after", 0)
                or body.get("retryAfter", 0)
                or 0
            )
            raise TidioRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise TidioNetworkError(
                f"Tidio server error {status}: {err_msg}",
                status_code=status,
            )

    # ── Project (auth health check) ───────────────────────────────────────────

    async def get_project(self) -> dict[str, Any]:
        """GET /api/v1/project — verify credentials and return project/account info."""
        return await self._request("GET", "/api/v1/project")

    # ── Conversations ─────────────────────────────────────────────────────────

    async def get_conversations(
        self,
        page: int = 1,
        page_size: int = 50,
        status: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/conversations — paginated conversation listing."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        return await self._request("GET", "/api/v1/conversations", params=params)

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """GET /api/v1/conversations/{id} — fetch a single conversation by ID."""
        return await self._request("GET", f"/api/v1/conversations/{conversation_id}")

    async def get_conversation_messages(self, conversation_id: str) -> dict[str, Any]:
        """GET /api/v1/conversations/{id}/messages — fetch all messages for a conversation."""
        return await self._request(
            "GET", f"/api/v1/conversations/{conversation_id}/messages"
        )

    # ── Visitors ──────────────────────────────────────────────────────────────

    async def get_visitors(
        self,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """GET /api/v1/visitors — paginated visitor listing."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        return await self._request("GET", "/api/v1/visitors", params=params)

    # ── Operators ─────────────────────────────────────────────────────────────

    async def get_operators(self) -> dict[str, Any]:
        """GET /api/v1/operators — list all operators."""
        return await self._request("GET", "/api/v1/operators")

    # ── Chatbots ──────────────────────────────────────────────────────────────

    async def get_chatbots(self) -> dict[str, Any]:
        """GET /api/v1/chatbots — list all chatbots."""
        return await self._request("GET", "/api/v1/chatbots")
