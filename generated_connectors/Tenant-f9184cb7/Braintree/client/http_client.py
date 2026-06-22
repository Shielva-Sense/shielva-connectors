from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import aiohttp

# Allow sibling-level imports when running tests directly
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from exceptions import (
    BraintreeAuthError,
    BraintreeError,
    BraintreeNetworkError,
    BraintreeNotFoundError,
    BraintreeRateLimitError,
)

SANDBOX_BASE = "https://api.sandbox.braintreegateway.com/merchants/{merchant_id}"
PRODUCTION_BASE = "https://api.braintreegateway.com/merchants/{merchant_id}"
DEFAULT_TIMEOUT_S = 30.0
API_VERSION = "6"


class BraintreeHTTPClient:
    """Low-level async HTTP client for the Braintree REST API.

    Uses HTTP Basic Auth (public_key : private_key) and the
    ``X-ApiVersion`` header on every request.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._merchant_id: str = cfg.get("merchant_id", "")
        self._public_key: str = cfg.get("public_key", "")
        self._private_key: str = cfg.get("private_key", "")
        environment: str = cfg.get("environment", "sandbox").lower()
        base_template = SANDBOX_BASE if environment == "sandbox" else PRODUCTION_BASE
        self._base_url: str = base_template.format(merchant_id=self._merchant_id)
        self._session: aiohttp.ClientSession | None = None

    # ── Session lifecycle ────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            auth = aiohttp.BasicAuth(self._public_key, self._private_key)
            self._session = aiohttp.ClientSession(
                auth=auth,
                headers={
                    "X-ApiVersion": API_VERSION,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_S),
            )
        return self._session

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> BraintreeHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    # ── Core request ─────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        url = f"{self._base_url}/{path.lstrip('/')}"
        session = self._get_session()
        try:
            async with session.request(method, url, **kwargs) as resp:
                status = resp.status
                try:
                    body: dict[str, Any] = await resp.json(content_type=None)
                except Exception:
                    body = {}
                self._raise_for_status(status, body)
                return body
        except (BraintreeError,):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise BraintreeNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise BraintreeNetworkError(f"Network error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise BraintreeNetworkError(f"HTTP client error: {exc}") from exc

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map Braintree HTTP status codes to typed exceptions."""
        if status in (200, 201, 202):
            return
        msg = (
            body.get("message")
            or body.get("error", {}).get("message", "")
            or f"Braintree API error (HTTP {status})"
        )
        if status == 401:
            raise BraintreeAuthError(f"Authentication failed: {msg}", status_code=401)
        if status == 403:
            raise BraintreeAuthError(f"Forbidden: {msg}", status_code=403)
        if status == 404:
            raise BraintreeNotFoundError("resource", msg)
        if status == 422:
            raise BraintreeError(f"Unprocessable entity: {msg}", status_code=422)
        if status == 429:
            raise BraintreeRateLimitError(f"Rate limited: {msg}")
        if status >= 500:
            raise BraintreeNetworkError(f"Server error {status}: {msg}", status_code=status)
        raise BraintreeError(f"Braintree error {status}: {msg}", status_code=status)

    # ── Merchant ─────────────────────────────────────────────────────────────

    async def get_merchant(self) -> dict[str, Any]:
        """GET /merchants/{merchant_id} — verify credentials and fetch merchant info."""
        return await self._request("GET", "")

    # ── Transactions ─────────────────────────────────────────────────────────

    async def search_transactions(self, page: int = 1) -> dict[str, Any]:
        """POST /transactions/advanced_search — paginated transaction search."""
        payload = {"search": {}, "page": page}
        return await self._request("POST", "transactions/advanced_search", json=payload)

    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        """GET /transactions/{id}."""
        return await self._request("GET", f"transactions/{transaction_id}")

    # ── Customers ────────────────────────────────────────────────────────────

    async def get_customers(self, page: int = 1) -> dict[str, Any]:
        """POST /customers/advanced_search — paginated customer search."""
        payload = {"search": {}, "page": page}
        return await self._request("POST", "customers/advanced_search", json=payload)

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        """GET /customers/{id}."""
        return await self._request("GET", f"customers/{customer_id}")

    # ── Subscriptions ────────────────────────────────────────────────────────

    async def get_subscriptions(self, page: int = 1) -> dict[str, Any]:
        """POST /subscriptions/advanced_search — paginated subscription search."""
        payload = {"search": {}, "page": page}
        return await self._request("POST", "subscriptions/advanced_search", json=payload)

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        """GET /subscriptions/{id}."""
        return await self._request("GET", f"subscriptions/{subscription_id}")

    # ── Plans ────────────────────────────────────────────────────────────────

    async def get_plans(self) -> dict[str, Any]:
        """GET /plans — fetch all billing plans."""
        return await self._request("GET", "plans")

    # ── Disputes ─────────────────────────────────────────────────────────────

    async def search_disputes(self, page: int = 1) -> dict[str, Any]:
        """POST /disputes/advanced_search — paginated dispute search."""
        payload = {"search": {}, "page": page}
        return await self._request("POST", "disputes/advanced_search", json=payload)
