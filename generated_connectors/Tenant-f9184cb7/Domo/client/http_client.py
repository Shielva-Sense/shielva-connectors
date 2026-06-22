"""Domo connector — Domo REST API HTTP client.

Auth flow: GET /oauth/token with BasicAuth(client_id, client_secret)
           → access_token stored in config
All API calls: Authorization: Bearer {access_token}
Pagination: limit + offset parameters. Responses are plain JSON lists.
"""
from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import aiohttp

from exceptions import (
    DomoAuthError,
    DomoError,
    DomoNetworkError,
    DomoNotFoundError,
    DomoRateLimitError,
)

_DOMO_BASE = "https://api.domo.com"
_TOKEN_URL = "https://api.domo.com/oauth/token"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_PAGE_SIZE = 50


class DomoHTTPClient:
    """Thin async HTTP wrapper for the Domo REST API.

    Token acquisition:
        GET /oauth/token?grant_type=client_credentials&scope=data%20user%20dashboard
        with BasicAuth(client_id, client_secret)
        → stores access_token in self._config["access_token"]

    All resource endpoints:
        GET /v1/<resource>?limit=<n>&offset=<n>
        with Authorization: Bearer <access_token>
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        base_url: str = _DOMO_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._config = config or {}
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # ── internals ─────────────────────────────────────────────────────────────

    def _client_id(self) -> str:
        return str(self._config.get("client_id", ""))

    def _client_secret(self) -> str:
        return str(self._config.get("client_secret", ""))

    def _access_token(self) -> str:
        return str(self._config.get("access_token", ""))

    def _bearer_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _basic_auth_header(self) -> str:
        """Return Base64-encoded Basic auth header value for token endpoint."""
        credentials = f"{self._client_id()}:{self._client_secret()}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    def _raise_for_status(
        self, status: int, body: Any, context: str
    ) -> Any:
        """Map HTTP status codes to typed Domo exceptions."""
        if 200 <= status < 300:
            return body
        # body may be a dict or a list depending on endpoint
        if isinstance(body, dict):
            err_msg = body.get(
                "message",
                body.get("error", body.get("statusReason", f"HTTP {status}")),
            )
        else:
            err_msg = f"HTTP {status}"
        if status in (401, 403):
            raise DomoAuthError(f"[{context}] Auth error ({status}): {err_msg}")
        if status == 404:
            raise DomoNotFoundError(f"[{context}] Not found (404): {err_msg}")
        if status == 429:
            raise DomoRateLimitError(f"[{context}] Rate limited (429): {err_msg}")
        raise DomoError(f"[{context}] Domo API error ({status}): {err_msg}")

    # ── auth ─────────────────────────────────────────────────────────────────

    async def get_token(self) -> Dict[str, Any]:
        """GET /oauth/token — acquire an OAuth2 client credentials access token.

        Uses BasicAuth(client_id, client_secret).
        Stores the token in self._config["access_token"] for subsequent calls.
        Returns the full token response dict.
        """
        url = (
            f"{self._base_url}/oauth/token"
            "?grant_type=client_credentials&scope=data%20user%20dashboard"
        )
        headers = {
            "Authorization": self._basic_auth_header(),
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    status = resp.status
                    data: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DomoNetworkError(f"[get_token] Network error: {exc}") from exc
        result: Dict[str, Any] = self._raise_for_status(status, data, "get_token")
        # Store token for subsequent requests
        self._config["access_token"] = result.get("access_token", "")
        return result

    # ── datasets ─────────────────────────────────────────────────────────────

    async def list_datasets(
        self, limit: int = _DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """GET /v1/datasets?limit={limit}&offset={offset} — list datasets."""
        url = f"{self._base_url}/v1/datasets"
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url, headers=self._bearer_headers(), params=params
                ) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DomoNetworkError(f"[list_datasets] Network error: {exc}") from exc
        result = self._raise_for_status(status, data, "list_datasets")
        return result if isinstance(result, list) else []

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """GET /v1/datasets/{dataset_id} — retrieve a single dataset."""
        url = f"{self._base_url}/v1/datasets/{dataset_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._bearer_headers()) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DomoNetworkError(f"[get_dataset] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_dataset")

    # ── pages (dashboards) ────────────────────────────────────────────────────

    async def list_pages(
        self, limit: int = _DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """GET /v1/pages?limit={limit}&offset={offset} — list dashboard pages."""
        url = f"{self._base_url}/v1/pages"
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url, headers=self._bearer_headers(), params=params
                ) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DomoNetworkError(f"[list_pages] Network error: {exc}") from exc
        result = self._raise_for_status(status, data, "list_pages")
        return result if isinstance(result, list) else []

    async def get_page(self, page_id: int) -> Dict[str, Any]:
        """GET /v1/pages/{page_id} — retrieve a single dashboard page."""
        url = f"{self._base_url}/v1/pages/{page_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._bearer_headers()) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DomoNetworkError(f"[get_page] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_page")

    # ── users ─────────────────────────────────────────────────────────────────

    async def list_users(
        self, limit: int = 500, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """GET /v1/users?limit={limit}&offset={offset} — list users."""
        url = f"{self._base_url}/v1/users"
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url, headers=self._bearer_headers(), params=params
                ) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DomoNetworkError(f"[list_users] Network error: {exc}") from exc
        result = self._raise_for_status(status, data, "list_users")
        return result if isinstance(result, list) else []

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        """GET /v1/users/{user_id} — retrieve a single user."""
        url = f"{self._base_url}/v1/users/{user_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._bearer_headers()) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DomoNetworkError(f"[get_user] Network error: {exc}") from exc
        return self._raise_for_status(status, data, "get_user")

    # ── groups ────────────────────────────────────────────────────────────────

    async def list_groups(
        self, limit: int = 500, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """GET /v1/groups?limit={limit}&offset={offset} — list groups."""
        url = f"{self._base_url}/v1/groups"
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url, headers=self._bearer_headers(), params=params
                ) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DomoNetworkError(f"[list_groups] Network error: {exc}") from exc
        result = self._raise_for_status(status, data, "list_groups")
        return result if isinstance(result, list) else []
