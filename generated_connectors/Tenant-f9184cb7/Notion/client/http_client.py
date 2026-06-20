"""Notion connector — Notion API v1 HTTP client."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp

from exceptions import (
    NotionAuthError,
    NotionError,
    NotionNetworkError,
    NotionNotFoundError,
    NotionRateLimitError,
)

_NOTION_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_PAGE_SIZE = 100


class NotionHTTPClient:
    """Thin async HTTP wrapper for Notion API v1 endpoints.

    All requests use Bearer token auth and the Notion-Version header.
    Pagination uses has_more + next_cursor in the response body.
    """

    def __init__(
        self,
        base_url: str = _NOTION_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _auth_headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _raise_for_status(self, status: int, data: Dict[str, Any], context: str) -> Dict[str, Any]:
        """Map HTTP status codes to typed exceptions."""
        if status == 200:
            return data
        if status == 401:
            message = data.get("message", "Unauthorized")
            raise NotionAuthError(f"[{context}] Auth error (401): {message}")
        if status == 403:
            message = data.get("message", "Forbidden")
            raise NotionAuthError(f"[{context}] Forbidden (403): {message}")
        if status == 404:
            message = data.get("message", "Not found")
            raise NotionNotFoundError(f"[{context}] Not found (404): {message}")
        if status == 429:
            raise NotionRateLimitError(f"[{context}] Rate limited (429)")
        if status >= 500:
            message = data.get("message", f"HTTP {status}")
            raise NotionNetworkError(f"[{context}] Server error ({status}): {message}")
        message = data.get("message", f"HTTP {status}")
        raise NotionError(f"[{context}] Notion API error ({status}): {message}")

    # Keep old name as an alias for backward compat with existing tests
    def _check_response(self, status: int, data: Dict[str, Any], context: str) -> Dict[str, Any]:
        return self._raise_for_status(status, data, context)

    async def get_bot_user(self, token: str) -> Dict[str, Any]:
        """GET /users/me — retrieve the bot user for this integration token."""
        url = f"{self._base_url}/users/me"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers(token)) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise NotionNetworkError(f"[get_bot_user] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_bot_user")

    # Backward-compat alias used by existing connector + tests
    async def get_user_me(self, token: str) -> Dict[str, Any]:
        """Alias for get_bot_user — kept for backward compatibility."""
        return await self.get_bot_user(token)

    async def list_users(
        self,
        token: str,
        page_size: int = _DEFAULT_PAGE_SIZE,
        start_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /users — list all users in the workspace."""
        url = f"{self._base_url}/users"
        params: Dict[str, Any] = {"page_size": page_size}
        if start_cursor:
            params["start_cursor"] = start_cursor
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url,
                    headers=self._auth_headers(token),
                    params=params,
                ) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise NotionNetworkError(f"[list_users] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "list_users")

    async def search(
        self,
        token: str,
        query: str = "",
        filter_type: Optional[str] = None,
        start_cursor: Optional[str] = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """POST /search — search pages and databases."""
        url = f"{self._base_url}/search"
        payload: Dict[str, Any] = {"page_size": page_size}
        if query:
            payload["query"] = query
        if filter_type:
            payload["filter"] = {"value": filter_type, "property": "object"}
        if start_cursor:
            payload["start_cursor"] = start_cursor
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(
                    url,
                    headers=self._auth_headers(token),
                    json=payload,
                ) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise NotionNetworkError(f"[search] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "search")

    async def get_page(self, token: str, page_id: str) -> Dict[str, Any]:
        """GET /pages/{page_id} — retrieve a single page."""
        url = f"{self._base_url}/pages/{page_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers(token)) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise NotionNetworkError(f"[get_page] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_page")

    async def get_page_content(
        self,
        token: str,
        block_id: str,
        start_cursor: Optional[str] = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """GET /blocks/{block_id}/children — retrieve child blocks of a page or block."""
        return await self.get_block_children(token, block_id, start_cursor=start_cursor, page_size=page_size)

    async def get_block_children(
        self,
        token: str,
        block_id: str,
        start_cursor: Optional[str] = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """GET /blocks/{block_id}/children — retrieve child blocks of a page or block."""
        url = f"{self._base_url}/blocks/{block_id}/children"
        params: Dict[str, Any] = {"page_size": page_size}
        if start_cursor:
            params["start_cursor"] = start_cursor
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url,
                    headers=self._auth_headers(token),
                    params=params,
                ) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise NotionNetworkError(f"[get_block_children] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_block_children")

    async def query_database(
        self,
        token: str,
        database_id: str,
        filter_obj: Optional[Dict[str, Any]] = None,
        sorts: Optional[List[Dict[str, Any]]] = None,
        start_cursor: Optional[str] = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """POST /databases/{database_id}/query — query a database for pages."""
        url = f"{self._base_url}/databases/{database_id}/query"
        payload: Dict[str, Any] = {"page_size": page_size}
        if filter_obj:
            payload["filter"] = filter_obj
        if sorts:
            payload["sorts"] = sorts
        if start_cursor:
            payload["start_cursor"] = start_cursor
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(
                    url,
                    headers=self._auth_headers(token),
                    json=payload,
                ) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise NotionNetworkError(f"[query_database] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "query_database")

    async def get_database(self, token: str, database_id: str) -> Dict[str, Any]:
        """GET /databases/{database_id} — retrieve a single database."""
        url = f"{self._base_url}/databases/{database_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers(token)) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise NotionNetworkError(f"[get_database] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_database")

    async def list_databases(
        self,
        token: str,
        page_size: int = _DEFAULT_PAGE_SIZE,
        start_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /search with database filter — list all accessible databases."""
        return await self.search(
            token,
            query="",
            filter_type="database",
            start_cursor=start_cursor,
            page_size=page_size,
        )
