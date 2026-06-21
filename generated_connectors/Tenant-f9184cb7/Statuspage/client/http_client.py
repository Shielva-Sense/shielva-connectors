"""All Statuspage REST API HTTP calls — zero business logic, zero normalization.

Async httpx client. Auth header is ``Authorization: OAuth {api_key}`` (Statuspage
uses the literal "OAuth" scheme keyword for API-token auth — **not** Bearer).
Retries with exponential backoff + jitter on 429 and 5xx responses, honouring
``Retry-After`` when present.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    StatuspageAuthError,
    StatuspageError,
    StatuspageNetworkError,
    StatuspageNotFound,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BASE = "https://api.statuspage.io/v1"
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BASE_DELAY = 0.5
_MAX_DELAY = 8.0


class StatuspageHTTPClient:
    """Thin async HTTP client for the Statuspage REST API.

    Auth: ``Authorization: OAuth {api_key}`` (NOT Bearer).
    Retries 429/5xx with exponential backoff + jitter, honouring ``Retry-After``
    when present. Every public method is a 1:1 mapping to a single REST endpoint
    — no business logic, no normalization.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE,
        timeout: float = 30.0,
        max_retries: int = _MAX_RETRIES,
    ):
        self._api_key = api_key or ""
        self._base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries

    # ── headers ────────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        # Statuspage uses the literal "OAuth" scheme keyword for API tokens.
        return {
            "Authorization": f"OAuth {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── error mapping ──────────────────────────────────────────────────────

    @staticmethod
    def _raise_for_status(resp: httpx.Response, context: str = "") -> None:
        status = resp.status_code
        if status < 400:
            return
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        ctx = f" [{context}]" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}
        if status in (401, 403):
            raise StatuspageAuthError(
                f"HTTP {status}{ctx}: authentication failed",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise StatuspageNotFound(
                f"HTTP 404{ctx}: resource not found",
                status_code=404,
                response_body=body_dict,
            )
        raise StatuspageError(
            f"HTTP {status}{ctx}",
            status_code=status,
            response_body=body_dict,
        )

    # ── retrying request loop ─────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        url = f"{self._base_url}{path}"
        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt <= self._max_retries:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                        json=json_body,
                    )
                if resp.status_code in _RETRY_STATUS and attempt < self._max_retries:
                    delay = self._retry_delay(attempt, resp)
                    logger.warning(
                        "statuspage.http.retry",
                        status=resp.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                self._raise_for_status(resp, context=context or f"{method} {path}")
                if resp.status_code == 204 or not resp.content:
                    return {}
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text}
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    raise StatuspageNetworkError(
                        f"transport error after {attempt + 1} attempts: {exc}",
                    ) from exc
                logger.warning(
                    "statuspage.http.transport_retry",
                    attempt=attempt + 1,
                    context=context,
                    error=str(exc),
                )
                await asyncio.sleep(self._retry_delay(attempt, None))
                attempt += 1
        if last_exc is not None:
            raise StatuspageNetworkError(str(last_exc)) from last_exc
        raise StatuspageError("exhausted retries")

    @staticmethod
    def _retry_delay(attempt: int, resp: Optional[httpx.Response]) -> float:
        if resp is not None:
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    return min(float(ra), _MAX_DELAY)
                except ValueError:
                    pass
        delay = min(_BASE_DELAY * (2 ** attempt), _MAX_DELAY)
        return delay + random.uniform(0, 0.25)

    # ── Pages ──────────────────────────────────────────────────────────────

    async def list_pages(
        self, page: int = 1, per_page: int = 100
    ) -> List[Dict[str, Any]]:
        return await self._request(
            "GET",
            "/pages",
            params={"page": page, "per_page": per_page},
            context="list_pages",
        )

    async def get_page(self, page_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/pages/{page_id}", context=f"get_page({page_id})"
        )

    # ── Components ─────────────────────────────────────────────────────────

    async def list_components(self, page_id: str) -> List[Dict[str, Any]]:
        return await self._request(
            "GET",
            f"/pages/{page_id}/components",
            context="list_components",
        )

    async def get_component(
        self, page_id: str, component_id: str
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/pages/{page_id}/components/{component_id}",
            context="get_component",
        )

    async def create_component(
        self, page_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/pages/{page_id}/components",
            json_body=body,
            context="create_component",
        )

    async def patch_component(
        self, page_id: str, component_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/pages/{page_id}/components/{component_id}",
            json_body=body,
            context="patch_component",
        )

    async def delete_component(
        self, page_id: str, component_id: str
    ) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/pages/{page_id}/components/{component_id}",
            context="delete_component",
        )

    # ── Component groups ───────────────────────────────────────────────────

    async def list_component_groups(self, page_id: str) -> List[Dict[str, Any]]:
        return await self._request(
            "GET",
            f"/pages/{page_id}/component-groups",
            context="list_component_groups",
        )

    # ── Incidents ──────────────────────────────────────────────────────────

    async def list_incidents(
        self,
        page_id: str,
        q: Optional[str] = None,
        limit: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit, "page": page}
        if q:
            params["q"] = q
        return await self._request(
            "GET",
            f"/pages/{page_id}/incidents",
            params=params,
            context="list_incidents",
        )

    async def get_incident(
        self, page_id: str, incident_id: str
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/pages/{page_id}/incidents/{incident_id}",
            context="get_incident",
        )

    async def create_incident(
        self, page_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/pages/{page_id}/incidents",
            json_body=body,
            context="create_incident",
        )

    async def patch_incident(
        self, page_id: str, incident_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/pages/{page_id}/incidents/{incident_id}",
            json_body=body,
            context="patch_incident",
        )

    # ── Maintenances ───────────────────────────────────────────────────────

    async def list_maintenances(
        self,
        page_id: str,
        limit: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        return await self._request(
            "GET",
            f"/pages/{page_id}/incidents/scheduled",
            params={"limit": limit, "page": page},
            context="list_maintenances",
        )

    # ── Subscribers ────────────────────────────────────────────────────────

    async def list_subscribers(
        self,
        page_id: str,
        type_: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit, "page": page}
        if type_:
            params["type"] = type_
        if state:
            params["state"] = state
        return await self._request(
            "GET",
            f"/pages/{page_id}/subscribers",
            params=params,
            context="list_subscribers",
        )

    async def create_subscriber(
        self, page_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/pages/{page_id}/subscribers",
            json_body=body,
            context="create_subscriber",
        )

    async def delete_subscriber(
        self, page_id: str, subscriber_id: str
    ) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/pages/{page_id}/subscribers/{subscriber_id}",
            context="delete_subscriber",
        )

    # ── Metrics ────────────────────────────────────────────────────────────

    async def list_metrics(self, page_id: str) -> List[Dict[str, Any]]:
        return await self._request(
            "GET", f"/pages/{page_id}/metrics", context="list_metrics"
        )

    # ── Templates ──────────────────────────────────────────────────────────

    async def list_incident_templates(
        self, page_id: str
    ) -> List[Dict[str, Any]]:
        return await self._request(
            "GET",
            f"/pages/{page_id}/incident_templates",
            context="list_incident_templates",
        )
