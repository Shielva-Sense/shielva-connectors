"""All Insightly REST API HTTP calls — zero business logic, zero normalization.

The Insightly API:
- Base URL: https://api.{pod}.insightly.com/v3.1
- Auth: HTTP Basic with `api_key` as username and an empty password.
- Pagination: `?top=N&skip=M` (OData-style)
- All responses are JSON arrays / objects.

Retry on 429/5xx and transport errors with exponential backoff. Status codes
map to typed exceptions defined in `exceptions.py`.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    InsightlyAuthError,
    InsightlyBadRequestError,
    InsightlyConflictError,
    InsightlyError,
    InsightlyNetworkError,
    InsightlyNotFoundError,
    InsightlyRateLimitError,
    InsightlyServerError,
)
from helpers.utils import build_basic_auth_header

logger = structlog.get_logger(__name__)

_INSIGHTLY_BASE_TEMPLATE = "https://api.{pod}.insightly.com/v3.1"
_DEFAULT_TIMEOUT_S: float = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class InsightlyHTTPClient:
    """Thin async HTTP client for the Insightly REST API.

    All methods return raw JSON (list or dict). Auth + retry + status mapping
    are owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        api_key: str,
        pod: str = "na1",
        base_url: Optional[str] = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not pod and not base_url:
            raise ValueError("pod or base_url is required")
        self._api_key = api_key
        self._pod = pod or "na1"
        self._base_url = (
            base_url.rstrip("/")
            if base_url
            else _INSIGHTLY_BASE_TEMPLATE.format(pod=self._pod)
        )
        self._timeout = httpx.Timeout(timeout_s)
        self._auth_header = build_basic_auth_header(api_key)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": self._auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return {}

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        status = response.status_code
        if status < 400:
            return
        body = self._safe_json(response) if response.content else {}
        if isinstance(body, dict):
            message = body.get("Message") or body.get("message") or str(body)
        else:
            message = str(body)

        ctx = f" ({context})" if context else ""
        body_dict: Dict[str, Any] = body if isinstance(body, dict) else {"raw": body}

        if status in (401, 403):
            raise InsightlyAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 400:
            raise InsightlyBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 404:
            raise InsightlyNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise InsightlyConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            raise InsightlyRateLimitError(
                f"429 Rate limit exceeded{ctx}",
                status_code=429,
                response_body=body_dict,
            )
        if status >= 500:
            raise InsightlyServerError(
                f"HTTP {status} server error{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise InsightlyError(
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
        json_body: Optional[Any] = None,
        context: str = "",
    ) -> Any:
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
                            "insightly.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return None
                return self._safe_json(response)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "insightly.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise InsightlyNetworkError(
                    f"Transport error{(': ' + context) if context else ''}: {exc}"
                ) from exc

        if last_exc:
            raise InsightlyNetworkError(str(last_exc)) from last_exc
        raise InsightlyNetworkError(
            f"Exhausted retries{(': ' + context) if context else ''}"
        )

    # ── Identity / health ───────────────────────────────────────────────────

    async def get_me(self) -> Dict[str, Any]:
        """GET /Users/Me — cheapest authenticated probe (health-check target)."""
        result = await self._request("GET", "/Users/Me", context="get_me")
        return result if isinstance(result, dict) else {}

    async def list_users(self) -> List[Dict[str, Any]]:
        """GET /Users — list account users."""
        result = await self._request("GET", "/Users", context="list_users")
        return result if isinstance(result, list) else []

    # ── Contacts ────────────────────────────────────────────────────────────

    async def list_contacts(
        self, top: int = 50, skip: int = 0, brief: bool = False
    ) -> List[Dict[str, Any]]:
        """GET /Contacts?top=&skip=&brief="""
        params: Dict[str, Any] = {
            "top": top,
            "skip": skip,
            "brief": str(brief).lower(),
        }
        result = await self._request(
            "GET", "/Contacts", params=params, context="list_contacts"
        )
        return result if isinstance(result, list) else []

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        result = await self._request(
            "GET", f"/Contacts/{contact_id}", context=f"get_contact({contact_id})"
        )
        return result if isinstance(result, dict) else {}

    async def create_contact(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._request(
            "POST", "/Contacts", json_body=payload, context="create_contact"
        )
        return result if isinstance(result, dict) else {}

    async def update_contact(
        self, contact_id: int, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /Contacts/{id} — Insightly's PUT expects the FULL record + CONTACT_ID."""
        body = {**payload, "CONTACT_ID": contact_id}
        result = await self._request(
            "PUT",
            f"/Contacts/{contact_id}",
            json_body=body,
            context=f"update_contact({contact_id})",
        )
        return result if isinstance(result, dict) else {}

    async def delete_contact(self, contact_id: int) -> None:
        await self._request(
            "DELETE",
            f"/Contacts/{contact_id}",
            context=f"delete_contact({contact_id})",
        )

    # ── Organisations ───────────────────────────────────────────────────────

    async def list_organisations(
        self, top: int = 50, skip: int = 0
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"top": top, "skip": skip}
        result = await self._request(
            "GET", "/Organisations", params=params, context="list_organisations"
        )
        return result if isinstance(result, list) else []

    async def get_organisation(self, organisation_id: int) -> Dict[str, Any]:
        result = await self._request(
            "GET",
            f"/Organisations/{organisation_id}",
            context=f"get_organisation({organisation_id})",
        )
        return result if isinstance(result, dict) else {}

    async def create_organisation(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._request(
            "POST",
            "/Organisations",
            json_body=payload,
            context="create_organisation",
        )
        return result if isinstance(result, dict) else {}

    async def update_organisation(
        self, organisation_id: int, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        body = {**payload, "ORGANISATION_ID": organisation_id}
        result = await self._request(
            "PUT",
            f"/Organisations/{organisation_id}",
            json_body=body,
            context=f"update_organisation({organisation_id})",
        )
        return result if isinstance(result, dict) else {}

    async def delete_organisation(self, organisation_id: int) -> None:
        await self._request(
            "DELETE",
            f"/Organisations/{organisation_id}",
            context=f"delete_organisation({organisation_id})",
        )

    # ── Opportunities ───────────────────────────────────────────────────────

    async def list_opportunities(
        self, top: int = 50, skip: int = 0
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"top": top, "skip": skip}
        result = await self._request(
            "GET", "/Opportunities", params=params, context="list_opportunities"
        )
        return result if isinstance(result, list) else []

    async def get_opportunity(self, opportunity_id: int) -> Dict[str, Any]:
        result = await self._request(
            "GET",
            f"/Opportunities/{opportunity_id}",
            context=f"get_opportunity({opportunity_id})",
        )
        return result if isinstance(result, dict) else {}

    async def create_opportunity(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._request(
            "POST", "/Opportunities", json_body=payload, context="create_opportunity"
        )
        return result if isinstance(result, dict) else {}

    async def update_opportunity(
        self, opportunity_id: int, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        body = {**payload, "OPPORTUNITY_ID": opportunity_id}
        result = await self._request(
            "PUT",
            f"/Opportunities/{opportunity_id}",
            json_body=body,
            context=f"update_opportunity({opportunity_id})",
        )
        return result if isinstance(result, dict) else {}

    async def delete_opportunity(self, opportunity_id: int) -> None:
        await self._request(
            "DELETE",
            f"/Opportunities/{opportunity_id}",
            context=f"delete_opportunity({opportunity_id})",
        )

    # ── Leads ───────────────────────────────────────────────────────────────

    async def list_leads(self, top: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"top": top, "skip": skip}
        result = await self._request(
            "GET", "/Leads", params=params, context="list_leads"
        )
        return result if isinstance(result, list) else []

    async def get_lead(self, lead_id: int) -> Dict[str, Any]:
        result = await self._request(
            "GET", f"/Leads/{lead_id}", context=f"get_lead({lead_id})"
        )
        return result if isinstance(result, dict) else {}

    async def create_lead(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._request(
            "POST", "/Leads", json_body=payload, context="create_lead"
        )
        return result if isinstance(result, dict) else {}

    async def update_lead(
        self, lead_id: int, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        body = {**payload, "LEAD_ID": lead_id}
        result = await self._request(
            "PUT",
            f"/Leads/{lead_id}",
            json_body=body,
            context=f"update_lead({lead_id})",
        )
        return result if isinstance(result, dict) else {}

    async def delete_lead(self, lead_id: int) -> None:
        await self._request(
            "DELETE", f"/Leads/{lead_id}", context=f"delete_lead({lead_id})"
        )

    # ── Projects ────────────────────────────────────────────────────────────

    async def list_projects(self, top: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"top": top, "skip": skip}
        result = await self._request(
            "GET", "/Projects", params=params, context="list_projects"
        )
        return result if isinstance(result, list) else []

    async def get_project(self, project_id: int) -> Dict[str, Any]:
        result = await self._request(
            "GET", f"/Projects/{project_id}", context=f"get_project({project_id})"
        )
        return result if isinstance(result, dict) else {}

    async def create_project(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._request(
            "POST", "/Projects", json_body=payload, context="create_project"
        )
        return result if isinstance(result, dict) else {}

    async def update_project(
        self, project_id: int, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        body = {**payload, "PROJECT_ID": project_id}
        result = await self._request(
            "PUT",
            f"/Projects/{project_id}",
            json_body=body,
            context=f"update_project({project_id})",
        )
        return result if isinstance(result, dict) else {}

    async def delete_project(self, project_id: int) -> None:
        await self._request(
            "DELETE",
            f"/Projects/{project_id}",
            context=f"delete_project({project_id})",
        )

    # ── Tasks ───────────────────────────────────────────────────────────────

    async def list_tasks(self, top: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"top": top, "skip": skip}
        result = await self._request(
            "GET", "/Tasks", params=params, context="list_tasks"
        )
        return result if isinstance(result, list) else []

    async def get_task(self, task_id: int) -> Dict[str, Any]:
        result = await self._request(
            "GET", f"/Tasks/{task_id}", context=f"get_task({task_id})"
        )
        return result if isinstance(result, dict) else {}

    async def create_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._request(
            "POST", "/Tasks", json_body=payload, context="create_task"
        )
        return result if isinstance(result, dict) else {}

    async def update_task(
        self, task_id: int, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        body = {**payload, "TASK_ID": task_id}
        result = await self._request(
            "PUT",
            f"/Tasks/{task_id}",
            json_body=body,
            context=f"update_task({task_id})",
        )
        return result if isinstance(result, dict) else {}

    async def delete_task(self, task_id: int) -> None:
        await self._request(
            "DELETE", f"/Tasks/{task_id}", context=f"delete_task({task_id})"
        )

    # ── Read-only surfaces (Events / Notes / Emails / Pipelines / etc.) ─────

    async def list_events(self, top: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"top": top, "skip": skip}
        result = await self._request(
            "GET", "/Events", params=params, context="list_events"
        )
        return result if isinstance(result, list) else []

    async def list_notes(self, top: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"top": top, "skip": skip}
        result = await self._request(
            "GET", "/Notes", params=params, context="list_notes"
        )
        return result if isinstance(result, list) else []

    async def list_emails(self, top: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"top": top, "skip": skip}
        result = await self._request(
            "GET", "/Emails", params=params, context="list_emails"
        )
        return result if isinstance(result, list) else []

    async def list_pipelines(self) -> List[Dict[str, Any]]:
        result = await self._request("GET", "/Pipelines", context="list_pipelines")
        return result if isinstance(result, list) else []

    async def list_custom_objects(self) -> List[Dict[str, Any]]:
        result = await self._request(
            "GET", "/CustomObjects", context="list_custom_objects"
        )
        return result if isinstance(result, list) else []

    async def list_tags(self, record_type: str = "contacts") -> List[Dict[str, Any]]:
        result = await self._request(
            "GET", f"/Tags/{record_type}", context=f"list_tags({record_type})"
        )
        return result if isinstance(result, list) else []
