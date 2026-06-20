from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    ConfluenceAuthError,
    ConfluenceError,
    ConfluenceNetworkError,
    ConfluenceNotFoundError,
    ConfluenceRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0


class ConfluenceHTTPClient:
    """Low-level async HTTP client for the Confluence REST API.

    Supports both Confluence v2 (spaces, pages, blogposts) and
    the legacy REST API (user/current, search).
    Authentication: HTTP Basic Auth — Atlassian account email + API token.
    """

    def __init__(
        self,
        domain: str,
        email: str,
        api_token: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._domain = domain
        self._email = email
        self._api_token = api_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._base_v2 = f"https://{domain}.atlassian.net/wiki/api/v2"
        self._base_rest = f"https://{domain}.atlassian.net/wiki/rest/api"
        self._auth = aiohttp.BasicAuth(email, api_token)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                auth=self._auth,
                timeout=self._timeout,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        return self._session

    async def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> dict[str, Any]:
        session = self._get_session()
        try:
            async with session.request(method, url, **kwargs) as response:
                return await self._raise_for_status(response)
        except aiohttp.ServerTimeoutError as exc:
            raise ConfluenceNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise ConfluenceNetworkError(f"Connection error: {exc}") from exc
        except (ConfluenceError, Exception):
            raise

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        if response.status in (200, 201, 204):
            if response.status == 204 or response.content_length == 0:
                return {}
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_msg = (
            body.get("message", "")
            or body.get("statusMessage", "")
            or str(body)
            or "Unknown Confluence error"
        )

        if response.status == 401:
            raise ConfluenceAuthError(
                f"Authentication failed: {err_msg}", 401, "unauthorized"
            )
        if response.status == 403:
            raise ConfluenceAuthError(f"Forbidden: {err_msg}", 403, "forbidden")
        if response.status == 404:
            url_str = str(response.url)
            raise ConfluenceNotFoundError("resource", url_str)
        if response.status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise ConfluenceRateLimitError(f"Rate limited: {err_msg}", retry_after)
        raise ConfluenceError(
            f"Confluence error {response.status}: {err_msg}",
            response.status,
        )

    # ── Auth probe ─────────────────────────────────────────────────────────────

    async def get_current_user(self) -> dict[str, Any]:
        """Probe endpoint — GET /wiki/rest/api/user/current. Used for install/health-check."""
        return await self._request("GET", f"{self._base_rest}/user/current")

    # ── Spaces ────────────────────────────────────────────────────────────────

    async def list_spaces(
        self,
        limit: int = 250,
        cursor: str | None = None,
        type: str | None = None,
    ) -> dict[str, Any]:
        """GET /wiki/api/v2/spaces — cursor-based paginated space list."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if type:
            params["type"] = type
        return await self._request(
            "GET",
            f"{self._base_v2}/spaces",
            params=params,
        )

    async def get_space(self, space_id: str) -> dict[str, Any]:
        """GET /wiki/api/v2/spaces/{space_id} — single space by ID."""
        return await self._request(
            "GET",
            f"{self._base_v2}/spaces/{space_id}",
        )

    # ── Pages ─────────────────────────────────────────────────────────────────

    async def list_pages(
        self,
        space_id: str | None = None,
        limit: int = 250,
        cursor: str | None = None,
        status: str = "current",
    ) -> dict[str, Any]:
        """GET /wiki/api/v2/pages — cursor-paginated page list, optionally filtered by space."""
        params: dict[str, Any] = {"limit": limit, "status": status}
        if space_id:
            params["spaceId"] = space_id
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET",
            f"{self._base_v2}/pages",
            params=params,
        )

    async def get_page(self, page_id: str) -> dict[str, Any]:
        """GET /wiki/api/v2/pages/{page_id}?body-format=storage — single page with body."""
        return await self._request(
            "GET",
            f"{self._base_v2}/pages/{page_id}",
            params={"body-format": "storage"},
        )

    async def get_page_children(
        self,
        page_id: str,
        limit: int = 250,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /wiki/api/v2/pages/{page_id}/children — child pages of a given page."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET",
            f"{self._base_v2}/pages/{page_id}/children",
            params=params,
        )

    # ── Blog posts ────────────────────────────────────────────────────────────

    async def list_blogposts(
        self,
        space_id: str | None = None,
        limit: int = 250,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /wiki/api/v2/blogposts — cursor-paginated blog post list, optionally filtered by space."""
        params: dict[str, Any] = {"limit": limit}
        if space_id:
            params["spaceId"] = space_id
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET",
            f"{self._base_v2}/blogposts",
            params=params,
        )

    # Keep legacy alias for internal sync use
    async def list_blog_posts(
        self,
        space_id: str | None = None,
        limit: int = 250,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Alias for list_blogposts — retained for backward compatibility."""
        return await self.list_blogposts(space_id=space_id, limit=limit, cursor=cursor)

    # ── Search ────────────────────────────────────────────────────────────────

    async def search_content(
        self,
        query: str,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /wiki/rest/api/search?cql=text~"{query}" — CQL-based full-text search."""
        cql = f'text~"{query}"'
        params: dict[str, Any] = {"cql": cql, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET",
            f"{self._base_rest}/search",
            params=params,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> ConfluenceHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
