"""All GoHighLevel API HTTP calls — zero business logic, zero normalization.

httpx async client. The HighLevel REST API expects:
  Authorization: Bearer <api_key>
  Version: <api_version>            (e.g. 2021-07-28 — mandatory)
  Content-Type: application/json
  Accept: application/json

Retry on 429/5xx with exponential backoff.
"""
import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    GoHighLevelAuthError,
    GoHighLevelError,
    GoHighLevelNetworkError,
    GoHighLevelNotFound,
)

logger = structlog.get_logger(__name__)

_GHL_BASE = "https://services.leadconnectorhq.com"
_DEFAULT_API_VERSION = "2021-07-28"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class GoHighLevelHTTPClient:
    """Thin async HTTP client for the GoHighLevel (HighLevel / LeadConnector) REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = _GHL_BASE,
        api_version: str = _DEFAULT_API_VERSION,
        location_id: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._api_key = api_key or ""
        self._base_url = (base_url or _GHL_BASE).rstrip("/")
        self._api_version = api_version or _DEFAULT_API_VERSION
        self._default_location_id = location_id or ""
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Version": self._api_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return headers

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            message = (
                body.get("message")
                or body.get("error")
                or body.get("details")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        if status in (401, 403):
            raise GoHighLevelAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 404:
            raise GoHighLevelNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        raise GoHighLevelError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {"raw": body},
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
                            "gohighlevel.http.retry",
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
                        "gohighlevel.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise GoHighLevelNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise GoHighLevelNetworkError(str(last_exc)) from last_exc
        raise GoHighLevelNetworkError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── Locations ─────────────────────────────────────────────────────────

    async def list_locations(
        self,
        limit: int = 20,
        skip: int = 0,
    ) -> Dict[str, Any]:
        """GET /locations/search — list sub-accounts."""
        params: Dict[str, Any] = {"limit": limit, "skip": skip}
        return await self._request(
            "GET",
            "/locations/search",
            params=params,
            context="list_locations",
        )

    async def get_location(self, location_id: str) -> Dict[str, Any]:
        """GET /locations/{locationId}."""
        return await self._request(
            "GET",
            f"/locations/{location_id}",
            context=f"get_location({location_id})",
        )

    # ── Contacts ──────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        location_id: Optional[str] = None,
        limit: int = 20,
        page: int = 1,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /contacts/?locationId=&limit=&page=&query=."""
        params: Dict[str, Any] = {"limit": limit, "page": page}
        lid = location_id or self._default_location_id
        if lid:
            params["locationId"] = lid
        if query:
            params["query"] = query
        return await self._request(
            "GET",
            "/contacts/",
            params=params,
            context="list_contacts",
        )

    async def get_contact(self, contact_id: str) -> Dict[str, Any]:
        """GET /contacts/{contactId}."""
        return await self._request(
            "GET",
            f"/contacts/{contact_id}",
            context=f"get_contact({contact_id})",
        )

    async def create_contact(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /contacts/."""
        body = dict(payload)
        if "locationId" not in body and self._default_location_id:
            body["locationId"] = self._default_location_id
        return await self._request(
            "POST",
            "/contacts/",
            json_body=body,
            context="create_contact",
        )

    async def update_contact(
        self,
        contact_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /contacts/{contactId}."""
        return await self._request(
            "PUT",
            f"/contacts/{contact_id}",
            json_body=payload,
            context=f"update_contact({contact_id})",
        )

    async def delete_contact(self, contact_id: str) -> Dict[str, Any]:
        """DELETE /contacts/{contactId}."""
        return await self._request(
            "DELETE",
            f"/contacts/{contact_id}",
            context=f"delete_contact({contact_id})",
        )

    # ── Opportunities ─────────────────────────────────────────────────────

    async def list_opportunities(
        self,
        location_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        limit: int = 20,
        page: int = 1,
    ) -> Dict[str, Any]:
        """GET /opportunities/search."""
        params: Dict[str, Any] = {"limit": limit, "page": page}
        lid = location_id or self._default_location_id
        if lid:
            params["location_id"] = lid
        if pipeline_id:
            params["pipeline_id"] = pipeline_id
        return await self._request(
            "GET",
            "/opportunities/search",
            params=params,
            context="list_opportunities",
        )

    async def get_opportunity(self, opportunity_id: str) -> Dict[str, Any]:
        """GET /opportunities/{opportunityId}."""
        return await self._request(
            "GET",
            f"/opportunities/{opportunity_id}",
            context=f"get_opportunity({opportunity_id})",
        )

    async def create_opportunity(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /opportunities/."""
        body = dict(payload)
        if "locationId" not in body and self._default_location_id:
            body["locationId"] = self._default_location_id
        return await self._request(
            "POST",
            "/opportunities/",
            json_body=body,
            context="create_opportunity",
        )

    async def update_opportunity(
        self,
        opportunity_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /opportunities/{opportunityId}."""
        return await self._request(
            "PUT",
            f"/opportunities/{opportunity_id}",
            json_body=payload,
            context=f"update_opportunity({opportunity_id})",
        )

    # ── Conversations ─────────────────────────────────────────────────────

    async def list_conversations(
        self,
        location_id: Optional[str] = None,
        contact_id: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """GET /conversations/search."""
        params: Dict[str, Any] = {"limit": limit}
        lid = location_id or self._default_location_id
        if lid:
            params["locationId"] = lid
        if contact_id:
            params["contactId"] = contact_id
        return await self._request(
            "GET",
            "/conversations/search",
            params=params,
            context="list_conversations",
        )

    async def get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """GET /conversations/{conversationId}."""
        return await self._request(
            "GET",
            f"/conversations/{conversation_id}",
            context=f"get_conversation({conversation_id})",
        )

    async def send_message(
        self,
        conversation_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /conversations/messages — send SMS/Email/IG/FB."""
        body = dict(payload)
        body.setdefault("conversationId", conversation_id)
        return await self._request(
            "POST",
            "/conversations/messages",
            json_body=body,
            context="send_message",
        )

    # ── Calendars / Pipelines / Users / Campaigns / Custom Fields / Tags ──

    async def list_calendars(
        self,
        location_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /calendars/?locationId=."""
        params: Dict[str, Any] = {}
        lid = location_id or self._default_location_id
        if lid:
            params["locationId"] = lid
        return await self._request(
            "GET",
            "/calendars/",
            params=params,
            context="list_calendars",
        )

    async def list_pipelines(
        self,
        location_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /opportunities/pipelines?locationId=."""
        params: Dict[str, Any] = {}
        lid = location_id or self._default_location_id
        if lid:
            params["locationId"] = lid
        return await self._request(
            "GET",
            "/opportunities/pipelines",
            params=params,
            context="list_pipelines",
        )

    async def list_users(
        self,
        location_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /users/?locationId=."""
        params: Dict[str, Any] = {}
        lid = location_id or self._default_location_id
        if lid:
            params["locationId"] = lid
        return await self._request(
            "GET",
            "/users/",
            params=params,
            context="list_users",
        )

    async def list_campaigns(
        self,
        location_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /campaigns/?locationId=."""
        params: Dict[str, Any] = {}
        lid = location_id or self._default_location_id
        if lid:
            params["locationId"] = lid
        return await self._request(
            "GET",
            "/campaigns/",
            params=params,
            context="list_campaigns",
        )

    async def list_custom_fields(self, location_id: str) -> Dict[str, Any]:
        """GET /locations/{locationId}/customFields."""
        return await self._request(
            "GET",
            f"/locations/{location_id}/customFields",
            context=f"list_custom_fields({location_id})",
        )

    async def list_tags(self, location_id: str) -> Dict[str, Any]:
        """GET /locations/{locationId}/tags."""
        return await self._request(
            "GET",
            f"/locations/{location_id}/tags",
            context=f"list_tags({location_id})",
        )
