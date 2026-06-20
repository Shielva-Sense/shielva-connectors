"""
RingCentral HTTP client.

Responsibilities:
- Authorization: Bearer header injection
- Auto-refresh via refresh_token when access_token expires
- Pagination via paging.totalPages
- Status-code → exception mapping
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urljoin, urlencode

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

from ..exceptions import (
    RingCentralAuthError,
    RingCentralError,
    RingCentralNetworkError,
    RingCentralNotFoundError,
    RingCentralRateLimitError,
)

DEFAULT_SERVER_URL = "https://platform.ringcentral.com"
API_PATH = "/restapi/v1.0"
TOKEN_PATH = "/restapi/oauth/token"


class RingCentralHTTPClient:
    """Async HTTP client for the RingCentral REST API v1."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config or {}
        self._server_url: str = (
            self.config.get("server_url") or DEFAULT_SERVER_URL
        ).rstrip("/")
        self._access_token: str = self.config.get("access_token", "")
        self._refresh_token: str = self.config.get("refresh_token", "")
        self._client_id: str = self.config.get("client_id", "")
        self._client_secret: str = self.config.get("client_secret", "")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _api_base(self) -> str:
        return f"{self._server_url}{API_PATH}"

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _raise_for_status(self, status: int, body: dict[str, Any] | str) -> None:
        """Map HTTP status codes to typed exceptions."""
        if status < 400:
            return
        message = body if isinstance(body, str) else str(body)
        if status in (401, 403):
            raise RingCentralAuthError(
                f"Authentication error ({status}): {message}", status_code=status
            )
        if status == 404:
            raise RingCentralNotFoundError(
                f"Resource not found ({status}): {message}", status_code=status
            )
        if status == 429:
            raise RingCentralRateLimitError(
                f"Rate limit exceeded ({status}): {message}", status_code=status
            )
        if status >= 500:
            raise RingCentralNetworkError(
                f"Server error ({status}): {message}", status_code=status
            )
        raise RingCentralError(
            f"Unexpected error ({status}): {message}", status_code=status
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform an authenticated GET request."""
        url = f"{self._api_base}{path}"
        if not _HAS_AIOHTTP:
            raise RingCentralNetworkError(
                "aiohttp is required. Install it with: pip install aiohttp"
            )
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, headers=self._auth_headers(), ssl=True
            ) as resp:
                try:
                    body = await resp.json()
                except Exception:
                    body = await resp.text()
                self._raise_for_status(resp.status, body)
                return body if isinstance(body, dict) else {"raw": body}

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def refresh_access_token(self) -> dict[str, Any]:
        """Use the stored refresh_token to obtain a new access_token."""
        if not _HAS_AIOHTTP:
            raise RingCentralNetworkError(
                "aiohttp is required. Install it with: pip install aiohttp"
            )
        token_url = f"{self._server_url}{TOKEN_PATH}"
        credentials = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        payload = urlencode(
            {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                token_url, data=payload, headers=headers, ssl=True
            ) as resp:
                try:
                    body = await resp.json()
                except Exception:
                    body = await resp.text()
                self._raise_for_status(resp.status, body)
                result = body if isinstance(body, dict) else {}
                # Update internal state
                self._access_token = result.get("access_token", self._access_token)
                self._refresh_token = result.get("refresh_token", self._refresh_token)
                return result

    # ------------------------------------------------------------------
    # Resource endpoints
    # ------------------------------------------------------------------

    async def get_extension_info(self) -> dict[str, Any]:
        """GET /account/~/extension/~ — used for health_check."""
        return await self._get("/account/~/extension/~")

    async def get_call_logs(
        self, page: int = 1, per_page: int = 100, **params: Any
    ) -> dict[str, Any]:
        """GET /account/~/call-log — returns {"records": [...], "paging": {...}}."""
        query: dict[str, Any] = {"page": page, "perPage": per_page, **params}
        return await self._get("/account/~/call-log", params=query)

    async def get_messages(
        self, page: int = 1, per_page: int = 100, **params: Any
    ) -> dict[str, Any]:
        """GET /account/~/extension/~/message-store."""
        query: dict[str, Any] = {"page": page, "perPage": per_page, **params}
        return await self._get("/account/~/extension/~/message-store", params=query)

    async def get_extensions(
        self, page: int = 1, per_page: int = 100, **params: Any
    ) -> dict[str, Any]:
        """GET /account/~/extension."""
        query: dict[str, Any] = {"page": page, "perPage": per_page, **params}
        return await self._get("/account/~/extension", params=query)

    async def get_contacts(
        self, page: int = 1, per_page: int = 250, **params: Any
    ) -> dict[str, Any]:
        """GET /account/~/extension/~/address-book/contact."""
        query: dict[str, Any] = {"page": page, "perPage": per_page, **params}
        return await self._get(
            "/account/~/extension/~/address-book/contact", params=query
        )

    async def get_meetings(
        self, page: int = 1, per_page: int = 100, **params: Any
    ) -> dict[str, Any]:
        """GET /account/~/extension/~/meeting."""
        query: dict[str, Any] = {"page": page, "perPage": per_page, **params}
        return await self._get("/account/~/extension/~/meeting", params=query)

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------

    async def paginate_all(
        self,
        fetch_fn: Any,
        per_page: int = 100,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a list endpoint using paging.totalPages."""
        all_records: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await fetch_fn(page=page, per_page=per_page, **kwargs)
            records: list[dict[str, Any]] = response.get("records", [])
            all_records.extend(records)
            paging: dict[str, Any] = response.get("paging", {})
            total_pages: int = int(paging.get("totalPages", 1))
            if page >= total_pages:
                break
            page += 1
        return all_records
