"""All Kommo API HTTP calls — zero business logic, zero normalization.

Tenant-aware: every Kommo account is hosted at ``https://{subdomain}.kommo.com``.
The subdomain is install-time config and is captured here so callers never
need to thread it through every method.

Auth model: long-lived OAuth access token (``AUTH_TYPE = "api_key"``) sent as
``Authorization: Bearer <access_token>``. There is no token refresh — the
operator rotates the token out-of-band.

Retries 429 / 5xx with exponential backoff + jitter (up to 3 attempts).
"""
import asyncio
import random
from typing import Any, Dict, List, Optional, Tuple

import httpx
import structlog

from exceptions import (
    KommoAuthError,
    KommoError,
    KommoNotFound,
    KommoServerError,
)

logger = structlog.get_logger(__name__)

# OCP: retry constants — change here, nowhere else.
_RETRY_DELAY_S: float = 1.0
_BACKOFF_FACTOR: float = 2.0
_MAX_RETRY_DELAY_S: float = 32.0
_MAX_RETRIES: int = 3
_DEFAULT_TIMEOUT_S: float = 30.0


class KommoHTTPClient:
    """Async httpx client for the Kommo REST API.

    Parameters
    ----------
    subdomain:
        The Kommo account subdomain (the ``mycompany`` in
        ``https://mycompany.kommo.com``). Required.
    access_token:
        The long-lived OAuth access token. Sent as ``Bearer <token>``.
    api_version:
        REST API version path segment (default ``"v4"``).
    base_url:
        Optional override of the computed base URL.
    timeout:
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        subdomain: str,
        access_token: str = "",
        api_version: str = "v4",
        base_url: str = "",
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        if not subdomain:
            raise ValueError("subdomain is required to build the Kommo base URL")
        self._subdomain = subdomain.strip().rstrip(".").lower()
        self._access_token = access_token or ""
        self._api_version = (api_version or "v4").strip("/") or "v4"
        if base_url:
            self._base_url = base_url.rstrip("/")
        else:
            self._base_url = (
                f"https://{self._subdomain}.kommo.com/api/{self._api_version}"
            )
        self._timeout = timeout

    # ── URL / header helpers ────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def subdomain(self) -> str:
        return self._subdomain

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Error mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_body(response: httpx.Response) -> Dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"raw": data}
        except Exception:
            return {"raw": response.text}

    def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        body = self._parse_body(response)
        message = ""
        if isinstance(body, dict):
            # Kommo surfaces errors as either ``{"title": ..., "detail": ...}``
            # (RFC 7807) or ``{"validation-errors": [...]}``.
            message = (
                body.get("detail")
                or body.get("title")
                or body.get("message")
                or ""
            )
        message = message or response.text or f"HTTP {status}"
        ctx = f": {context}" if context else ""

        if status == 401 or status == 403:
            raise KommoAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        if status == 404:
            raise KommoNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body,
            )
        raise KommoError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body,
        )

    # ── Core request loop ───────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        context: str = "",
        max_retries: int = _MAX_RETRIES,
    ) -> Dict[str, Any]:
        """Execute an HTTP request with backoff on 429/5xx + transport retry."""
        url = self._build_url(path)
        headers = self._headers()
        last_exc: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as session:
                    response = await session.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = KommoServerError(
                    f"transport error{': ' + context if context else ''}: {exc}",
                )
                if attempt < max_retries:
                    delay = min(
                        _RETRY_DELAY_S * (_BACKOFF_FACTOR ** attempt)
                        + random.uniform(0, 0.5),
                        _MAX_RETRY_DELAY_S,
                    )
                    logger.warning(
                        "kommo.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                break
            else:
                # Retry on 429 / 5xx with exponential backoff + jitter.
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt < max_retries:
                        retry_after = self._parse_retry_after(response)
                        delay = (
                            retry_after
                            if retry_after is not None
                            else min(
                                _RETRY_DELAY_S * (_BACKOFF_FACTOR ** attempt)
                                + random.uniform(0, 0.5),
                                _MAX_RETRY_DELAY_S,
                            )
                        )
                        logger.warning(
                            "kommo.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    data = response.json()
                    return data if isinstance(data, dict) else {"data": data}
                except Exception:
                    return {"raw": response.text}

        if last_exc is not None:
            raise last_exc
        raise KommoError(
            f"request exhausted retries{(': ' + context) if context else ''}",
        )

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> Optional[float]:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ── REST surface — one method per endpoint ─────────────────────────────

    # Account ----------------------------------------------------------------

    async def get_account(self) -> Dict[str, Any]:
        """GET /account — health probe."""
        return await self._request("GET", "/account", context="get_account")

    # Leads ------------------------------------------------------------------

    async def list_leads(
        self,
        page: int = 1,
        limit: int = 50,
        query: Optional[str] = None,
        filter_: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "limit": limit}
        if query:
            params["query"] = query
        if filter_:
            for key, value in self._flatten_filter(filter_):
                params[key] = value
        return await self._request(
            "GET", "/leads", params=params, context="list_leads",
        )

    async def get_lead(self, lead_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/leads/{lead_id}", context=f"get_lead({lead_id})",
        )

    async def create_leads(self, leads: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._request(
            "POST", "/leads", json_body=leads, context="create_leads",
        )

    async def update_lead(
        self, lead_id: int, fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/leads/{lead_id}",
            json_body=fields,
            context=f"update_lead({lead_id})",
        )

    async def delete_lead(self, lead_id: int) -> Dict[str, Any]:
        return await self._request(
            "DELETE", f"/leads/{lead_id}", context=f"delete_lead({lead_id})",
        )

    # Contacts ---------------------------------------------------------------

    async def list_contacts(
        self,
        page: int = 1,
        limit: int = 50,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "limit": limit}
        if query:
            params["query"] = query
        return await self._request(
            "GET", "/contacts", params=params, context="list_contacts",
        )

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/contacts/{contact_id}", context=f"get_contact({contact_id})",
        )

    async def create_contacts(
        self, contacts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return await self._request(
            "POST", "/contacts", json_body=contacts, context="create_contacts",
        )

    async def update_contact(
        self, contact_id: int, fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/contacts/{contact_id}",
            json_body=fields,
            context=f"update_contact({contact_id})",
        )

    async def delete_contact(self, contact_id: int) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/contacts/{contact_id}",
            context=f"delete_contact({contact_id})",
        )

    # Companies --------------------------------------------------------------

    async def list_companies(
        self, page: int = 1, limit: int = 50,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "limit": limit}
        return await self._request(
            "GET", "/companies", params=params, context="list_companies",
        )

    async def get_company(self, company_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/companies/{company_id}",
            context=f"get_company({company_id})",
        )

    async def create_companies(
        self, companies: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return await self._request(
            "POST", "/companies", json_body=companies, context="create_companies",
        )

    async def update_company(
        self, company_id: int, fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/companies/{company_id}",
            json_body=fields,
            context=f"update_company({company_id})",
        )

    async def delete_company(self, company_id: int) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/companies/{company_id}",
            context=f"delete_company({company_id})",
        )

    # Customers --------------------------------------------------------------

    async def list_customers(
        self, page: int = 1, limit: int = 50,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "limit": limit}
        return await self._request(
            "GET", "/customers", params=params, context="list_customers",
        )

    # Tasks ------------------------------------------------------------------

    async def list_tasks(
        self,
        page: int = 1,
        limit: int = 50,
        filter_: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "limit": limit}
        if filter_:
            for key, value in self._flatten_filter(filter_):
                params[key] = value
        return await self._request(
            "GET", "/tasks", params=params, context="list_tasks",
        )

    async def get_task(self, task_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/tasks/{task_id}", context=f"get_task({task_id})",
        )

    async def create_tasks(self, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._request(
            "POST", "/tasks", json_body=tasks, context="create_tasks",
        )

    async def update_task(
        self, task_id: int, fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/tasks/{task_id}",
            json_body=fields,
            context=f"update_task({task_id})",
        )

    async def delete_task(self, task_id: int) -> Dict[str, Any]:
        return await self._request(
            "DELETE", f"/tasks/{task_id}", context=f"delete_task({task_id})",
        )

    # Events -----------------------------------------------------------------

    async def list_events(
        self,
        page: int = 1,
        limit: int = 50,
        filter_: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "limit": limit}
        if filter_:
            for key, value in self._flatten_filter(filter_):
                params[key] = value
        return await self._request(
            "GET", "/events", params=params, context="list_events",
        )

    # Notes ------------------------------------------------------------------

    async def list_notes(
        self,
        entity_type: str,
        entity_id: int,
        page: int = 1,
        limit: int = 50,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "limit": limit}
        return await self._request(
            "GET",
            f"/{entity_type}/{entity_id}/notes",
            params=params,
            context=f"list_notes({entity_type},{entity_id})",
        )

    async def create_notes(
        self,
        entity_type: str,
        entity_id: int,
        notes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/{entity_type}/{entity_id}/notes",
            json_body=notes,
            context=f"create_notes({entity_type},{entity_id})",
        )

    # Custom Fields ----------------------------------------------------------

    async def list_custom_fields(self, entity_type: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/{entity_type}/custom_fields",
            context=f"list_custom_fields({entity_type})",
        )

    async def create_custom_fields(
        self,
        entity_type: str,
        fields: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/{entity_type}/custom_fields",
            json_body=fields,
            context=f"create_custom_fields({entity_type})",
        )

    # Pipelines / Users ------------------------------------------------------

    async def list_pipelines(self) -> Dict[str, Any]:
        return await self._request(
            "GET", "/leads/pipelines", context="list_pipelines",
        )

    async def list_users(self) -> Dict[str, Any]:
        return await self._request("GET", "/users", context="list_users")

    # Webhooks ---------------------------------------------------------------

    async def list_webhooks(self) -> Dict[str, Any]:
        return await self._request(
            "GET", "/webhooks", context="list_webhooks",
        )

    async def create_webhook(
        self,
        destination: str,
        settings: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"destination": destination}
        if settings:
            body["settings"] = settings
        return await self._request(
            "POST", "/webhooks", json_body=body, context="create_webhook",
        )

    async def delete_webhook(self, destination: str) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            "/webhooks",
            json_body={"destination": destination},
            context="delete_webhook",
        )

    # ── Filter flattening ──────────────────────────────────────────────────

    @staticmethod
    def _flatten_filter(
        filter_: Dict[str, Any], parent: str = "filter",
    ) -> List[Tuple[str, Any]]:
        """Flatten ``{"a": {"b": 1}}`` → ``[("filter[a][b]", 1)]``.

        Kommo expects nested bracket-style filter params. Lists become indexed.
        """
        out: List[Tuple[str, Any]] = []
        for key, value in filter_.items():
            current_key = f"{parent}[{key}]"
            if isinstance(value, dict):
                out.extend(KommoHTTPClient._flatten_filter(value, parent=current_key))
            elif isinstance(value, (list, tuple)):
                for idx, item in enumerate(value):
                    if isinstance(item, dict):
                        out.extend(
                            KommoHTTPClient._flatten_filter(
                                item, parent=f"{current_key}[{idx}]",
                            )
                        )
                    else:
                        out.append((f"{current_key}[{idx}]", item))
            else:
                out.append((current_key, value))
        return out
