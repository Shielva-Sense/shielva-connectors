"""All Brex API HTTP calls — zero business logic, zero normalization.

httpx async client. The Brex REST API expects:
  Authorization: Bearer <access_token>
  Content-Type:  application/json
  Accept:        application/json

Retry on 429/5xx with exponential backoff.
"""
import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    BrexAuthError,
    BrexError,
    BrexNetworkError,
    BrexNotFound,
)

logger = structlog.get_logger(__name__)

_BREX_BASE = "https://platform.brexapis.com"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class BrexHTTPClient:
    """Thin async HTTP client for the Brex REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        access_token: str = "",
        base_url: str = _BREX_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._access_token = access_token or ""
        self._base_url = (base_url or _BREX_BASE).rstrip("/")
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            message = (
                body.get("message")
                or body.get("error")
                or body.get("error_description")
                or body.get("details")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status == 401 or status == 403:
            raise BrexAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise BrexNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        raise BrexError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers()

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "brex.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                await self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "brex.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise BrexNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise BrexNetworkError(str(last_exc)) from last_exc
        raise BrexNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Users ──────────────────────────────────────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /v2/users/me."""
        return await self._request("GET", "/v2/users/me", context="get_current_user")

    async def list_users(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v2/users."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        return await self._request(
            "GET", "/v2/users", params=params, context="list_users",
        )

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """GET /v2/users/{id}."""
        return await self._request(
            "GET", f"/v2/users/{user_id}", context=f"get_user({user_id})",
        )

    # ── Cards ──────────────────────────────────────────────────────────────

    async def list_cards(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v2/cards."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if user_id:
            params["user_id"] = user_id
        return await self._request(
            "GET", "/v2/cards", params=params, context="list_cards",
        )

    async def get_card(self, card_id: str) -> Dict[str, Any]:
        """GET /v2/cards/{id}."""
        return await self._request(
            "GET", f"/v2/cards/{card_id}", context=f"get_card({card_id})",
        )

    # ── Transactions (card) ────────────────────────────────────────────────

    async def list_transactions(
        self,
        cursor: Optional[str] = None,
        limit: int = 100,
        posted_at_start: Optional[str] = None,
        expand: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /v2/transactions/card/primary."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if posted_at_start:
            params["posted_at_start"] = posted_at_start
        if expand:
            params["expand[]"] = expand
        return await self._request(
            "GET",
            "/v2/transactions/card/primary",
            params=params,
            context="list_transactions",
        )

    async def get_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """GET /v2/transactions/card/primary/{id}."""
        return await self._request(
            "GET",
            f"/v2/transactions/card/primary/{transaction_id}",
            context=f"get_transaction({transaction_id})",
        )

    # ── Expenses ───────────────────────────────────────────────────────────

    async def list_expenses(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
        expense_type: Optional[List[str]] = None,
        status: Optional[List[str]] = None,
        payment_status: Optional[List[str]] = None,
        user_id: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /v1/expenses/card."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if expense_type:
            params["expense_type[]"] = expense_type
        if status:
            params["status[]"] = status
        if payment_status:
            params["payment_status[]"] = payment_status
        if user_id:
            params["user_id[]"] = user_id
        return await self._request(
            "GET", "/v1/expenses/card", params=params, context="list_expenses",
        )

    async def get_expense(self, expense_id: str) -> Dict[str, Any]:
        """GET /v1/expenses/card/{id}."""
        return await self._request(
            "GET",
            f"/v1/expenses/card/{expense_id}",
            context=f"get_expense({expense_id})",
        )

    # ── Departments ────────────────────────────────────────────────────────

    async def list_departments(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v2/departments."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", "/v2/departments", params=params, context="list_departments",
        )

    # ── Locations ──────────────────────────────────────────────────────────

    async def list_locations(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v2/locations."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", "/v2/locations", params=params, context="list_locations",
        )

    # ── Vendors ────────────────────────────────────────────────────────────

    async def list_vendors(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v1/vendors."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", "/v1/vendors", params=params, context="list_vendors",
        )

    # ── Receipts ───────────────────────────────────────────────────────────

    async def list_receipts(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v1/expenses/card/receipt_match."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET",
            "/v1/expenses/card/receipt_match",
            params=params,
            context="list_receipts",
        )

    # ── Budgets ────────────────────────────────────────────────────────────

    async def list_budgets(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v2/budgets."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", "/v2/budgets", params=params, context="list_budgets",
        )

    # ── Spend Limits ───────────────────────────────────────────────────────

    async def list_spend_limits(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v2/spend_limits."""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", "/v2/spend_limits", params=params, context="list_spend_limits",
        )
