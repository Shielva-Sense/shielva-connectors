"""All HiBob API HTTP calls — zero business logic, zero normalization.

Authentication: HTTP Basic with ``{service_user_id}:{service_user_token}`` —
the Service-User credentials issued by HiBob (Bob) at
**Settings -> Integrations -> Service Users**.

Required headers on every request to ``https://api.hibob.com/v1/*``:

    Authorization: Basic base64(service_user_id:service_user_token)
    Content-Type:  application/json
    Accept:        application/json

Retry on 429 / 5xx with capped exponential backoff (honours ``Retry-After``).
"""
from __future__ import annotations

import asyncio
import base64
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    HiBobAuthError,
    HiBobBadRequestError,
    HiBobConflictError,
    HiBobError,
    HiBobNetworkError,
    HiBobNotFound,
    HiBobNotFoundError,
    HiBobRateLimitError,
    HiBobServerError,
)

logger = structlog.get_logger(__name__)

_HIBOB_BASE = "https://api.hibob.com/v1"
_DEFAULT_TIMEOUT_S = 30.0
_MAX_RETRIES = 3
_RETRY_BASE_DELAY_S: float = 0.5
_RETRY_MAX_DELAY_S: float = 16.0
_RETRY_BACKOFF_FACTOR: float = 2.0


def _build_basic_auth_header(service_user_id: str, service_user_token: str) -> str:
    """Return the value for the ``Authorization: Basic ...`` header."""
    raw = f"{service_user_id}:{service_user_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class HiBobHTTPClient:
    """Thin async HTTP client for the HiBob REST API.

    All methods are awaitable and return raw response dicts (or ``None`` for
    empty bodies). Auth + retry are owned here — the connector layer only
    orchestrates business calls.
    """

    def __init__(
        self,
        service_user_id: str = "",
        service_user_token: str = "",
        base_url: str = _HIBOB_BASE,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._service_user_id = service_user_id or ""
        self._service_user_token = service_user_token or ""
        self._base_url = (base_url or _HIBOB_BASE).rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max(0, int(max_retries))

    # ── credential injection (allow setting after construction) ──────────────

    def set_credentials(self, service_user_id: str, service_user_token: str) -> None:
        self._service_user_id = service_user_id or ""
        self._service_user_token = service_user_token or ""

    # ── header builders ──────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._service_user_id and self._service_user_token:
            headers["Authorization"] = _build_basic_auth_header(
                self._service_user_id, self._service_user_token
            )
        return headers

    # ── error mapping ────────────────────────────────────────────────────────

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
                body.get("error")
                or body.get("message")
                or body.get("detail")
                or response.text
                or ""
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body) or response.text or ""

        ctx = f": {context}" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status == 400:
            raise HiBobBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 401:
            raise HiBobAuthError(
                f"401 Unauthorized{ctx}: {message}",
                status_code=401,
                response_body=body_dict,
            )
        if status == 403:
            raise HiBobAuthError(
                f"403 Forbidden{ctx}: {message}",
                status_code=403,
                response_body=body_dict,
            )
        if status == 404:
            raise HiBobNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise HiBobConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after) if retry_after else 5.0
            except (TypeError, ValueError):
                retry_after_s = 5.0
            raise HiBobRateLimitError(
                f"429 Too Many Requests{ctx}: {message}",
                retry_after_s=retry_after_s,
            )
        if status >= 500:
            raise HiBobServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise HiBobError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── request driver with 429 / 5xx retry ──────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        url = (
            path
            if path.startswith("http")
            else f"{self._base_url}{path if path.startswith('/') else '/' + path}"
        )
        headers = self._headers()
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )

                # Retry on 429 / 5xx — bounded.
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self._max_retries:
                        retry_after = response.headers.get("Retry-After")
                        delay = self._compute_delay(attempt, retry_after)
                        logger.warning(
                            "hibob.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                    self._raise_for_status(response, context)

                self._raise_for_status(response, context)

                if response.status_code == 204 or not response.content:
                    return None
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}

            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    delay = self._compute_delay(attempt, None)
                    logger.warning(
                        "hibob.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise HiBobNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise HiBobNetworkError(str(last_exc)) from last_exc
        raise HiBobError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    @staticmethod
    def _compute_delay(attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return min(float(retry_after), _RETRY_MAX_DELAY_S)
            except (TypeError, ValueError):
                pass
        delay = _RETRY_BASE_DELAY_S * (_RETRY_BACKOFF_FACTOR ** attempt)
        return min(delay + random.uniform(0, 0.25), _RETRY_MAX_DELAY_S)

    # ── People (employees) ───────────────────────────────────────────────────

    async def health_check(self) -> Any:
        """GET /people?fields=id&limit=1 — verify credentials cheaply."""
        return await self._request(
            "GET",
            "/people",
            params={"fields": "id", "limit": 1},
            context="health_check",
        )

    async def list_people(
        self,
        *,
        limit: int = 50,
        fields: Optional[List[str]] = None,
    ) -> Any:
        """GET /people."""
        params: Dict[str, Any] = {"limit": limit}
        if fields:
            params["fields"] = ",".join(fields)
        return await self._request(
            "GET",
            "/people",
            params=params,
            context="list_people",
        )

    async def search_people(self, body: Dict[str, Any]) -> Any:
        """POST /people/search — bulk employee search with field projection."""
        return await self._request(
            "POST",
            "/people/search",
            json_body=body,
            context="search_people",
        )

    async def get_employee(self, employee_id: str) -> Any:
        """GET /people/{id}."""
        return await self._request(
            "GET",
            f"/people/{employee_id}",
            context=f"get_employee({employee_id})",
        )

    async def get_employee_profile(self, employee_id: str) -> Any:
        """GET /profiles/{id} — humanised projection."""
        return await self._request(
            "GET",
            f"/profiles/{employee_id}",
            context=f"get_employee_profile({employee_id})",
        )

    async def create_employee(self, body: Dict[str, Any]) -> Any:
        """POST /people."""
        return await self._request(
            "POST",
            "/people",
            json_body=body,
            context="create_employee",
        )

    async def update_employee(
        self, employee_id: str, fields: Dict[str, Any]
    ) -> Any:
        """PUT /people/{id}."""
        return await self._request(
            "PUT",
            f"/people/{employee_id}",
            json_body=fields,
            context=f"update_employee({employee_id})",
        )

    async def list_employments(self, employee_id: str) -> Any:
        """GET /people/{id}/employment."""
        return await self._request(
            "GET",
            f"/people/{employee_id}/employment",
            context=f"list_employments({employee_id})",
        )

    # ── Time-Off ─────────────────────────────────────────────────────────────

    async def list_time_off_requests(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        policy_type_display_name: Optional[str] = None,
        include_pending: bool = True,
    ) -> Any:
        """GET /timeoff/requests/changes."""
        params: Dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if policy_type_display_name:
            params["policyTypeDisplayName"] = policy_type_display_name
        params["includePending"] = "true" if include_pending else "false"
        return await self._request(
            "GET",
            "/timeoff/requests/changes",
            params=params,
            context="list_time_off_requests",
        )

    async def create_time_off_request(
        self, employee_id: str, body: Dict[str, Any]
    ) -> Any:
        """POST /timeoff/employees/{id}/requests."""
        return await self._request(
            "POST",
            f"/timeoff/employees/{employee_id}/requests",
            json_body=body,
            context=f"create_time_off_request({employee_id})",
        )

    # ── Payroll ──────────────────────────────────────────────────────────────

    async def list_payroll(self, employee_id: str) -> Any:
        """GET /payroll/history/{employee_id}."""
        return await self._request(
            "GET",
            f"/payroll/history/{employee_id}",
            context=f"list_payroll({employee_id})",
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def list_lifecycle_changes(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Any:
        """GET /people/lifecycle/changes."""
        params: Dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return await self._request(
            "GET",
            "/people/lifecycle/changes",
            params=params or None,
            context="list_lifecycle_changes",
        )

    # ── Named lists: departments + sites ─────────────────────────────────────

    async def list_departments(self) -> Any:
        """GET /company/named-lists/department."""
        return await self._request(
            "GET",
            "/company/named-lists/department",
            context="list_departments",
        )

    async def list_sites(self) -> Any:
        """GET /company/named-lists/site."""
        return await self._request(
            "GET",
            "/company/named-lists/site",
            context="list_sites",
        )
