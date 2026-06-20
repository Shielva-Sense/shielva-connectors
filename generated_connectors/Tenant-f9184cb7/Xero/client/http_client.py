from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    XeroAuthError,
    XeroError,
    XeroNetworkError,
    XeroNotFoundError,
    XeroRateLimitError,
)

XERO_BASE_URL = "https://api.xero.com/api.xro/2.0"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"
DEFAULT_TIMEOUT_S = 30.0


class XeroHTTPClient:
    """Low-level async HTTP client for the Xero Accounting API v2."""

    def __init__(
        self,
        access_token: str,
        xero_tenant_id: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._xero_tenant_id = xero_tenant_id
        self._client = httpx.AsyncClient(
            base_url=XERO_BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Xero-Tenant-Id": xero_tenant_id,
                "Accept": "application/json",
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise XeroNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise XeroNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201):
            try:
                return response.json()
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        err_msg = (
            body.get("Detail")
            or body.get("Message")
            or body.get("message")
            or response.text
            or "Unknown Xero error"
        )

        if response.status_code == 401:
            raise XeroAuthError(f"Authentication failed: {err_msg}", 401, "unauthorized")
        if response.status_code == 403:
            raise XeroAuthError(f"Forbidden: {err_msg}", 403, "forbidden")
        if response.status_code == 404:
            raise XeroNotFoundError("resource", path.lstrip("/"))
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "60"))
            raise XeroRateLimitError(f"Rate limited: {err_msg}", retry_after)

        raise XeroError(
            f"Xero error {response.status_code}: {err_msg}",
            response.status_code,
        )

    # ── Organisation ─────────────────────────────────────────────────────────

    async def get_organisation(self) -> dict[str, Any]:
        """GET /Organisation — used for health check."""
        return await self._request("GET", "/Organisation")

    # ── Invoices ─────────────────────────────────────────────────────────────

    async def list_invoices(
        self,
        modified_after: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page}
        headers: dict[str, str] = {}
        if modified_after:
            headers["If-Modified-Since"] = modified_after
        return await self._request("GET", "/Invoices", params=params, headers=headers)

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/Invoices/{invoice_id}")

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        modified_after: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page}
        headers: dict[str, str] = {}
        if modified_after:
            headers["If-Modified-Since"] = modified_after
        return await self._request("GET", "/Contacts", params=params, headers=headers)

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/Contacts/{contact_id}")

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def list_accounts(self) -> dict[str, Any]:
        return await self._request("GET", "/Accounts")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> XeroHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


async def get_xero_connections(access_token: str) -> list[dict[str, Any]]:
    """Fetch tenant connections from POST /connections (OAuth step after token exchange)."""
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
        try:
            response = await client.get(
                XERO_CONNECTIONS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
            if response.status_code == 200:
                return response.json()
            body: dict[str, Any] = {}
            try:
                body = response.json()
            except Exception:
                pass
            err_msg = body.get("Detail") or response.text or "Unknown error"
            if response.status_code == 401:
                raise XeroAuthError(f"Authentication failed: {err_msg}", 401)
            raise XeroError(f"Xero error {response.status_code}: {err_msg}", response.status_code)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise XeroNetworkError(f"Network error fetching connections: {exc}") from exc
