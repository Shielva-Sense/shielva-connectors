"""HTTP transport for the Hunter.io REST API.

- httpx.AsyncClient (one transport per connector instance)
- automatic `?api_key=<key>` query parameter injection on every request
- retry with exponential backoff on 429 + 5xx
- typed exception mapping (HunterAuthError / HunterNotFound / HunterNetworkError / HunterError)

All methods accept an `api_key` argument and return the raw parsed JSON dict.
No normalization, no business logic.
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
    HunterNotFound,
)

logger = structlog.get_logger(__name__)

_HUNTER_BASE = "https://api.hunter.io/v2"
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HunterHTTPClient:
    """Thin async HTTP client for Hunter.io v2.

    Every request is auto-stamped with `?api_key=<key>`. The query param is
    appended (never logged in cleartext) and merged with caller-supplied params.
    Retries on 429 + 5xx with capped exponential backoff + jitter.
    """

    def __init__(
        self,
        base_url: str = _HUNTER_BASE,
        max_retries: int = 3,
        timeout: Optional[httpx.Timeout] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(0, int(max_retries))
        self._timeout = timeout or _DEFAULT_TIMEOUT

    # ── Internal helpers ────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _params(self, api_key: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge caller params with api_key. Drops None values so httpx
        does not emit `?domain=None`."""
        merged: Dict[str, Any] = {}
        if params:
            for k, v in params.items():
                if v is None:
                    continue
                merged[k] = v
        # api_key is appended LAST so it always wins if the caller mistakenly
        # passed one through `params`.
        merged["api_key"] = api_key
        return merged

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {}

        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors:
            message = ", ".join(
                str(e.get("details") or e.get("code") or e) for e in errors
            )
        else:
            message = str(body) if body else response.text or ""

        ctx = f": {context}" if context else ""
        if status in (401, 403):
            raise HunterAuthError(
                f"{status} unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        if status == 404:
            raise HunterNotFound(
                f"404 not found{ctx}: {message}",
                status_code=404,
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
        delay = min(delay, 30.0)
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

            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                logger.warning(
                    "hunter.http.retryable_status",
                    status=response.status_code,
                    attempt=attempt,
                    context=context,
                )
                await self._backoff_sleep(attempt, response.headers.get("Retry-After"))
                continue

            self._raise_for_status(response, context)
            try:
                return response.json()
            except Exception as exc:
                raise HunterError(
                    f"invalid JSON response{': ' + context if context else ''}: {exc}"
                ) from exc

        # Exhausted retries without a successful response or raised error.
        if last_error:
            raise last_error
        raise HunterError(f"request exhausted retries{': ' + context if context else ''}")

    # ── Account ─────────────────────────────────────────────────────────────

    async def get_account(self, api_key: str) -> Dict[str, Any]:
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
        params: Dict[str, Any] = {"domain": domain, "company": company, "type": type}
        return await self._request(
            "GET", "/email-count", api_key, params=params, context="email_count"
        )

    # ── Enrichment ──────────────────────────────────────────────────────────

    async def combined_enrichment(self, api_key: str, *, email: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/enrichment/combined",
            api_key,
            params={"email": email},
            context="combined_enrichment",
        )

    async def company_enrichment(self, api_key: str, *, domain: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/enrichment/company",
            api_key,
            params={"domain": domain},
            context="company_enrichment",
        )

    async def person_enrichment(self, api_key: str, *, email: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/enrichment/person",
            api_key,
            params={"email": email},
            context="person_enrichment",
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
        return await self._request(
            "GET", f"/leads/{lead_id}", api_key, context=f"get_lead({lead_id})"
        )

    async def create_lead(
        self, api_key: str, *, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
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
        return await self._request(
            "PUT",
            f"/leads/{lead_id}",
            api_key,
            json_body=fields,
            context=f"update_lead({lead_id})",
        )

    async def delete_lead(self, api_key: str, *, lead_id: int) -> Dict[str, Any]:
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
        return await self._request(
            "POST",
            "/leads_lists",
            api_key,
            json_body=payload,
            context="create_lead_list",
        )
