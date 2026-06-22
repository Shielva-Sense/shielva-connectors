from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    ProductboardAuthError,
    ProductboardError,
    ProductboardNetworkError,
    ProductboardNotFoundError,
    ProductboardRateLimitError,
)

BASE_URL: str = "https://api.productboard.com"
DEFAULT_TIMEOUT_S: float = 30.0
API_VERSION: str = "1"


class ProductboardHTTPClient:
    """Low-level async HTTP client for the Productboard Public API.

    Authentication uses Bearer token sent in the Authorization header.
    The ``X-Version: 1`` header is required on every request.

    Productboard wraps all successful list responses in
    ``{"data": [...], "links": {"next": "<cursor_url>"}}``; this client
    preserves that envelope so callers can drive cursor-based pagination.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        _config = config or {}
        self._api_token: str = _config.get("api_token", "").strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    # ── Session management ────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_token}",
            "X-Version": API_VERSION,
            "Accept": "application/json",
        }

    # ── Error handling ────────────────────────────────────────────────────────

    def _raise_for_status(self, status: int, body: dict[str, Any], path: str) -> None:
        """Map HTTP status codes to typed connector exceptions."""
        err_msg = (
            body.get("error", {}).get("message", "")
            if isinstance(body.get("error"), dict)
            else body.get("message", "")
        ) or f"HTTP {status}"

        if status in (401, 403):
            raise ProductboardAuthError(
                f"Authentication failed: {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise ProductboardNotFoundError("resource", path)
        if status == 429:
            raise ProductboardRateLimitError(f"Rate limited: {err_msg}")
        if status >= 500:
            raise ProductboardNetworkError(
                f"Productboard server error {status}: {err_msg}",
                status_code=status,
            )
        raise ProductboardError(
            f"Productboard error {status}: {err_msg}",
            status_code=status,
        )

    # ── Raw request ───────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a request to either a full URL (cursor) or a path suffix."""
        # path may be a full cursor URL returned in links.next
        url = path if path.startswith("https://") else f"{BASE_URL}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(),
                params=params,
            ) as response:
                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                if response.status in (200, 201):
                    return body
                if response.status == 204:
                    return {}

                self._raise_for_status(response.status, body, path)
        except (ProductboardError,):
            raise
        except aiohttp.ClientConnectorError as exc:
            raise ProductboardNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise ProductboardNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise ProductboardNetworkError(f"Network error: {exc}") from exc

    # ── Me / current user ─────────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """GET /me — verify credentials and return the authenticated user."""
        return await self._request("GET", "/me")

    # ── Features ──────────────────────────────────────────────────────────────

    async def get_features(
        self,
        page_cursor: str | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """GET /features — list features with cursor pagination.

        Returns ``{"data": [...], "links": {"next": "<cursor_url>"}}``.
        Pass the value of ``links.next`` as ``page_cursor`` to fetch subsequent pages.
        """
        if page_cursor and page_cursor.startswith("https://"):
            # Productboard returns a full URL in links.next; use it directly
            return await self._request("GET", page_cursor)
        params: dict[str, Any] = {"page[size]": page_size}
        if page_cursor:
            params["page[before]"] = page_cursor
        return await self._request("GET", "/features", params=params)

    async def get_feature(self, feature_id: str) -> dict[str, Any]:
        """GET /features/{feature_id} — get a single feature by ID."""
        return await self._request("GET", f"/features/{feature_id}")

    # ── Components ────────────────────────────────────────────────────────────

    async def get_components(self, page_cursor: str | None = None) -> dict[str, Any]:
        """GET /components — list all components with cursor pagination."""
        if page_cursor and page_cursor.startswith("https://"):
            return await self._request("GET", page_cursor)
        params: dict[str, Any] = {}
        if page_cursor:
            params["page[before]"] = page_cursor
        return await self._request("GET", "/components", params=params or None)

    # ── Products ──────────────────────────────────────────────────────────────

    async def get_products(self) -> dict[str, Any]:
        """GET /products — list all products (no pagination in Productboard v1)."""
        return await self._request("GET", "/products")

    # ── Notes ─────────────────────────────────────────────────────────────────

    async def get_notes(
        self,
        page_cursor: str | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """GET /notes — list notes with cursor pagination."""
        if page_cursor and page_cursor.startswith("https://"):
            return await self._request("GET", page_cursor)
        params: dict[str, Any] = {"page[size]": page_size}
        if page_cursor:
            params["page[before]"] = page_cursor
        return await self._request("GET", "/notes", params=params)

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(self) -> dict[str, Any]:
        """GET /users — list all users in the workspace."""
        return await self._request("GET", "/users")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> ProductboardHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
