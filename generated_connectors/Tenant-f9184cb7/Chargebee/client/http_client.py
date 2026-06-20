from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    ChargebeeAuthError,
    ChargebeeError,
    ChargebeeNetworkError,
    ChargebeeNotFoundError,
    ChargebeeRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0


def _base_url(site: str) -> str:
    """Return the Chargebee API v2 base URL for the given site name."""
    site = site.strip().rstrip("/")
    return f"https://{site}.chargebee.com/api/v2"


class ChargebeeHTTPClient:
    """Low-level async HTTP client for the Chargebee REST API v2.

    Authentication uses HTTP Basic Auth with the API key as the username and
    an empty string as the password, per Chargebee's specification.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth(self, api_key: str) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(login=api_key, password="")

    async def _request(
        self,
        method: str,
        site: str,
        api_key: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        url = f"{_base_url(site)}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                auth=self._auth(api_key),
                headers={"Content-Type": "application/json"},
                **kwargs,
            ) as response:
                if response.status in (200, 201):
                    return await response.json(content_type=None)
                if response.status == 204:
                    return {}

                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                err_msg = body.get("message", str(body)) if body else f"HTTP {response.status}"
                api_code = body.get("api_error_code", "") if body else ""

                if response.status in (401, 403):
                    raise ChargebeeAuthError(
                        f"Authentication failed: {err_msg}",
                        status_code=response.status,
                        code=api_code or "auth_error",
                    )
                if response.status == 404:
                    raise ChargebeeNotFoundError("resource", path)
                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise ChargebeeRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )
                if response.status >= 500:
                    raise ChargebeeNetworkError(
                        f"Chargebee server error {response.status}: {err_msg}",
                        status_code=response.status,
                    )
                raise ChargebeeError(
                    f"Chargebee error {response.status}: {err_msg}",
                    status_code=response.status,
                    code=api_code,
                )
        except (ChargebeeError,):
            raise
        except aiohttp.ClientConnectorError as exc:
            raise ChargebeeNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise ChargebeeNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise ChargebeeNetworkError(f"Network error: {exc}") from exc

    # ── Subscriptions ─────────────────────────────────────────────────────────

    async def list_subscriptions(
        self,
        site: str,
        api_key: str,
        limit: int = 100,
        offset: str | None = None,
    ) -> dict[str, Any]:
        """GET /subscriptions — list subscriptions with offset-based pagination.

        Returns Chargebee's raw response: ``{"list": [...], "next_offset": "..."}``.
        """
        params: dict[str, Any] = {"limit": limit}
        if offset is not None:
            params["offset"] = offset
        return await self._request("GET", site, api_key, "/subscriptions", params=params)

    async def get_subscription(
        self, site: str, api_key: str, subscription_id: str
    ) -> dict[str, Any]:
        """GET /subscriptions/{id} — get a single subscription."""
        return await self._request(
            "GET", site, api_key, f"/subscriptions/{subscription_id}"
        )

    # ── Customers ─────────────────────────────────────────────────────────────

    async def list_customers(
        self,
        site: str,
        api_key: str,
        limit: int = 100,
        offset: str | None = None,
    ) -> dict[str, Any]:
        """GET /customers — list customers with offset-based pagination.

        Returns Chargebee's raw response: ``{"list": [...], "next_offset": "..."}``.
        """
        params: dict[str, Any] = {"limit": limit}
        if offset is not None:
            params["offset"] = offset
        return await self._request("GET", site, api_key, "/customers", params=params)

    async def get_customer(
        self, site: str, api_key: str, customer_id: str
    ) -> dict[str, Any]:
        """GET /customers/{id} — get a single customer."""
        return await self._request(
            "GET", site, api_key, f"/customers/{customer_id}"
        )

    # ── Invoices ──────────────────────────────────────────────────────────────

    async def list_invoices(
        self,
        site: str,
        api_key: str,
        limit: int = 100,
        offset: str | None = None,
    ) -> dict[str, Any]:
        """GET /invoices — list invoices with offset-based pagination.

        Returns Chargebee's raw response: ``{"list": [...], "next_offset": "..."}``.
        """
        params: dict[str, Any] = {"limit": limit}
        if offset is not None:
            params["offset"] = offset
        return await self._request("GET", site, api_key, "/invoices", params=params)

    async def get_invoice(
        self, site: str, api_key: str, invoice_id: str
    ) -> dict[str, Any]:
        """GET /invoices/{id} — get a single invoice."""
        return await self._request(
            "GET", site, api_key, f"/invoices/{invoice_id}"
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> ChargebeeHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
