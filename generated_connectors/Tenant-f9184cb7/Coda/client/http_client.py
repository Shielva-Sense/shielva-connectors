"""Coda connector — Coda API v1 HTTP client."""
from __future__ import annotations

from typing import Any, Dict, Optional

import aiohttp

from exceptions import (
    CodaAuthError,
    CodaError,
    CodaNetworkError,
    CodaNotFoundError,
    CodaRateLimitError,
)

_CODA_BASE = "https://coda.io/apis/v1"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_PAGE_SIZE = 25
_DEFAULT_PAGE_SIZE_LARGE = 50
_DEFAULT_ROW_PAGE_SIZE = 500


class CodaHTTPClient:
    """Thin async HTTP wrapper for Coda API v1 endpoints.

    All requests authenticate via Bearer token in the Authorization header.
    Coda uses ``nextPageToken`` and ``nextPageLink`` for cursor-based pagination.
    """

    def __init__(
        self,
        base_url: str = _CODA_BASE,
        config: Optional[Dict[str, Any]] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._config = config or {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _api_token(self) -> str:
        return self._config.get("api_token", "")

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_token()}",
            "Content-Type": "application/json",
        }

    def _raise_for_status(self, status: int, body: Dict[str, Any], context: str) -> Dict[str, Any]:
        """Map HTTP status codes to typed Coda exceptions."""
        if status == 200:
            return body
        if status in (401, 403):
            message = body.get("message", "Unauthorized")
            raise CodaAuthError(f"[{context}] Auth error ({status}): {message}")
        if status == 404:
            message = body.get("message", "Not found")
            raise CodaNotFoundError(f"[{context}] Not found (404): {message}")
        if status == 429:
            raise CodaRateLimitError(f"[{context}] Rate limited (429)")
        if status >= 500:
            message = body.get("message", f"HTTP {status}")
            raise CodaNetworkError(f"[{context}] Server error ({status}): {message}")
        message = body.get("message", f"HTTP {status}")
        raise CodaError(f"[{context}] Coda API error ({status}): {message}")

    async def get_who_am_i(self) -> Dict[str, Any]:
        """GET /whoami — retrieve the identity of the current API token holder."""
        url = f"{self._base_url}/whoami"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise CodaNetworkError(f"[get_who_am_i] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_who_am_i")

    async def get_docs(
        self,
        limit: int = _DEFAULT_PAGE_SIZE,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /docs — list accessible docs.

        Returns ``{"items": [...], "nextPageToken": "...", "nextPageLink": "..."}``.
        """
        url = f"{self._base_url}/docs"
        params: Dict[str, Any] = {"limit": limit}
        if page_token:
            params["pageToken"] = page_token
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers(), params=params) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise CodaNetworkError(f"[get_docs] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_docs")

    async def get_doc(self, doc_id: str) -> Dict[str, Any]:
        """GET /docs/{docId} — retrieve a single doc."""
        url = f"{self._base_url}/docs/{doc_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise CodaNetworkError(f"[get_doc] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_doc")

    async def get_pages(
        self,
        doc_id: str,
        limit: int = _DEFAULT_PAGE_SIZE_LARGE,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /docs/{docId}/pages — list pages within a doc."""
        url = f"{self._base_url}/docs/{doc_id}/pages"
        params: Dict[str, Any] = {"limit": limit}
        if page_token:
            params["pageToken"] = page_token
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers(), params=params) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise CodaNetworkError(f"[get_pages] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_pages")

    async def get_tables(
        self,
        doc_id: str,
        limit: int = _DEFAULT_PAGE_SIZE_LARGE,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /docs/{docId}/tables — list tables (and views) within a doc."""
        url = f"{self._base_url}/docs/{doc_id}/tables"
        params: Dict[str, Any] = {"limit": limit}
        if page_token:
            params["pageToken"] = page_token
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers(), params=params) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise CodaNetworkError(f"[get_tables] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_tables")

    async def get_rows(
        self,
        doc_id: str,
        table_id: str,
        limit: int = _DEFAULT_ROW_PAGE_SIZE,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /docs/{docId}/tables/{tableId}/rows — list rows in a table.

        Uses ``valueFormat=rich`` so cell values include type information.
        """
        url = f"{self._base_url}/docs/{doc_id}/tables/{table_id}/rows"
        params: Dict[str, Any] = {"limit": limit, "valueFormat": "rich"}
        if page_token:
            params["pageToken"] = page_token
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers(), params=params) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise CodaNetworkError(f"[get_rows] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_rows")
