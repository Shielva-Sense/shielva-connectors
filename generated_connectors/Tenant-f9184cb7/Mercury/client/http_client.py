"""All Mercury REST API HTTP calls — zero business logic, zero normalization.

httpx async client. Mercury REST API expects:
  Authorization: Bearer <api_token>
  Accept:        application/json
  Content-Type:  application/json
  Idempotency-Key: <uuid>          (only on money-movement POSTs)

Retry on 429/5xx with exponential backoff + jitter.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    MercuryAuthError,
    MercuryError,
    MercuryNetworkError,
    MercuryNotFound,
    MercuryRateLimitError,
    MercuryServerError,
)

logger = structlog.get_logger(__name__)

_MERCURY_BASE = "https://api.mercury.com/api/v1"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class MercuryHTTPClient:
    """Thin async HTTP client for the Mercury REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        api_token: str = "",
        base_url: str = _MERCURY_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._api_token = api_token or ""
        self._base_url = (base_url or _MERCURY_BASE).rstrip("/")
        self._timeout = timeout
        self._max_retries = max(0, max_retries)

    # ── Internal ────────────────────────────────────────────────────────────

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self._api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _raise_for_status(response: httpx.Response, context: str = "") -> None:
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
                or body.get("detail")
                or body.get("details")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        body_dict = body if isinstance(body, dict) else {"raw": body}
        ctx = f": {context}" if context else ""

        if status == 401 or status == 403:
            raise MercuryAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise MercuryNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 429:
            raise MercuryRateLimitError(
                f"429 Rate Limited{ctx}: {message}",
            )
        if status >= 500:
            raise MercuryServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise MercuryError(
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
        extra_headers: Optional[Dict[str, str]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers(extra_headers)

        last_exc: Optional[BaseException] = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
                if response.status_code in _RETRY_STATUSES and attempt < self._max_retries:
                    delay = _backoff(attempt)
                    logger.warning(
                        "mercury.http.retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                self._raise_for_status(response, context=context or f"{method} {path}")
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    delay = _backoff(attempt)
                    logger.warning(
                        "mercury.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise MercuryNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}"
                ) from exc

        if last_exc:
            raise MercuryNetworkError(str(last_exc)) from last_exc
        raise MercuryNetworkError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── Accounts ────────────────────────────────────────────────────────────

    async def list_accounts(self) -> Dict[str, Any]:
        """GET /accounts — every Mercury account on the organization."""
        return await self._request("GET", "/accounts", context="list_accounts")

    async def get_account(self, account_id: str) -> Dict[str, Any]:
        """GET /account/{id}."""
        return await self._request(
            "GET",
            f"/account/{account_id}",
            context=f"get_account({account_id})",
        )

    # ── Transactions ────────────────────────────────────────────────────────

    async def list_account_transactions(
        self,
        account_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        order: str = "desc",
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /account/{id}/transactions."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset, "order": order}
        if status:
            params["status"] = status
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if search:
            params["search"] = search
        return await self._request(
            "GET",
            f"/account/{account_id}/transactions",
            params=params,
            context=f"list_account_transactions({account_id})",
        )

    async def get_transaction(
        self, account_id: str, transaction_id: str
    ) -> Dict[str, Any]:
        """GET /account/{aid}/transaction/{tid}."""
        return await self._request(
            "GET",
            f"/account/{account_id}/transaction/{transaction_id}",
            context=f"get_transaction({account_id},{transaction_id})",
        )

    # ── Recipients ──────────────────────────────────────────────────────────

    async def list_recipients(self) -> Dict[str, Any]:
        """GET /recipients."""
        return await self._request("GET", "/recipients", context="list_recipients")

    async def get_recipient(self, recipient_id: str) -> Dict[str, Any]:
        """GET /recipient/{id}."""
        return await self._request(
            "GET",
            f"/recipient/{recipient_id}",
            context=f"get_recipient({recipient_id})",
        )

    async def create_recipient(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /recipient."""
        return await self._request(
            "POST",
            "/recipient",
            json_body=body,
            context="create_recipient",
        )

    # ── Money movement ──────────────────────────────────────────────────────

    async def send_payment(
        self,
        account_id: str,
        body: Dict[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """POST /account/{id}/transactions — money movement.

        Mercury requires an Idempotency-Key header on every money-movement
        call. The connector layer enforces non-empty key before calling here.
        """
        if not idempotency_key:
            raise MercuryError("idempotency_key is required for money-movement endpoints")
        return await self._request(
            "POST",
            f"/account/{account_id}/transactions",
            json_body=body,
            extra_headers={"Idempotency-Key": idempotency_key},
            context=f"send_payment({account_id})",
        )

    # ── Statements ──────────────────────────────────────────────────────────

    async def list_statements(
        self, account_id: str, start: str, end: str
    ) -> Dict[str, Any]:
        """GET /account/{id}/statements?start=&end=."""
        return await self._request(
            "GET",
            f"/account/{account_id}/statements",
            params={"start": start, "end": end},
            context=f"list_statements({account_id})",
        )


def _backoff(attempt: int) -> float:
    """Exponential backoff with ±25% jitter, capped at 10s."""
    base = _BACKOFF_BASE * (2 ** attempt)
    jitter = random.uniform(0.75, 1.25)
    return min(base * jitter, 10.0)
