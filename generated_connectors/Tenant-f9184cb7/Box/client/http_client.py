"""All Box API HTTP calls — zero business logic, zero normalization.

Uses httpx async client. Each method accepts an access_token and returns the
raw parsed JSON dict. Retry and backoff are handled by the caller via
helpers/utils.with_retry().
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    BoxAuthError,
    BoxNetworkError,
    BoxNotFoundError,
    BoxRateLimitError,
)

logger = structlog.get_logger(__name__)

_BOX_BASE = "https://api.box.com/2.0"


class BoxHTTPClient:
    """Thin async HTTP client for the Box Content API (v2).

    All methods accept *access_token* and return raw response dicts.
    Never interprets business logic — callers own retry and normalization.
    """

    def __init__(self, base_url: str = _BOX_BASE) -> None:
        self._base_url = base_url.rstrip("/")

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def _raise_for_status(
        self, response: httpx.Response, context: str = ""
    ) -> None:
        """Map HTTP error codes to connector exceptions."""
        status = response.status_code
        if status < 400:
            return

        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {}

        message = body.get("message", "") or str(body)
        ctx = f": {context}" if context else ""

        if status == 401:
            raise BoxAuthError(f"401 Unauthorized{ctx}: {message}")
        if status == 404:
            raise BoxNotFoundError(f"404 Not Found{ctx}: {message}")
        if status == 429:
            raise BoxRateLimitError(f"429 Rate limit exceeded{ctx}")
        raise BoxNetworkError(f"HTTP {status}{ctx}: {message}")

    async def get_current_user(self, access_token: str) -> Dict[str, Any]:
        """GET /users/me — verify token works and fetch current user."""
        url = f"{self._base_url}/users/me"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=self._auth_headers(access_token))
            except httpx.RequestError as exc:
                raise BoxNetworkError(
                    f"Network error in get_current_user: {exc}"
                ) from exc
        await self._raise_for_status(resp, "get_current_user")
        return resp.json()

    async def get_folder_items(
        self,
        access_token: str,
        folder_id: str = "0",
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /folders/{folder_id}/items — list folder contents.

        Box returns {total_count, entries, offset, limit}.
        """
        url = f"{self._base_url}/folders/{folder_id}/items"
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "fields": "id,type,name,size,modified_at,created_at,parent,sha1,description",
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    url,
                    headers=self._auth_headers(access_token),
                    params=params,
                )
            except httpx.RequestError as exc:
                raise BoxNetworkError(
                    f"Network error in get_folder_items({folder_id}): {exc}"
                ) from exc
        await self._raise_for_status(resp, f"get_folder_items({folder_id})")
        return resp.json()

    async def get_file(self, access_token: str, file_id: str) -> Dict[str, Any]:
        """GET /files/{file_id} — fetch file metadata."""
        url = f"{self._base_url}/files/{file_id}"
        params: Dict[str, Any] = {
            "fields": "id,type,name,size,modified_at,created_at,parent,sha1,description,file_version,owned_by,shared_link",
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    url,
                    headers=self._auth_headers(access_token),
                    params=params,
                )
            except httpx.RequestError as exc:
                raise BoxNetworkError(
                    f"Network error in get_file({file_id}): {exc}"
                ) from exc
        await self._raise_for_status(resp, f"get_file({file_id})")
        return resp.json()

    async def get_folder(self, access_token: str, folder_id: str) -> Dict[str, Any]:
        """GET /folders/{folder_id} — fetch folder metadata."""
        url = f"{self._base_url}/folders/{folder_id}"
        params: Dict[str, Any] = {
            "fields": "id,type,name,size,modified_at,created_at,parent,description,owned_by",
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    url,
                    headers=self._auth_headers(access_token),
                    params=params,
                )
            except httpx.RequestError as exc:
                raise BoxNetworkError(
                    f"Network error in get_folder({folder_id}): {exc}"
                ) from exc
        await self._raise_for_status(resp, f"get_folder({folder_id})")
        return resp.json()

    async def search(
        self,
        access_token: str,
        query: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /search — search Box for files and folders.

        Returns {total_count, entries, offset, limit}.
        """
        url = f"{self._base_url}/search"
        params: Dict[str, Any] = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "fields": "id,type,name,size,modified_at,created_at,parent,sha1,description",
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    url,
                    headers=self._auth_headers(access_token),
                    params=params,
                )
            except httpx.RequestError as exc:
                raise BoxNetworkError(
                    f"Network error in search({query!r}): {exc}"
                ) from exc
        await self._raise_for_status(resp, f"search({query!r})")
        return resp.json()

    async def post_form_data(
        self,
        url: str,
        payload: Dict[str, str],
        context: str = "post_form_data",
    ) -> Dict[str, Any]:
        """Generic POST of form-encoded data — used for OAuth token operations.

        Returns parsed JSON. All auth field naming stays in connector.py.
        """
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, data=payload)
            except httpx.RequestError as exc:
                raise BoxNetworkError(
                    f"Network error in {context}: {exc}"
                ) from exc
        await self._raise_for_status(resp, context)
        return resp.json()
