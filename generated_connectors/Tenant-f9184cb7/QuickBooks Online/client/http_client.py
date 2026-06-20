from __future__ import annotations

import urllib.parse
from typing import Any

import httpx

from exceptions import (
    QuickBooksAuthError,
    QuickBooksNetworkError,
    QuickBooksNotFoundError,
    QuickBooksRateLimitError,
    QuickBooksServerError,
    QuickBooksError,
)

QBO_BASE_URL = "https://quickbooks.api.intuit.com/v3/company"
INTUIT_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
INTUIT_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
DEFAULT_TIMEOUT_S = 30.0
QBO_SCOPE = "com.intuit.quickbooks.accounting"


class QuickBooksHTTPClient:
    """Low-level async HTTP client for the QuickBooks Online v3 REST API."""

    def __init__(
        self,
        access_token: str,
        realm_id: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._realm_id = realm_id
        self._base = f"{QBO_BASE_URL}/{realm_id}"
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise QuickBooksNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise QuickBooksNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201):
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        # QBO wraps errors under Fault.Error[] or a top-level message
        fault = body.get("Fault", {})
        errors = fault.get("Error", [])
        err_msg = (
            errors[0].get("Message", response.text) if errors else response.text or "Unknown QBO error"
        )
        err_code = errors[0].get("code", "") if errors else ""

        if response.status_code == 401:
            raise QuickBooksAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if response.status_code == 403:
            raise QuickBooksAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status_code == 404:
            raise QuickBooksNotFoundError("resource", path)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise QuickBooksRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise QuickBooksServerError(
                f"QBO server error {response.status_code}: {err_msg}",
                response.status_code,
            )

        raise QuickBooksError(
            f"QBO error {response.status_code}: {err_msg}",
            response.status_code,
            err_code,
        )

    # ── Company info (health check) ──────────────────────────────────────────

    async def get_companyinfo(self) -> dict[str, Any]:
        """GET /companyinfo/{realm_id} — minimal health probe."""
        return await self._request("GET", f"/companyinfo/{self._realm_id}")

    # ── Query (SQL-like) ─────────────────────────────────────────────────────

    async def query(self, query_str: str) -> dict[str, Any]:
        """Run a QBO SQL-like query via GET /query?query=<sql>."""
        params = {"query": query_str, "minorversion": "65"}
        return await self._request("GET", "/query", params=params)

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(self, max_results: int = 100) -> dict[str, Any]:
        return await self.query(
            f"SELECT * FROM Customer MAXRESULTS {max_results}"
        )

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/customer/{customer_id}")

    # ── Invoices ─────────────────────────────────────────────────────────────

    async def list_invoices(self, max_results: int = 100) -> dict[str, Any]:
        return await self.query(
            f"SELECT * FROM Invoice MAXRESULTS {max_results}"
        )

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/invoice/{invoice_id}")

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def list_accounts(self, max_results: int = 100) -> dict[str, Any]:
        return await self.query(
            f"SELECT * FROM Account MAXRESULTS {max_results}"
        )

    # ── Items (products / services) ───────────────────────────────────────────

    async def list_items(self, max_results: int = 100) -> dict[str, Any]:
        return await self.query(
            f"SELECT * FROM Item MAXRESULTS {max_results}"
        )

    # ── Paginated fetch helpers ───────────────────────────────────────────────

    async def get_customers(
        self, start: int = 1, max: int = 1000
    ) -> dict[str, Any]:
        """SELECT * FROM Customer STARTPOSITION {start} MAXRESULTS {max}."""
        return await self.query(
            f"SELECT * FROM Customer STARTPOSITION {start} MAXRESULTS {max}"
        )

    async def get_invoices(
        self, start: int = 1, max: int = 1000
    ) -> dict[str, Any]:
        """SELECT * FROM Invoice STARTPOSITION {start} MAXRESULTS {max}."""
        return await self.query(
            f"SELECT * FROM Invoice STARTPOSITION {start} MAXRESULTS {max}"
        )

    async def get_items(
        self, start: int = 1, max: int = 1000
    ) -> dict[str, Any]:
        """SELECT * FROM Item STARTPOSITION {start} MAXRESULTS {max}."""
        return await self.query(
            f"SELECT * FROM Item STARTPOSITION {start} MAXRESULTS {max}"
        )

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str,
        client_id: str = "",
        client_secret: str = "",
    ) -> dict[str, Any]:
        """Exchange an authorization code for tokens (convenience method)."""
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        return await self.post_form_data(
            INTUIT_TOKEN_URL,
            data=data,
            basic_auth=(client_id, client_secret) if client_id else None,
        )

    async def refresh_token(
        self,
        refresh_token_value: str,
        client_id: str = "",
        client_secret: str = "",
    ) -> dict[str, Any]:
        """Refresh the access token using a refresh token (convenience method)."""
        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_value,
        }
        return await self.post_form_data(
            INTUIT_TOKEN_URL,
            data=data,
            basic_auth=(client_id, client_secret) if client_id else None,
        )

    # ── OAuth token exchange (form-encoded POST) ─────────────────────────────

    async def post_form_data(
        self,
        url: str,
        data: dict[str, str],
        basic_auth: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST application/x-www-form-urlencoded — used for token exchange/refresh."""
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            kwargs: dict[str, Any] = {
                "data": data,
                "headers": {"Accept": "application/json"},
            }
            if basic_auth:
                kwargs["auth"] = basic_auth
            try:
                response = await client.post(url, **kwargs)
            except httpx.TimeoutException as exc:
                raise QuickBooksNetworkError(f"Token request timed out: {exc}") from exc
            except httpx.NetworkError as exc:
                raise QuickBooksNetworkError(f"Token network error: {exc}") from exc

        if response.status_code == 200:
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass
        err_msg = body.get("error_description", body.get("error", response.text))
        if response.status_code in (400, 401):
            raise QuickBooksAuthError(
                f"Token exchange failed: {err_msg}", response.status_code
            )
        raise QuickBooksError(
            f"Token request error {response.status_code}: {err_msg}",
            response.status_code,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> QuickBooksHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
