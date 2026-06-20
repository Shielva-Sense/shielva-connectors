from __future__ import annotations

import time
from typing import Any

import httpx

from exceptions import (
    PayPalAuthError,
    PayPalInvalidCredentialsError,
    PayPalNetworkError,
    PayPalNotFoundError,
    PayPalRateLimitError,
    PayPalServerError,
    PayPalTokenError,
)

PAYPAL_LIVE_BASE = "https://api-m.paypal.com"
PAYPAL_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
DEFAULT_TIMEOUT_S = 30.0


class PayPalHTTPClient:
    """
    Low-level async HTTP client for the PayPal REST API v2.

    Manages OAuth2 client_credentials token acquisition and automatic
    refresh before each API call when the token is expired or missing.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        sandbox: bool = False,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._sandbox = sandbox
        self._base_url = PAYPAL_SANDBOX_BASE if sandbox else PAYPAL_LIVE_BASE
        self._access_token: str = ""
        self._token_expires_at: float = 0.0
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    # ── Token management ─────────────────────────────────────────────────────

    async def _acquire_token(self) -> None:
        """POST /v1/oauth2/token with BasicAuth to get a client_credentials token."""
        try:
            response = await self._client.post(
                "/v1/oauth2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as exc:
            raise PayPalNetworkError(f"Token request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise PayPalNetworkError(f"Network error during token acquisition: {exc}") from exc

        if response.status_code == 401:
            raise PayPalInvalidCredentialsError(
                "Invalid client_id or client_secret", 401, "invalid_client"
            )
        if response.status_code == 400:
            body: dict[str, Any] = {}
            try:
                body = response.json()
            except Exception:
                pass
            raise PayPalTokenError(
                f"Token request failed: {body.get('error_description', response.text)}",
                400,
                body.get("error", "token_error"),
            )
        if response.status_code != 200:
            raise PayPalTokenError(
                f"Token acquisition failed with status {response.status_code}: {response.text}",
                response.status_code,
            )

        data: dict[str, Any] = response.json()
        self._access_token = data.get("access_token", "")
        expires_in: int = int(data.get("expires_in", 3600))
        # Refresh 60 seconds early to avoid edge-case expiry mid-request
        self._token_expires_at = time.monotonic() + expires_in - 60

    def _is_token_expired(self) -> bool:
        return not self._access_token or time.monotonic() >= self._token_expires_at

    async def _ensure_token(self) -> None:
        if self._is_token_expired():
            await self._acquire_token()

    # ── Core request dispatcher ───────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        await self._ensure_token()
        headers: dict[str, str] = {"Authorization": f"Bearer {self._access_token}"}
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise PayPalNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise PayPalNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        # PayPal wraps errors: {"name": "...", "message": "...", "details": [...]}
        err_name = body.get("name", "")
        err_msg = body.get("message", response.text or "Unknown PayPal error")
        err_code = body.get("error", err_name)

        if response.status_code == 401:
            raise PayPalAuthError(f"Authentication failed: {err_msg}", 401, err_code)
        if response.status_code == 403:
            raise PayPalAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status_code == 404:
            raise PayPalNotFoundError(err_name or "resource", path)
        if response.status_code == 422:
            raise PayPalAuthError(f"Unprocessable entity: {err_msg}", 422, err_code)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise PayPalRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise PayPalServerError(
                f"PayPal server error {response.status_code}: {err_msg}", response.status_code
            )

        from exceptions import PayPalError
        raise PayPalError(
            f"PayPal error {response.status_code}: {err_msg}", response.status_code, err_code
        )

    # ── Auth probe ───────────────────────────────────────────────────────────

    async def get_token(self) -> dict[str, Any]:
        """Acquire a new OAuth2 token; returns the raw token response dict."""
        try:
            response = await self._client.post(
                "/v1/oauth2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as exc:
            raise PayPalNetworkError(f"Token request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise PayPalNetworkError(f"Network error: {exc}") from exc

        if response.status_code == 401:
            raise PayPalInvalidCredentialsError(
                "Invalid client_id or client_secret", 401, "invalid_client"
            )
        if response.status_code != 200:
            body: dict[str, Any] = {}
            try:
                body = response.json()
            except Exception:
                pass
            raise PayPalTokenError(
                f"Token acquisition failed: {body.get('error_description', response.text)}",
                response.status_code,
            )

        data: dict[str, Any] = response.json()
        self._access_token = data.get("access_token", "")
        expires_in: int = int(data.get("expires_in", 3600))
        self._token_expires_at = time.monotonic() + expires_in - 60
        return data

    # ── Reporting: Transactions ───────────────────────────────────────────────

    async def list_transactions(
        self,
        start_date: str,
        end_date: str,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """GET /v1/reporting/transactions"""
        params: dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
            "page": page,
            "page_size": page_size,
        }
        return await self._request("GET", "/v1/reporting/transactions", params=params)

    # ── Reporting: Balances ───────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """GET /v1/reporting/balances"""
        return await self._request("GET", "/v1/reporting/balances")

    # ── Orders v2 ────────────────────────────────────────────────────────────

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """GET /v2/checkout/orders/{order_id}"""
        return await self._request("GET", f"/v2/checkout/orders/{order_id}")

    # ── Payments v2 ──────────────────────────────────────────────────────────

    async def list_payments(
        self,
        page_size: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        """GET /v2/payments/captures (page-based)"""
        # PayPal v1 payment list endpoint
        params: dict[str, Any] = {
            "count": page_size,
            "start_index": (page - 1) * page_size,
        }
        return await self._request("GET", "/v1/payments/payment", params=params)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PayPalHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
