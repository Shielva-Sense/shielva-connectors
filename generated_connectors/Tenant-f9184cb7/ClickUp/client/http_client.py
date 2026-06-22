"""ClickUp connector — ClickUp API v2 HTTP client."""
from __future__ import annotations

from typing import Any, Dict

import aiohttp

from exceptions import (
    ClickUpAuthError,
    ClickUpError,
    ClickUpNetworkError,
    ClickUpNotFoundError,
    ClickUpRateLimitError,
)

BASE_URL: str = "https://api.clickup.com/api/v2"
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_PAGE_SIZE: int = 100


class ClickUpHTTPClient:
    """Async HTTP client for the ClickUp API v2.

    Authentication uses a Personal API Token sent as the raw ``Authorization``
    header value (no "Bearer" prefix) — per ClickUp's documentation.

    Task pagination uses page-based integers (``?page=0``, ``?page=1``, …).
    Continues until the response contains fewer items than the page size, or
    ``last_page: true`` is present in the response body.
    """

    def __init__(
        self,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _headers(self) -> Dict[str, str]:
        """ClickUp uses the raw token value — no 'Bearer' prefix."""
        return {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }

    def _raise_for_status(
        self,
        status: int,
        body: Dict[str, Any],
        context: str = "",
    ) -> Dict[str, Any]:
        """Map HTTP status codes to typed ClickUp exceptions.

        Returns the body unchanged for 2xx responses.
        """
        if 200 <= status < 300:
            return body
        err_msg: str = (
            body.get("err")
            or body.get("error")
            or body.get("message")
            or f"HTTP {status}"
        )
        ctx = f"[{context}] " if context else ""
        if status in (401, 403):
            raise ClickUpAuthError(
                f"{ctx}Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise ClickUpNotFoundError(f"{ctx}Not found", "")
        if status == 429:
            raise ClickUpRateLimitError(
                f"{ctx}Rate limited (429): {err_msg}",
                retry_after=0.0,
            )
        if status >= 500:
            raise ClickUpNetworkError(
                f"{ctx}Server error ({status}): {err_msg}",
                status_code=status,
            )
        raise ClickUpError(
            f"{ctx}ClickUp API error ({status}): {err_msg}",
            status_code=status,
        )

    async def _get(self, path: str, **params: Any) -> Dict[str, Any]:
        """Perform an authenticated GET request and return the parsed JSON body."""
        url = f"{BASE_URL}{path}"
        session = self._get_session()
        try:
            async with session.get(
                url, headers=self._headers(), params=params or None
            ) as resp:
                status = resp.status
                try:
                    body: Dict[str, Any] = await resp.json(content_type=None)
                except Exception:
                    body = {}
        except ClickUpError:
            raise
        except aiohttp.ClientConnectorError as exc:
            raise ClickUpNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise ClickUpNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise ClickUpNetworkError(f"Network error: {exc}") from exc
        return self._raise_for_status(status, body, path)

    # ── User ──────────────────────────────────────────────────────────────────

    async def get_authorized_user(self) -> Dict[str, Any]:
        """GET /user — return the authenticated user (health check / install)."""
        return await self._get("/user")

    # ── Teams / Workspaces ────────────────────────────────────────────────────

    async def get_teams(self) -> Dict[str, Any]:
        """GET /team — list all authorized workspaces (teams)."""
        return await self._get("/team")

    # ── Spaces ────────────────────────────────────────────────────────────────

    async def get_spaces(self, team_id: str, archived: bool = False) -> Dict[str, Any]:
        """GET /team/{team_id}/space?archived=false — list spaces in a workspace."""
        return await self._get(
            f"/team/{team_id}/space",
            archived="true" if archived else "false",
        )

    async def get_space(self, space_id: str) -> Dict[str, Any]:
        """GET /space/{space_id} — retrieve a single space."""
        return await self._get(f"/space/{space_id}")

    # ── Folders ───────────────────────────────────────────────────────────────

    async def get_folders(self, space_id: str, archived: bool = False) -> Dict[str, Any]:
        """GET /space/{space_id}/folder?archived=false — list folders in a space."""
        return await self._get(
            f"/space/{space_id}/folder",
            archived="true" if archived else "false",
        )

    # ── Lists ─────────────────────────────────────────────────────────────────

    async def get_lists(
        self,
        space_id: str | None = None,
        folder_id: str | None = None,
        archived: bool = False,
    ) -> Dict[str, Any]:
        """GET /space/{space_id}/list  or  /folder/{folder_id}/list.

        Exactly one of *space_id* or *folder_id* must be provided.
        """
        if folder_id:
            path = f"/folder/{folder_id}/list"
        elif space_id:
            path = f"/space/{space_id}/list"
        else:
            raise ValueError("Either space_id or folder_id must be provided")
        return await self._get(path, archived="true" if archived else "false")

    # ── Tasks ─────────────────────────────────────────────────────────────────

    async def get_tasks(
        self,
        list_id: str,
        page: int = 0,
        archived: bool = False,
        include_closed: bool = False,
    ) -> Dict[str, Any]:
        """GET /list/{list_id}/task — list tasks in a list (page-based pagination).

        Args:
            list_id: ClickUp list identifier.
            page: 0-indexed page number.
            archived: include archived tasks.
            include_closed: include closed tasks.
        """
        return await self._get(
            f"/list/{list_id}/task",
            page=page,
            archived="true" if archived else "false",
            include_closed="true" if include_closed else "false",
        )

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        """GET /task/{task_id} — retrieve a single task by ID."""
        return await self._get(f"/task/{task_id}")

    # ── Members ───────────────────────────────────────────────────────────────

    async def get_members(self, list_id: str) -> Dict[str, Any]:
        """GET /list/{list_id}/member — list members of a list."""
        return await self._get(f"/list/{list_id}/member")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> ClickUpHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
