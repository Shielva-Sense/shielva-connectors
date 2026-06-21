"""HTTP transport for the Hunter.io v2 REST API.

Hunter authenticates via a `?api_key=<key>` query parameter — NOT a header.

Responsibilities:
- httpx.AsyncClient per request (no shared connection pool, no leaks)
- automatic api_key query injection — caller cannot accidentally clobber
- retry with exponential-backoff + jitter on 429 + 5xx (honours Retry-After)
- typed exception mapping:
    401/403 → HunterAuthError
    404     → HunterNotFoundError
    429     → HunterRateLimitError (after retries exhausted)
    5xx     → HunterServerError (after retries exhausted)
    network → HunterNetworkError

NO business logic, NO normalization — connector.py orchestrates only.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    HunterAuthError,
    HunterError,
    HunterNetworkError,
    HunterNotFoundError,
    HunterRateLimitError,
    HunterServerError,
)

logger = structlog.get_logger(__name__)

_HUNTER_BASE = "https://api.hunter.io/v2"
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_BACKOFF_S = 30.0


class HunterHTTPClient:
    """Thin async HTTP client for Hunter.io v2.

    Every request is auto-stamped with `?api_key=<key>`. The query param is
    appended LAST so it always wins if the caller mistakenly passes one via
    `params`. `None` values are dropped from `params` so httpx does not emit
    `?domain=None`.
    """

    def __init__(
        self,
        base_url: str = _HUNTER_BASE,
        max_retries: int = 3,
        timeout: Optional[httpx.Timeout] = None,
    ) -> None:
        self._base_url = (base_url or _HUNTER_BASE).rstrip("/")
        self._max_retries = max(0, int(max_retries))
        self._timeout = timeout or _DEFAULT_TIMEOUT

    # ── Internal helpers ────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _params(self, api_key: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge caller params with api_key. Drops None values."""
        merged: Dict[str, Any] = {}
        if params:
            for k, v in params.items():
                if v is None:
                    continue
                merged[k] = v
        # api_key is appended LAST so it always wins.
        merged["api_key"] = api_key
        return merged

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
            if not isinstance(body, dict):
                body = {"raw": body}
        except Exception:
            body = {"raw": response.text}

        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors:
            message = ", ".join(
                str(e.get("details") or e.get("code") or e) for e in errors
            )
        else:
            message = (
                body.get("message") or body.get("error") or str(body) or response.text or ""
            )
            if not isinstance(message, str):
                message = str(message)

        ctx = f": {context}" if context else ""
        if status in (401, 403):
            raise HunterAuthError(
                f"{status} unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        if status == 404:
            raise HunterNotFoundError(
                f"404 not found{ctx}: {message}",
                status_code=404,
                response_body=body,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after) if retry_after else 1.0
            except (TypeError, ValueError):
                retry_after_s = 1.0
            raise HunterRateLimitError(
                f"429 rate limited{ctx}: {message}", retry_after_s=retry_after_s
            )
        if status >= 500:
            raise HunterServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        raise HunterError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body,
        )

    async def _backoff_sleep(self, attempt: int, retry_after: Optional[str]) -> None:
        """Sleep before a retry. Honor Retry-After when the server sends it."""
        if retry_after:
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = 2.0 ** attempt
        else:
            delay = (2.0 ** attempt) + random.random()
        delay = min(delay, _MAX_BACKOFF_S)
        await asyncio.sleep(delay)

    async def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        url = self._url(path)
        merged_params = self._params(api_key, params)

        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        params=merged_params,
                        json=json_body,
                    )
            except (httpx.ConnectError, httpx.ReadError, httpx.WriteError) as exc:
                last_error = HunterNetworkError(
                    f"network error{': ' + context if context else ''}: {exc}"
                )
                if attempt < self._max_retries:
                    logger.warning(
                        "hunter.http.transport_retry",
                        attempt=attempt + 1,
                        context=context,
                        error=str(exc),
                    )
                    await self._backoff_sleep(attempt, None)
                    continue
                raise last_error from exc
            except httpx.TimeoutException as exc:
                last_error = HunterNetworkError(
                    f"timeout{': ' + context if context else ''}: {exc}"
                )
                if attempt < self._max_retries:
                    await self._backoff_sleep(attempt, None)
                    continue
                raise last_error from exc

            if (
                response.status_code in _RETRYABLE_STATUS
                and attempt < self._max_retries
            ):
                logger.warning(
                    "hunter.http.retryable_status",
                    status=response.status_code,
                    attempt=attempt + 1,
                    context=context,
                )
                await self._backoff_sleep(attempt, response.headers.get("Retry-After"))
                continue

            self._raise_for_status(response, context=context)
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except Exception as exc:
                raise HunterError(
                    f"invalid JSON response{': ' + context if context else ''}: {exc}"
                ) from exc

        # Exhausted retries without a successful response or raised error.
        if last_error:
            raise last_error
        raise HunterError(
            f"request exhausted retries{': ' + context if context else ''}"
        )

    # ── Account ─────────────────────────────────────────────────────────────

    async def get_account(self, api_key: str) -> Dict[str, Any]:
        """GET /account."""
        return await self._request("GET", "/account", api_key, context="get_account")

    # ── Domain / email discovery ────────────────────────────────────────────

    async def domain_search(
        self,
        api_key: str,
        *,
        domain: Optional[str] = None,
        company: Optional[str] = None,
        limit: int = 25,
        offset: int = 0,
        type: Optional[str] = None,
        seniority: Optional[str] = None,
        department: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /domain-search."""
        params: Dict[str, Any] = {
            "domain": domain,
            "company": company,
            "limit": limit,
            "offset": offset,
            "type": type,
            "seniority": seniority,
            "department": department,
        }
        return await self._request(
            "GET", "/domain-search", api_key, params=params, context="domain_search"
        )

    async def email_finder(
        self,
        api_key: str,
        *,
        domain: Optional[str] = None,
        company: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        full_name: Optional[str] = None,
        max_duration: int = 10,
    ) -> Dict[str, Any]:
        """GET /email-finder."""
        params: Dict[str, Any] = {
            "domain": domain,
            "company": company,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "max_duration": max_duration,
        }
        return await self._request(
            "GET", "/email-finder", api_key, params=params, context="email_finder"
        )

    async def email_verifier(self, api_key: str, *, email: str) -> Dict[str, Any]:
        """GET /email-verifier."""
        return await self._request(
            "GET",
            "/email-verifier",
            api_key,
            params={"email": email},
            context="email_verifier",
        )

    async def email_count(
        self,
        api_key: str,
        *,
        domain: Optional[str] = None,
        company: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /email-count."""
        params: Dict[str, Any] = {"domain": domain, "company": company, "type": type}
        return await self._request(
            "GET", "/email-count", api_key, params=params, context="email_count"
        )

    # ── Leads ───────────────────────────────────────────────────────────────

    async def list_leads(
        self,
        api_key: str,
        *,
        offset: int = 0,
        limit: int = 20,
        lead_list_id: Optional[int] = None,
        email: Optional[str] = None,
        domain: Optional[str] = None,
        company: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /leads."""
        params: Dict[str, Any] = {
            "offset": offset,
            "limit": limit,
            "lead_list_id": lead_list_id,
            "email": email,
            "domain": domain,
            "company": company,
        }
        return await self._request(
            "GET", "/leads", api_key, params=params, context="list_leads"
        )

    async def get_lead(self, api_key: str, *, lead_id: int) -> Dict[str, Any]:
        """GET /leads/{lead_id}."""
        return await self._request(
            "GET",
            f"/leads/{lead_id}",
            api_key,
            context=f"get_lead({lead_id})",
        )

    async def create_lead(
        self, api_key: str, *, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /leads."""
        return await self._request(
            "POST",
            "/leads",
            api_key,
            json_body=payload,
            context="create_lead",
        )

    async def update_lead(
        self, api_key: str, *, lead_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /leads/{lead_id}."""
        return await self._request(
            "PUT",
            f"/leads/{lead_id}",
            api_key,
            json_body=fields,
            context=f"update_lead({lead_id})",
        )

    async def delete_lead(self, api_key: str, *, lead_id: int) -> Dict[str, Any]:
        """DELETE /leads/{lead_id}."""
        return await self._request(
            "DELETE",
            f"/leads/{lead_id}",
            api_key,
            context=f"delete_lead({lead_id})",
        )

    # ── Lead lists ──────────────────────────────────────────────────────────

    async def list_lead_lists(
        self, api_key: str, *, offset: int = 0, limit: int = 20
    ) -> Dict[str, Any]:
        """GET /leads_lists."""
        return await self._request(
            "GET",
            "/leads_lists",
            api_key,
            params={"offset": offset, "limit": limit},
            context="list_lead_lists",
        )

    async def create_lead_list(
        self, api_key: str, *, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /leads_lists."""
        return await self._request(
            "POST",
            "/leads_lists",
            api_key,
            json_body=payload,
            context="create_lead_list",
        )

    # ── Campaigns ──────────────────────────────────────────────────────────

    async def list_campaigns(
        self, api_key: str, *, offset: int = 0, limit: int = 20
    ) -> Dict[str, Any]:
        """GET /campaigns."""
        return await self._request(
            "GET",
            "/campaigns",
            api_key,
            params={"offset": offset, "limit": limit},
            context="list_campaigns",
        )
