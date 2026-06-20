from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    AhaAuthError,
    AhaError,
    AhaNetworkError,
    AhaNotFoundError,
    AhaRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_PER_PAGE: int = 200


def _build_base_url(subdomain: str) -> str:
    """Construct the Aha! REST API v1 base URL for the given subdomain."""
    return f"https://{subdomain}.aha.io/api/v1"


class AhaHTTPClient:
    """Low-level async HTTP client for the Aha! REST API v1.

    Authentication uses a Bearer token (API key) sent in the Authorization
    header.  Pagination is page-number based; each response includes a
    ``pagination`` object with ``total_pages``.

    The base URL is per-subdomain: ``https://{subdomain}.aha.io/api/v1``.
    """

    def __init__(self, subdomain: str = "", timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._subdomain = subdomain
        self._base_url = _build_base_url(subdomain) if subdomain else ""
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse, path: str
    ) -> None:
        """Raise a typed exception for non-2xx responses."""
        if response.status in (200, 201):
            return

        body_raw: dict[str, Any] = {}
        try:
            body_raw = await response.json(content_type=None)
        except Exception:
            pass

        # Aha! returns errors as {"errors": ["msg"]} or {"error": "msg"}
        errors = body_raw.get("errors", [])
        if isinstance(errors, list) and errors:
            err_msg = str(errors[0])
        else:
            err_msg = body_raw.get("error", "") or f"HTTP {response.status}"

        if response.status in (401, 403):
            raise AhaAuthError(
                f"Authentication failed: {err_msg}",
                status_code=response.status,
                code="auth_error",
            )
        if response.status == 404:
            raise AhaNotFoundError("resource", path)
        if response.status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise AhaRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if response.status >= 500:
            raise AhaNetworkError(
                f"Aha! server error {response.status}: {err_msg}",
                status_code=response.status,
            )
        raise AhaError(
            f"Aha! error {response.status}: {err_msg}",
            status_code=response.status,
        )

    async def _request(
        self,
        method: str,
        api_key: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(api_key),
                params=params,
            ) as response:
                await self._raise_for_status(response, path)
                if response.status == 204:
                    return {}
                return await response.json(content_type=None)
        except AhaError:
            raise
        except aiohttp.ClientConnectorError as exc:
            raise AhaNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise AhaNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise AhaNetworkError(f"Network error: {exc}") from exc

    # ── Me / User ─────────────────────────────────────────────────────────────

    async def get_me(self, api_key: str) -> dict[str, Any]:
        """GET /api/v1/me — return current user info."""
        return await self._request("GET", api_key, "/me")

    # ── Products ──────────────────────────────────────────────────────────────

    async def get_products(self, api_key: str, page: int = 1) -> dict[str, Any]:
        """GET /api/v1/products — list all products/workspaces with pagination."""
        params: dict[str, Any] = {"page": page, "per_page": DEFAULT_PER_PAGE}
        return await self._request("GET", api_key, "/products", params=params)

    # ── Features ─────────────────────────────────────────────────────────────

    async def get_features(
        self, api_key: str, product_id: str, page: int = 1
    ) -> dict[str, Any]:
        """GET /api/v1/products/{product_id}/features — list features with pagination."""
        params: dict[str, Any] = {"page": page, "per_page": DEFAULT_PER_PAGE}
        return await self._request(
            "GET", api_key, f"/products/{product_id}/features", params=params
        )

    async def get_feature(self, api_key: str, feature_id: str) -> dict[str, Any]:
        """GET /api/v1/features/{feature_id} — get a single feature by ID."""
        return await self._request("GET", api_key, f"/features/{feature_id}")

    # ── Goals ─────────────────────────────────────────────────────────────────

    async def get_goals(self, api_key: str, product_id: str) -> dict[str, Any]:
        """GET /api/v1/products/{product_id}/goals — list goals for a product."""
        return await self._request("GET", api_key, f"/products/{product_id}/goals")

    # ── Releases ─────────────────────────────────────────────────────────────

    async def get_releases(
        self, api_key: str, product_id: str, page: int = 1
    ) -> dict[str, Any]:
        """GET /api/v1/products/{product_id}/releases — list releases with pagination."""
        params: dict[str, Any] = {"page": page, "per_page": DEFAULT_PER_PAGE}
        return await self._request(
            "GET", api_key, f"/products/{product_id}/releases", params=params
        )

    # ── Ideas ─────────────────────────────────────────────────────────────────

    async def get_ideas(
        self, api_key: str, product_id: str, page: int = 1
    ) -> dict[str, Any]:
        """GET /api/v1/products/{product_id}/ideas — list ideas with pagination."""
        params: dict[str, Any] = {"page": page, "per_page": DEFAULT_PER_PAGE}
        return await self._request(
            "GET", api_key, f"/products/{product_id}/ideas", params=params
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> AhaHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
