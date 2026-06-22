from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    WordPressAuthError,
    WordPressNetworkError,
    WordPressNotFoundError,
    WordPressRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_PER_PAGE = 100


def _api_base(site_url: str) -> str:
    """Return the WordPress REST API v2 base URL for a given site."""
    return site_url.rstrip("/") + "/wp-json/wp/v2"


class WordPressHTTPClient:
    """Low-level async HTTP client for the WordPress REST API v2.

    Uses HTTP Basic Auth with WordPress Application Passwords (WP 5.6+).
    Authentication: Authorization: Basic base64(username:app_password)
    """

    def __init__(self, config: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        cfg = config or {}
        self._site_url: str = cfg.get("site_url", "")
        self._username: str = cfg.get("username", "")
        self._app_password: str = cfg.get("app_password", "")

    def _auth(self) -> aiohttp.BasicAuth:
        """Build aiohttp BasicAuth from username and application password."""
        return aiohttp.BasicAuth(login=self._username, password=self._app_password)

    def _base_url(self) -> str:
        return _api_base(self._site_url)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        site_url: str = "",
        username: str = "",
        app_password: str = "",
    ) -> tuple[Any, dict[str, str]]:
        """Execute a request and return (parsed_body, response_headers)."""
        # Allow per-call overrides (used in tests / multi-site scenarios)
        base = _api_base(site_url or self._site_url)
        auth = aiohttp.BasicAuth(
            login=username or self._username,
            password=app_password or self._app_password,
        )
        url = f"{base}{path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "Shielva-WordPress-Connector/1.0.0",
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, params=params, headers=headers, auth=auth, ssl=False
                ) as response:
                    resp_headers = dict(response.headers)
                    status = response.status

                    if status == 200:
                        body = await response.json(content_type=None)
                        return body, resp_headers

                    # Try to parse error body for a human-readable message
                    try:
                        error_body: dict[str, Any] = await response.json(content_type=None)
                    except Exception:
                        error_body = {}

                    err_msg: str = (
                        (error_body.get("message") or error_body.get("error", ""))
                        if isinstance(error_body, dict)
                        else str(error_body)
                    ) or f"HTTP {status}"
                    err_code: str = (
                        error_body.get("code", "") if isinstance(error_body, dict) else ""
                    )

                    self._raise_for_status(status, err_msg, err_code, resp_headers)

        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise WordPressNetworkError(f"Network error: {exc}") from exc
        except (WordPressAuthError, WordPressNotFoundError, WordPressRateLimitError, WordPressNetworkError):
            raise
        except Exception as exc:
            raise WordPressNetworkError(f"Unexpected error: {exc}") from exc

        # unreachable — _raise_for_status always raises for non-200
        raise WordPressNetworkError("Unexpected empty response")

    def _raise_for_status(
        self,
        status: int,
        err_msg: str,
        err_code: str,
        resp_headers: dict[str, str],
    ) -> None:
        """Map HTTP status codes to typed exceptions."""
        if status in (401, 403):
            raise WordPressAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code=err_code,
            )
        if status == 404:
            raise WordPressNotFoundError("resource", err_code or "unknown")
        if status == 429:
            retry_after = float(resp_headers.get("Retry-After", "0"))
            raise WordPressRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise WordPressNetworkError(
                f"WordPress server error {status}: {err_msg}",
                status_code=status,
                code=err_code,
            )
        raise WordPressNetworkError(
            f"Unexpected HTTP {status}: {err_msg}",
            status_code=status,
            code=err_code,
        )

    # ── Auth / Identity ───────────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """GET /users/me — verify credentials and retrieve authenticated user info."""
        body, _ = await self._request("GET", "/users/me")
        return body  # type: ignore[return-value]

    # ── Posts ─────────────────────────────────────────────────────────────────

    async def get_posts(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        status: str = "any",
        **params: Any,
    ) -> list[dict[str, Any]]:
        """GET /posts — paginated list of posts."""
        query: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "status": status,
            **params,
        }
        body, _ = await self._request("GET", "/posts", params=query)
        return body  # type: ignore[return-value]

    async def get_posts_with_headers(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        status: str = "any",
        **params: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """GET /posts — returns (items, headers) so caller can read X-WP-TotalPages."""
        query: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "status": status,
            **params,
        }
        body, headers = await self._request("GET", "/posts", params=query)
        return body, headers  # type: ignore[return-value]

    async def get_post(self, post_id: int) -> dict[str, Any]:
        """GET /posts/{id} — single post."""
        body, _ = await self._request("GET", f"/posts/{post_id}")
        return body  # type: ignore[return-value]

    # ── Pages ─────────────────────────────────────────────────────────────────

    async def get_pages(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        status: str = "any",
    ) -> list[dict[str, Any]]:
        """GET /pages — paginated list of pages."""
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "status": status,
        }
        body, _ = await self._request("GET", "/pages", params=params)
        return body  # type: ignore[return-value]

    async def get_pages_with_headers(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        status: str = "any",
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """GET /pages — returns (items, headers) including X-WP-TotalPages."""
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "status": status,
        }
        body, headers = await self._request("GET", "/pages", params=params)
        return body, headers  # type: ignore[return-value]

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> list[dict[str, Any]]:
        """GET /users — paginated list of users."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        body, _ = await self._request("GET", "/users", params=params)
        return body  # type: ignore[return-value]

    async def get_users_with_headers(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """GET /users — returns (items, headers)."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        body, headers = await self._request("GET", "/users", params=params)
        return body, headers  # type: ignore[return-value]

    # ── Media ─────────────────────────────────────────────────────────────────

    async def get_media(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> list[dict[str, Any]]:
        """GET /media — paginated list of media items."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        body, _ = await self._request("GET", "/media", params=params)
        return body  # type: ignore[return-value]

    async def get_media_with_headers(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """GET /media — returns (items, headers)."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        body, headers = await self._request("GET", "/media", params=params)
        return body, headers  # type: ignore[return-value]

    # ── Categories ────────────────────────────────────────────────────────────

    async def get_categories(
        self,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> list[dict[str, Any]]:
        """GET /categories — list of all categories."""
        params: dict[str, Any] = {"per_page": per_page}
        body, _ = await self._request("GET", "/categories", params=params)
        return body  # type: ignore[return-value]

    # ── Tags ──────────────────────────────────────────────────────────────────

    async def get_tags(
        self,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> list[dict[str, Any]]:
        """GET /tags — list of all tags."""
        params: dict[str, Any] = {"per_page": per_page}
        body, _ = await self._request("GET", "/tags", params=params)
        return body  # type: ignore[return-value]
