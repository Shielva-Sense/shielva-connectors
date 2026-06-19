from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    StripeAuthError,
    StripeInvalidKeyError,
    StripeNetworkError,
    StripeNotFoundError,
    StripeRateLimitError,
    StripeServerError,
)

STRIPE_BASE_URL = "https://api.stripe.com/v1"
DEFAULT_TIMEOUT_S = 30.0


class StripeHTTPClient:
    """Low-level async HTTP client for the Stripe REST API."""

    def __init__(self, api_key: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=STRIPE_BASE_URL,
            auth=(api_key, ""),
            timeout=timeout,
            headers={"Stripe-Version": "2024-06-20"},
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise StripeNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise StripeNetworkError(f"Network error: {exc}") from exc

        if response.status_code == 200:
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass
        error = body.get("error", {})
        err_msg = error.get("message", response.text or "Unknown Stripe error")
        err_code = error.get("code", "")
        err_type = error.get("type", "")

        if response.status_code == 401:
            raise StripeAuthError(f"Authentication failed: {err_msg}", 401, err_code)
        if response.status_code == 403:
            raise StripeAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status_code == 404:
            raise StripeNotFoundError(err_type or "resource", error.get("param", "unknown"))
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise StripeRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise StripeServerError(f"Stripe server error {response.status_code}: {err_msg}", response.status_code)

        from exceptions import StripeError
        raise StripeError(f"Stripe error {response.status_code}: {err_msg}", response.status_code, err_code)

    async def get_account(self) -> dict[str, Any]:
        return await self._request("GET", "/account")

    # ── Balance ──────────────────────────────────────────────────────────────

    async def get_balance(self, api_key: str | None = None) -> dict[str, Any]:  # noqa: ARG002
        return await self._request("GET", "/balance")

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(self, limit: int = 100, starting_after: str | None = None, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, **kwargs}
        if starting_after:
            params["starting_after"] = starting_after
        return await self._request("GET", "/customers", params=params)

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/customers/{customer_id}")

    async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
        return await self._request("POST", "/customers", data=kwargs)

    async def update_customer(self, customer_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self._request("POST", f"/customers/{customer_id}", data=kwargs)

    async def delete_customer(self, customer_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/customers/{customer_id}")

    # ── Charges ──────────────────────────────────────────────────────────────

    async def list_charges(self, limit: int = 100, starting_after: str | None = None, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, **kwargs}
        if starting_after:
            params["starting_after"] = starting_after
        return await self._request("GET", "/charges", params=params)

    async def get_charge(self, charge_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/charges/{charge_id}")

    # ── Payment Intents ───────────────────────────────────────────────────────

    async def list_payment_intents(self, limit: int = 100, starting_after: str | None = None, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, **kwargs}
        if starting_after:
            params["starting_after"] = starting_after
        return await self._request("GET", "/payment_intents", params=params)

    async def get_payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/payment_intents/{payment_intent_id}")

    async def create_payment_intent(self, amount: int, currency: str, **kwargs: Any) -> dict[str, Any]:
        return await self._request("POST", "/payment_intents", data={"amount": amount, "currency": currency, **kwargs})

    # ── Subscriptions ────────────────────────────────────────────────────────

    async def list_subscriptions(self, limit: int = 100, starting_after: str | None = None, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, **kwargs}
        if starting_after:
            params["starting_after"] = starting_after
        return await self._request("GET", "/subscriptions", params=params)

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/subscriptions/{subscription_id}")

    async def cancel_subscription(self, subscription_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/subscriptions/{subscription_id}")

    # ── Products ─────────────────────────────────────────────────────────────

    async def list_products(self, limit: int = 100, starting_after: str | None = None, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, **kwargs}
        if starting_after:
            params["starting_after"] = starting_after
        return await self._request("GET", "/products", params=params)

    # ── Invoices ─────────────────────────────────────────────────────────────

    async def list_invoices(self, limit: int = 100, starting_after: str | None = None, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, **kwargs}
        if starting_after:
            params["starting_after"] = starting_after
        return await self._request("GET", "/invoices", params=params)

    # ── Refunds ──────────────────────────────────────────────────────────────

    async def create_refund(self, charge: str, amount: int | None = None, **kwargs: Any) -> dict[str, Any]:
        data: dict[str, Any] = {"charge": charge, **kwargs}
        if amount is not None:
            data["amount"] = amount
        return await self._request("POST", "/refunds", data=data)

    # ── Events ───────────────────────────────────────────────────────────────

    async def list_events(self, limit: int = 100, starting_after: str | None = None, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, **kwargs}
        if starting_after:
            params["starting_after"] = starting_after
        return await self._request("GET", "/events", params=params)

    async def get_event(self, event_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/events/{event_id}")

    # ── Webhooks ─────────────────────────────────────────────────────────────

    async def list_webhooks(self) -> dict[str, Any]:
        return await self._request("GET", "/webhook_endpoints")

    async def create_webhook(self, url: str, enabled_events: list[str], **kwargs: Any) -> dict[str, Any]:
        return await self._request("POST", "/webhook_endpoints", data={"url": url, "enabled_events[]": enabled_events, **kwargs})

    async def delete_webhook(self, webhook_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/webhook_endpoints/{webhook_id}")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> StripeHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
