"""Loom connector — Loom API v1 HTTP client."""
from __future__ import annotations

from typing import Any, Dict, Optional

import aiohttp

from exceptions import (
    LoomAuthError,
    LoomError,
    LoomNetworkError,
    LoomNotFoundError,
    LoomRateLimitError,
)

_LOOM_BASE = "https://www.loom.com/v1"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_PAGE_SIZE = 50


class LoomHTTPClient:
    """Thin async HTTP wrapper for the Loom API v1 endpoints.

    All requests use Bearer token auth via the Authorization header.
    Pagination uses a `next_page` cursor returned in the response body.

    Args:
        config: connector config dict containing `api_key`.
        base_url: Loom API base URL (overridable for tests).
        timeout: total request timeout in seconds.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        base_url: str = _LOOM_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._config = config or {}
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _get_api_key(self) -> str:
        return self._config.get("api_key", "")

    def _auth_headers(self) -> Dict[str, str]:
        """Return Bearer auth headers for Loom API requests."""
        return {
            "Authorization": f"Bearer {self._get_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _raise_for_status(self, status: int, body: Dict[str, Any], context: str) -> None:
        """Map HTTP status codes to typed Loom exceptions.

        Args:
            status: HTTP status code from the response.
            body: parsed JSON response body.
            context: calling method name for error messages.

        Raises:
            LoomAuthError: on 401 or 403.
            LoomNotFoundError: on 404.
            LoomRateLimitError: on 429.
            LoomNetworkError: on 5xx.
            LoomError: on any other non-2xx status.
        """
        if 200 <= status < 300:
            return
        message = body.get("message", body.get("error", f"HTTP {status}"))
        if status == 401:
            raise LoomAuthError(f"[{context}] Unauthorized (401): {message}")
        if status == 403:
            raise LoomAuthError(f"[{context}] Forbidden (403): {message}")
        if status == 404:
            raise LoomNotFoundError(f"[{context}] Not found (404): {message}")
        if status == 429:
            raise LoomRateLimitError(f"[{context}] Rate limited (429): {message}")
        if status >= 500:
            raise LoomNetworkError(f"[{context}] Server error ({status}): {message}")
        raise LoomError(f"[{context}] Loom API error ({status}): {message}")

    async def get_me(self) -> Dict[str, Any]:
        """GET /me — retrieve the authenticated user / workspace info.

        Used by health_check to validate the API key.
        """
        url = f"{self._base_url}/me"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise LoomNetworkError(f"[get_me] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_me")
        return body

    async def get_videos(
        self,
        page_size: int = _DEFAULT_PAGE_SIZE,
        next_page: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /videos — list videos with optional cursor pagination.

        Args:
            page_size: number of results per page (max 50).
            next_page: cursor token from a previous response for pagination.

        Returns:
            dict with `videos` list and optional `next_page` cursor.
        """
        url = f"{self._base_url}/videos"
        params: Dict[str, Any] = {"limit": page_size}
        if next_page:
            params["next_page"] = next_page
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url, headers=self._auth_headers(), params=params
                ) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise LoomNetworkError(f"[get_videos] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_videos")
        return body

    async def get_video(self, video_id: str) -> Dict[str, Any]:
        """GET /videos/{video_id} — retrieve a single video by ID.

        Args:
            video_id: the Loom video identifier.
        """
        url = f"{self._base_url}/videos/{video_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise LoomNetworkError(f"[get_video] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_video")
        return body

    async def get_video_transcript(self, video_id: str) -> Dict[str, Any]:
        """GET /videos/{video_id}/transcript — retrieve the transcript for a video.

        Returns a dict with `transcript` content (may be empty if not available).

        Args:
            video_id: the Loom video identifier.
        """
        url = f"{self._base_url}/videos/{video_id}/transcript"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise LoomNetworkError(f"[get_video_transcript] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_video_transcript")
        return body

    async def get_folders(
        self,
        folder_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /folders or GET /folders/{folder_id} — list or retrieve folders.

        If folder_id is provided, fetches that specific folder's details.
        Otherwise fetches the root folder listing.

        Args:
            folder_id: optional folder ID to fetch a specific folder.
        """
        if folder_id:
            url = f"{self._base_url}/folders/{folder_id}"
        else:
            url = f"{self._base_url}/folders"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise LoomNetworkError(f"[get_folders] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_folders")
        return body

    async def get_workspaces(self) -> Dict[str, Any]:
        """GET /workspaces — list all workspaces accessible to the API key."""
        url = f"{self._base_url}/workspaces"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise LoomNetworkError(f"[get_workspaces] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_workspaces")
        return body
