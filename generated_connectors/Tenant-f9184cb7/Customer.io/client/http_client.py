from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    CustomerIOAuthError,
    CustomerIOError,
    CustomerIONetworkError,
    CustomerIONotFoundError,
    CustomerIORateLimitError,
    CustomerIOServerError,
)

CUSTOMERIO_BASE_URL = "https://api.customer.io/v1"
DEFAULT_TIMEOUT_S: float = 30.0


class CustomerIOHTTPClient:
    """Low-level async HTTP client for the Customer.io App API v1.

    Uses aiohttp.ClientSession with Bearer token authentication.
    Base URL: https://api.customer.io/v1
    Auth: Authorization: Bearer {app_api_key}
    """

    def __init__(self, app_api_key: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._app_api_key = app_api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._headers = {
            "Authorization": f"Bearer {app_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                timeout=self._timeout,
            )
        return self._session

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        session = self._get_session()
        full_url = f"{CUSTOMERIO_BASE_URL}{url}"
        try:
            async with session.request(method, full_url, **kwargs) as response:
                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass
                return self._raise_for_status(response, body)
        except CustomerIOError:
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise CustomerIONetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            raise CustomerIONetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise CustomerIONetworkError(f"Network error: {exc}") from exc

    def _raise_for_status(
        self, response: aiohttp.ClientResponse, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Map HTTP status codes to typed exceptions; return body on success."""
        status = response.status

        if status in (200, 201, 202, 204):
            return body if body else {}

        # Customer.io error responses have a "meta" or "errors" key
        meta = body.get("meta", {}) or {}
        err_msg: str = (
            meta.get("error", "")
            or body.get("message", "")
            or f"HTTP {status}"
        )
        err_code: str = body.get("code", "")

        if status == 401:
            raise CustomerIOAuthError(f"Authentication failed: {err_msg}", 401, err_code)
        if status == 403:
            raise CustomerIOAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if status == 404:
            raise CustomerIONotFoundError("resource", str(response.url))
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise CustomerIORateLimitError(f"Rate limited: {err_msg}", retry_after)
        if status >= 500:
            raise CustomerIOServerError(
                f"Customer.io server error {status}: {err_msg}",
                status,
            )

        raise CustomerIOError(
            f"Customer.io error {status}: {err_msg}",
            status,
            err_code,
        )

    # ── Workspaces ───────────────────────────────────────────────────────────

    async def get_workspaces(self) -> dict[str, Any]:
        """GET /workspaces — used for health check and install validation."""
        return await self._request("GET", "/workspaces")

    # ── Customers ────────────────────────────────────────────────────────────

    async def get_customers(
        self,
        start: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """GET /customers — list all customers with optional cursor and limit."""
        params: dict[str, Any] = {"limit": limit}
        if start:
            params["start"] = start
        return await self._request("GET", "/customers", params=params)

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        """GET /customers/{customer_id} — fetch a single customer."""
        return await self._request("GET", f"/customers/{customer_id}")

    # ── Campaigns ────────────────────────────────────────────────────────────

    async def get_campaigns(self) -> dict[str, Any]:
        """GET /campaigns — list of campaigns."""
        return await self._request("GET", "/campaigns")

    async def get_campaign(self, campaign_id: int | str) -> dict[str, Any]:
        """GET /campaigns/{campaign_id} — fetch a single campaign."""
        return await self._request("GET", f"/campaigns/{campaign_id}")

    # ── Broadcasts ───────────────────────────────────────────────────────────

    async def get_broadcasts(self) -> dict[str, Any]:
        """GET /broadcasts — list one-time email broadcasts."""
        return await self._request("GET", "/broadcasts")

    # ── Newsletters ──────────────────────────────────────────────────────────

    async def get_newsletters(self) -> dict[str, Any]:
        """GET /newsletters — list newsletters."""
        return await self._request("GET", "/newsletters")

    # ── Segments ─────────────────────────────────────────────────────────────

    async def get_segments(self) -> dict[str, Any]:
        """GET /segments — list segments."""
        return await self._request("GET", "/segments")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> CustomerIOHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
