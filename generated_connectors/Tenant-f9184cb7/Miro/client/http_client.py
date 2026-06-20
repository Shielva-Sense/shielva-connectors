"""Miro connector — Miro REST API v2 HTTP client.

All calls use Authorization: Bearer {access_token}.
Cursor-based pagination is handled by callers via the returned `cursor` field.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp

from exceptions import (
    MiroAuthError,
    MiroError,
    MiroNetworkError,
    MiroNotFoundError,
    MiroRateLimitError,
)

_MIRO_BASE = "https://api.miro.com/v2"
_MIRO_V1_BASE = "https://api.miro.com/v1"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_PAGE_SIZE = 50


class MiroHTTPClient:
    """Thin async HTTP wrapper for the Miro REST API v2.

    Authentication is via Bearer token in the Authorization header.
    All resource endpoints are under https://api.miro.com/v2/.
    Token introspection uses https://api.miro.com/v2/oauth-token.
    """

    def __init__(
        self,
        base_url: str = _MIRO_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._v1_base = _MIRO_V1_BASE
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._config: Dict[str, Any] = config or {}

    def _access_token(self) -> str:
        """Return the OAuth access token from config."""
        return self._config.get("access_token", "")

    def _auth_headers(self) -> Dict[str, str]:
        """Build Authorization: Bearer headers for Miro API calls."""
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _raise_for_status(
        self,
        status: int,
        body: Any,
        context: str,
    ) -> None:
        """Map HTTP status codes to typed Miro exceptions.

        200–299 are success; anything else raises the appropriate typed error.
        """
        if 200 <= status < 300:
            return

        # Normalise body — may be dict, list, or empty
        if isinstance(body, dict):
            message = (
                body.get("message")
                or body.get("error")
                or body.get("description")
                or f"HTTP {status}"
            )
        else:
            message = f"HTTP {status}"

        if status in (401, 403):
            raise MiroAuthError(f"[{context}] Auth error ({status}): {message}")
        if status == 404:
            raise MiroNotFoundError(f"[{context}] Not found (404): {message}")
        if status == 429:
            raise MiroRateLimitError(f"[{context}] Rate limited (429): {message}")
        if status >= 500:
            raise MiroNetworkError(f"[{context}] Server error ({status}): {message}")
        raise MiroError(f"[{context}] Miro API error ({status}): {message}")

    async def get_token_info(self) -> Dict[str, Any]:
        """GET /v2/oauth-token — introspect the current access token.

        Returns scopes, team_id, user_id, and expiry information.
        Used for health_check to verify token validity without requiring a
        specific resource.
        """
        url = f"{self._base_url}/oauth-token"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    body: Any = await resp.json()
        except aiohttp.ClientError as exc:
            raise MiroNetworkError(f"[get_token_info] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_token_info")
        return body  # type: ignore[return-value]

    async def get_boards(
        self,
        limit: int = _DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
        **params: Any,
    ) -> Dict[str, Any]:
        """GET /v2/boards — list boards accessible to the token.

        Supports cursor-based pagination: pass cursor= from a previous response
        to fetch the next page. The response includes a `cursor` field for
        subsequent pages, and `data` list of board objects.
        """
        url = f"{self._base_url}/boards"
        query: Dict[str, Any] = {"limit": limit, **params}
        if cursor:
            query["cursor"] = cursor
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url, headers=self._auth_headers(), params=query
                ) as resp:
                    status = resp.status
                    body: Any = await resp.json()
        except aiohttp.ClientError as exc:
            raise MiroNetworkError(f"[get_boards] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_boards")
        return body  # type: ignore[return-value]

    async def get_board(self, board_id: str) -> Dict[str, Any]:
        """GET /v2/boards/{board_id} — retrieve a single board by ID."""
        url = f"{self._base_url}/boards/{board_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    body: Any = await resp.json()
        except aiohttp.ClientError as exc:
            raise MiroNetworkError(f"[get_board] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_board")
        return body  # type: ignore[return-value]

    async def get_board_items(
        self,
        board_id: str,
        limit: int = _DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v2/boards/{board_id}/items — list all items on a board.

        Supports cursor pagination. Optionally filter by item type
        (sticky_note, card, shape, text, frame, image, etc.).
        Response contains `data` list and optional `cursor` for next page.
        """
        url = f"{self._base_url}/boards/{board_id}/items"
        query: Dict[str, Any] = {"limit": limit}
        if cursor:
            query["cursor"] = cursor
        if type:
            query["type"] = type
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url, headers=self._auth_headers(), params=query
                ) as resp:
                    status = resp.status
                    body: Any = await resp.json()
        except aiohttp.ClientError as exc:
            raise MiroNetworkError(
                f"[get_board_items] Network error: {exc}"
            ) from exc
        self._raise_for_status(status, body, "get_board_items")
        return body  # type: ignore[return-value]

    async def get_teams(self, org_id: str) -> Dict[str, Any]:
        """GET /v2/orgs/{org_id}/teams — list teams in an organisation.

        Requires the organizations:read scope.
        """
        url = f"{self._base_url}/orgs/{org_id}/teams"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    body: Any = await resp.json()
        except aiohttp.ClientError as exc:
            raise MiroNetworkError(f"[get_teams] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_teams")
        return body  # type: ignore[return-value]
