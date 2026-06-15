"""GmailHTTPClient — all Gmail REST API calls live here; zero business logic."""
from typing import Any, Dict, List, Optional, Tuple, Type

import aiohttp
import structlog

from exceptions import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorRateLimitError,
)
from helpers.utils import retry

logger = structlog.get_logger(__name__)

_PAGE_SIZE = 100


class GmailHTTPClient:
    """Thin async HTTP wrapper over the Gmail REST API v1."""

    # OCP-4: status-code → (exception_class, message_prefix) lookup
    _ERROR_MAP: Dict[int, Tuple[Type[ConnectorError], str]] = {
        401: (ConnectorAuthError, "Authentication failed"),
        403: (ConnectorPermissionError, "Permission denied"),
        404: (ConnectorNotFoundError, "Not found"),
        429: (ConnectorRateLimitError, "Rate limited"),
    }

    def __init__(self, access_token: str, base_url: str) -> None:
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _raise_for_status(self, status: int, response_text: str, context: str = "") -> None:
        """Translate HTTP status codes into typed connector exceptions via lookup dict."""
        entry = self._ERROR_MAP.get(status)
        if entry:
            exc_class, prefix = entry
            raise exc_class(f"{prefix} [{context}]: {response_text}")
        raise ConnectorError(f"HTTP {status} [{context}]: {response_text}")

    # ── Profile ──────────────────────────────────────────────────────────────

    @retry()
    async def execute_get_profile(self) -> Dict[str, Any]:
        """GET users/me/profile — used by health_check()."""
        url = f"{self._base_url}/users/me/profile"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._headers()) as resp:
                text = await resp.text()
                if resp.status != 200:
                    self._raise_for_status(resp.status, text, "get_profile")
                return await resp.json(content_type=None)

    # ── Messages ─────────────────────────────────────────────────────────────

    @retry()
    async def execute_list_messages(
        self,
        query: str = "",
        max_results: int = _PAGE_SIZE,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET users/me/messages — returns {messages, nextPageToken, resultSizeEstimate}."""
        url = f"{self._base_url}/users/me/messages"
        params: Dict[str, Any] = {"maxResults": max_results}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._headers(), params=params) as resp:
                text = await resp.text()
                if resp.status != 200:
                    self._raise_for_status(resp.status, text, "list_messages")
                return await resp.json(content_type=None)

    @retry()
    async def execute_get_message(self, msg_id: str) -> Dict[str, Any]:
        """GET users/me/messages/{id}?format=full — returns full message resource."""
        url = f"{self._base_url}/users/me/messages/{msg_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=self._headers(), params={"format": "full"}
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    self._raise_for_status(resp.status, text, f"get_message:{msg_id}")
                return await resp.json(content_type=None)

    @retry()
    async def execute_modify_message(
        self,
        msg_id: str,
        add_label_ids: Optional[List[str]] = None,
        remove_label_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST users/me/messages/{id}/modify — adds/removes label IDs."""
        url = f"{self._base_url}/users/me/messages/{msg_id}/modify"
        payload: Dict[str, Any] = {}
        if add_label_ids:
            payload["addLabelIds"] = add_label_ids
        if remove_label_ids:
            payload["removeLabelIds"] = remove_label_ids
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                text = await resp.text()
                if resp.status != 200:
                    self._raise_for_status(resp.status, text, f"modify_message:{msg_id}")
                return await resp.json(content_type=None)

    @retry()
    async def execute_trash_message(self, msg_id: str) -> Dict[str, Any]:
        """POST users/me/messages/{id}/trash — moves to Trash; returns trashed resource."""
        url = f"{self._base_url}/users/me/messages/{msg_id}/trash"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._headers()) as resp:
                text = await resp.text()
                if resp.status != 200:
                    self._raise_for_status(resp.status, text, f"trash_message:{msg_id}")
                return await resp.json(content_type=None)

    @retry()
    async def execute_delete_message(self, msg_id: str) -> None:
        """DELETE users/me/messages/{id} — permanent delete; returns None (204)."""
        url = f"{self._base_url}/users/me/messages/{msg_id}"
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=self._headers()) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    self._raise_for_status(resp.status, text, f"delete_message:{msg_id}")

    # ── Threads ──────────────────────────────────────────────────────────────

    @retry()
    async def execute_trash_thread(self, thread_id: str) -> Dict[str, Any]:
        """POST users/me/threads/{id}/trash — moves thread to Trash; returns trashed thread."""
        url = f"{self._base_url}/users/me/threads/{thread_id}/trash"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._headers()) as resp:
                text = await resp.text()
                if resp.status != 200:
                    self._raise_for_status(resp.status, text, f"trash_thread:{thread_id}")
                return await resp.json(content_type=None)

    @retry()
    async def execute_delete_thread(self, thread_id: str) -> None:
        """DELETE users/me/threads/{id} — permanent delete; returns None (204)."""
        url = f"{self._base_url}/users/me/threads/{thread_id}"
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=self._headers()) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    self._raise_for_status(resp.status, text, f"delete_thread:{thread_id}")
