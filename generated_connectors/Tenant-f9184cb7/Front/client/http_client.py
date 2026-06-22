"""Low-level async HTTP client for the Front Core API v1."""
from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    FrontAuthError,
    FrontError,
    FrontNetworkError,
    FrontNotFoundError,
    FrontRateLimitError,
)

BASE_URL: str = "https://api2.frontapp.com"
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_PAGE_LIMIT: int = 100


class FrontHTTPClient:
    """Async HTTP client for the Front Core API v1.

    Authentication uses a Bearer token sent in the Authorization header.
    Front uses HAL-style ``_links`` and ``_pagination.next`` for cursor
    pagination — the ``next`` field is a full URL containing the page token.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._api_token: str = cfg.get("api_token", "").strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    # ── Session management ───────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ── Error handling ───────────────────────────────────────────────────────

    def _raise_for_status(self, status: int, body: dict[str, Any], path: str) -> None:
        """Map Front API error status codes to connector exceptions."""
        err_msg: str = body.get("message", "") or body.get("error", "") or f"HTTP {status}"

        if status in (401, 403):
            raise FrontAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise FrontNotFoundError("resource", path)
        if status == 429:
            raise FrontRateLimitError(
                f"Rate limited: {err_msg}",
                retry_after=0.0,
            )
        if status >= 500:
            raise FrontNetworkError(
                f"Front server error {status}: {err_msg}",
                status_code=status,
            )
        raise FrontError(
            f"Front error {status}: {err_msg}",
            status_code=status,
        )

    # ── Core request ─────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        full_url: str | None = None,
    ) -> Any:
        """Execute an HTTP request against the Front API.

        When ``full_url`` is provided (e.g. for pagination ``next`` links), it
        is used directly instead of constructing the URL from ``path``.
        """
        url = full_url if full_url else f"{BASE_URL}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(),
                params=params if not full_url else None,
            ) as response:
                if response.status in (200, 201):
                    return await response.json(content_type=None)
                if response.status == 204:
                    return {}

                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                self._raise_for_status(response.status, body, path)
                return {}  # unreachable — _raise_for_status always raises
        except (FrontError,):
            raise
        except aiohttp.ClientConnectorError as exc:
            raise FrontNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise FrontNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise FrontNetworkError(f"Network error: {exc}") from exc

    # ── Me ───────────────────────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """GET /me — verify credentials and return current teammate info."""
        return await self._request("GET", "/me")

    # ── Conversations ────────────────────────────────────────────────────────

    async def get_conversations(
        self,
        page_token: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        **params: Any,
    ) -> dict[str, Any]:
        """GET /conversations — list conversations with optional cursor pagination.

        Returns the raw Front response containing ``_results`` and
        ``_pagination`` fields.
        """
        query: dict[str, Any] = {"limit": limit, **params}
        full_url: str | None = None
        if page_token:
            # Front's next URL already contains all params; use it directly.
            full_url = page_token
            query = {}
        return await self._request("GET", "/conversations", params=query, full_url=full_url)

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """GET /conversations/{id} — fetch a single conversation."""
        return await self._request("GET", f"/conversations/{conversation_id}")

    async def get_conversation_messages(self, conversation_id: str) -> dict[str, Any]:
        """GET /conversations/{id}/messages — list messages in a conversation."""
        return await self._request("GET", f"/conversations/{conversation_id}/messages")

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def get_contacts(
        self,
        page_token: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> dict[str, Any]:
        """GET /contacts — list contacts with cursor pagination."""
        full_url: str | None = None
        query: dict[str, Any] = {"limit": limit}
        if page_token:
            full_url = page_token
            query = {}
        return await self._request("GET", "/contacts", params=query, full_url=full_url)

    # ── Teammates ────────────────────────────────────────────────────────────

    async def get_teammates(self) -> dict[str, Any]:
        """GET /teammates — list all teammates in the company."""
        return await self._request("GET", "/teammates")

    # ── Inboxes ──────────────────────────────────────────────────────────────

    async def get_inboxes(self) -> dict[str, Any]:
        """GET /inboxes — list all inboxes."""
        return await self._request("GET", "/inboxes")

    # ── Tags ─────────────────────────────────────────────────────────────────

    async def get_tags(self) -> dict[str, Any]:
        """GET /tags — list all tags."""
        return await self._request("GET", "/tags")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> FrontHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
