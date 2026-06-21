"""All Hubstaff API HTTP calls — zero business logic, zero normalization.

httpx async client. The Hubstaff REST API expects:
  Authorization: Bearer <access_token>
  Content-Type:  application/json
  Accept:        application/json

Retry on 429/5xx with exponential backoff. Honour ``Retry-After`` on 429.
"""
import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    HubstaffAuthError,
    HubstaffError,
    HubstaffNetworkError,
    HubstaffNotFound,
    HubstaffRateLimitError,
)

logger = structlog.get_logger(__name__)

_HUBSTAFF_BASE = "https://api.hubstaff.com/v2"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class HubstaffHTTPClient:
    """Thin async HTTP client for the Hubstaff v2 REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        access_token: str = "",
        base_url: str = _HUBSTAFF_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._access_token = access_token or ""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
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
            error_obj = body.get("error")
            if isinstance(error_obj, dict):
                message = error_obj.get("message") or str(body)
            elif isinstance(error_obj, str):
                message = error_obj
            else:
                message = (
                    body.get("message")
                    or body.get("details")
                    or str(body)
                )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        if status == 401 or status == 403:
            raise HubstaffAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 404:
            raise HubstaffNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 429:
            try:
                retry_after_s = float(response.headers.get("Retry-After", "5"))
            except (TypeError, ValueError):
                retry_after_s = 5.0
            err = HubstaffRateLimitError(
                f"429 Rate Limit{ctx}: {message}",
                retry_after_s=retry_after_s,
            )
            err.response_body = body if isinstance(body, dict) else {"raw": body}
            raise err
        raise HubstaffError(
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
                        # Honour Retry-After on 429 if present.
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = (
                                float(retry_after)
                                if retry_after and response.status_code == 429
                                else _BACKOFF_BASE * (2 ** attempt)
                            )
                        except ValueError:
                            delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "hubstaff.http.retry",
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
                        "hubstaff.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise HubstaffNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise HubstaffNetworkError(str(last_exc)) from last_exc
        raise HubstaffNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    @staticmethod
    def _csv(values: Optional[List[Any]]) -> Optional[str]:
        if not values:
            return None
        return ",".join(str(v) for v in values)

    # ── Identity ───────────────────────────────────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /users/me."""
        return await self._request("GET", "/users/me", context="get_current_user")

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        """GET /users/{id}."""
        return await self._request(
            "GET",
            f"/users/{user_id}",
            context=f"get_user({user_id})",
        )

    # ── Organizations ─────────────────────────────────────────────────────

    async def list_organizations(
        self,
        page_start_id: Optional[int] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations."""
        params: Dict[str, Any] = {"page_limit": page_limit}
        if page_start_id is not None:
            params["page_start_id"] = page_start_id
        return await self._request(
            "GET",
            "/organizations",
            params=params,
            context="list_organizations",
        )

    async def list_users(
        self,
        organization_id: int,
        page_start_id: Optional[int] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/members."""
        params: Dict[str, Any] = {"page_limit": page_limit}
        if page_start_id is not None:
            params["page_start_id"] = page_start_id
        return await self._request(
            "GET",
            f"/organizations/{organization_id}/members",
            params=params,
            context="list_users",
        )

    async def list_teams(
        self,
        organization_id: int,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/teams."""
        return await self._request(
            "GET",
            f"/organizations/{organization_id}/teams",
            context="list_teams",
        )

    # ── Projects ───────────────────────────────────────────────────────────

    async def list_projects(
        self,
        organization_id: int,
        status: str = "active",
        page_start_id: Optional[int] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/projects."""
        params: Dict[str, Any] = {"page_limit": page_limit, "status": status}
        if page_start_id is not None:
            params["page_start_id"] = page_start_id
        return await self._request(
            "GET",
            f"/organizations/{organization_id}/projects",
            params=params,
            context="list_projects",
        )

    async def get_project(self, project_id: int) -> Dict[str, Any]:
        """GET /projects/{id}."""
        return await self._request(
            "GET",
            f"/projects/{project_id}",
            context=f"get_project({project_id})",
        )

    async def create_project(
        self,
        organization_id: int,
        name: str,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /organizations/{id}/projects."""
        project: Dict[str, Any] = {"name": name}
        if description is not None:
            project["description"] = description
        return await self._request(
            "POST",
            f"/organizations/{organization_id}/projects",
            json_body={"project": project},
            context="create_project",
        )

    # ── Tasks ──────────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        project_id: int,
        status: str = "open",
        page_start_id: Optional[int] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /projects/{id}/tasks."""
        params: Dict[str, Any] = {"page_limit": page_limit, "status": status}
        if page_start_id is not None:
            params["page_start_id"] = page_start_id
        return await self._request(
            "GET",
            f"/projects/{project_id}/tasks",
            params=params,
            context="list_tasks",
        )

    # ── Activities + Time Entries ─────────────────────────────────────────

    async def list_activities(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        project_ids: Optional[List[int]] = None,
        page_start_id: Optional[int] = None,
        page_limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/activities."""
        params: Dict[str, Any] = {"page_limit": page_limit}
        if date_start:
            params["date_start"] = date_start
        if date_stop:
            params["date_stop"] = date_stop
        csv_u = self._csv(user_ids)
        if csv_u:
            params["user_ids"] = csv_u
        csv_p = self._csv(project_ids)
        if csv_p:
            params["project_ids"] = csv_p
        if page_start_id is not None:
            params["page_start_id"] = page_start_id
        return await self._request(
            "GET",
            f"/organizations/{organization_id}/activities",
            params=params,
            context="list_activities",
        )

    async def list_time_entries(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        project_ids: Optional[List[int]] = None,
        page_start_id: Optional[int] = None,
        page_limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/time_entries."""
        params: Dict[str, Any] = {"page_limit": page_limit}
        if date_start:
            params["date_start"] = date_start
        if date_stop:
            params["date_stop"] = date_stop
        csv_u = self._csv(user_ids)
        if csv_u:
            params["user_ids"] = csv_u
        csv_p = self._csv(project_ids)
        if csv_p:
            params["project_ids"] = csv_p
        if page_start_id is not None:
            params["page_start_id"] = page_start_id
        return await self._request(
            "GET",
            f"/organizations/{organization_id}/time_entries",
            params=params,
            context="list_time_entries",
        )

    async def list_daily_activities(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        project_ids: Optional[List[int]] = None,
        page_start_id: Optional[int] = None,
        page_limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/activities/daily."""
        params: Dict[str, Any] = {"page_limit": page_limit}
        if date_start:
            params["date_start"] = date_start
        if date_stop:
            params["date_stop"] = date_stop
        csv_u = self._csv(user_ids)
        if csv_u:
            params["user_ids"] = csv_u
        csv_p = self._csv(project_ids)
        if csv_p:
            params["project_ids"] = csv_p
        if page_start_id is not None:
            params["page_start_id"] = page_start_id
        return await self._request(
            "GET",
            f"/organizations/{organization_id}/activities/daily",
            params=params,
            context="list_daily_activities",
        )

    # ── Screenshots ────────────────────────────────────────────────────────

    async def list_screenshots(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        project_ids: Optional[List[int]] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/screenshots."""
        params: Dict[str, Any] = {"page_limit": page_limit}
        if date_start:
            params["date_start"] = date_start
        if date_stop:
            params["date_stop"] = date_stop
        csv_u = self._csv(user_ids)
        if csv_u:
            params["user_ids"] = csv_u
        csv_p = self._csv(project_ids)
        if csv_p:
            params["project_ids"] = csv_p
        return await self._request(
            "GET",
            f"/organizations/{organization_id}/screenshots",
            params=params,
            context="list_screenshots",
        )

    # ── Apps + URLs + Notes ───────────────────────────────────────────────

    async def list_apps(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/application_activities."""
        params: Dict[str, Any] = {"page_limit": page_limit}
        if date_start:
            params["date_start"] = date_start
        if date_stop:
            params["date_stop"] = date_stop
        csv_u = self._csv(user_ids)
        if csv_u:
            params["user_ids"] = csv_u
        return await self._request(
            "GET",
            f"/organizations/{organization_id}/application_activities",
            params=params,
            context="list_apps",
        )

    async def list_urls(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/url_activities."""
        params: Dict[str, Any] = {"page_limit": page_limit}
        if date_start:
            params["date_start"] = date_start
        if date_stop:
            params["date_stop"] = date_stop
        csv_u = self._csv(user_ids)
        if csv_u:
            params["user_ids"] = csv_u
        return await self._request(
            "GET",
            f"/organizations/{organization_id}/url_activities",
            params=params,
            context="list_urls",
        )

    async def list_notes(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/notes."""
        params: Dict[str, Any] = {"page_limit": page_limit}
        if date_start:
            params["date_start"] = date_start
        if date_stop:
            params["date_stop"] = date_stop
        csv_u = self._csv(user_ids)
        if csv_u:
            params["user_ids"] = csv_u
        return await self._request(
            "GET",
            f"/organizations/{organization_id}/notes",
            params=params,
            context="list_notes",
        )
