"""All Clockify API HTTP calls — zero business logic, zero normalization.

Uses httpx (async). Two base URLs are tracked: the standard API base and the
Reports API base (Clockify's analytics endpoints live on a different host).
Retries are layered by the caller via helpers/utils.with_retry().
"""
import asyncio
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    ClockifyAuthError,
    ClockifyError,
    ClockifyNetworkError,
    ClockifyNotFound,
    ClockifyRateLimitError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_API_BASE = "https://api.clockify.me/api/v1"
_DEFAULT_REPORTS_BASE = "https://reports.api.clockify.me/v1"

# Built-in retry counters (the connector also wraps calls in with_retry; this
# layer handles transient 429/5xx that occur even before the caller sees them).
_INNER_RETRIES = 2
_INNER_BASE_DELAY = 0.5


class ClockifyHTTPClient:
    """Thin async httpx client for the Clockify REST API.

    All methods accept an *api_key* and return raw response dicts.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = _DEFAULT_API_BASE,
        reports_base_url: str = _DEFAULT_REPORTS_BASE,
        timeout: float = 30.0,
    ):
        self._api_key = api_key
        self._base_url = (base_url or _DEFAULT_API_BASE).rstrip("/")
        self._reports_base_url = (reports_base_url or _DEFAULT_REPORTS_BASE).rstrip("/")
        self._timeout = timeout

    # ── Public accessors ───────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def reports_base_url(self) -> str:
        return self._reports_base_url

    def set_api_key(self, api_key: str) -> None:
        self._api_key = api_key

    # ── Internal ──────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Api-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}
        message = ""
        if isinstance(body, dict):
            message = body.get("message") or body.get("error") or str(body)
        else:
            message = str(body)
        ctx = f": {context}" if context else ""

        if status == 401:
            raise ClockifyAuthError(
                f"401 Unauthorized{ctx}: {message}",
                status_code=401,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 403:
            raise ClockifyAuthError(
                f"403 Forbidden{ctx}: {message}",
                status_code=403,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 404:
            raise ClockifyNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 429:
            raise ClockifyRateLimitError(
                f"429 Rate Limit{ctx}: {message}",
                status_code=429,
                response_body=body if isinstance(body, dict) else {},
            )
        raise ClockifyError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {},
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        """Execute one HTTP request with a small inner retry loop on 429/5xx."""
        last_exc: Optional[Exception] = None
        for attempt in range(_INNER_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        json=json,
                        headers=self._headers(),
                    )
            except httpx.HTTPError as exc:
                last_exc = ClockifyNetworkError(
                    f"network error{': ' + context if context else ''}: {exc}"
                )
                if attempt == _INNER_RETRIES:
                    raise last_exc
                await asyncio.sleep(
                    _INNER_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.25)
                )
                continue

            # Retry on 429 / 5xx
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < _INNER_RETRIES:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else (
                            _INNER_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.25)
                        )
                    except ValueError:
                        delay = _INNER_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.25)
                    logger.warning(
                        "clockify.transient_retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                # last attempt: fall through to raise
            self._raise_for_status(response, context=context)
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw": response.text}
        # Should be unreachable
        if last_exc:
            raise last_exc
        raise ClockifyError("request exhausted retries", status_code=0)

    # ── Identity ──────────────────────────────────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /user — authenticated user profile (used for health_check too)."""
        return await self._request("GET", f"{self._base_url}/user", context="get_current_user")

    # ── Workspaces ────────────────────────────────────────────────────────

    async def list_workspaces(self) -> Any:
        return await self._request(
            "GET", f"{self._base_url}/workspaces", context="list_workspaces"
        )

    # ── Projects ──────────────────────────────────────────────────────────

    async def list_projects(
        self,
        workspace_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self._base_url}/workspaces/{workspace_id}/projects"
        return await self._request("GET", url, params=params, context="list_projects")

    async def get_project(self, workspace_id: str, project_id: str) -> Dict[str, Any]:
        url = f"{self._base_url}/workspaces/{workspace_id}/projects/{project_id}"
        return await self._request("GET", url, context="get_project")

    async def create_project(
        self,
        workspace_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/workspaces/{workspace_id}/projects"
        return await self._request("POST", url, json=payload, context="create_project")

    # ── Clients ───────────────────────────────────────────────────────────

    async def list_clients(
        self,
        workspace_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self._base_url}/workspaces/{workspace_id}/clients"
        return await self._request("GET", url, params=params, context="list_clients")

    async def create_client(
        self,
        workspace_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/workspaces/{workspace_id}/clients"
        return await self._request("POST", url, json=payload, context="create_client")

    # ── Tags ──────────────────────────────────────────────────────────────

    async def list_tags(
        self,
        workspace_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self._base_url}/workspaces/{workspace_id}/tags"
        return await self._request("GET", url, params=params, context="list_tags")

    # ── Tasks ─────────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        workspace_id: str,
        project_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = (
            f"{self._base_url}/workspaces/{workspace_id}/projects/{project_id}/tasks"
        )
        return await self._request("GET", url, params=params, context="list_tasks")

    # ── Time entries ──────────────────────────────────────────────────────

    async def list_time_entries(
        self,
        workspace_id: str,
        user_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self._base_url}/workspaces/{workspace_id}/user/{user_id}/time-entries"
        return await self._request("GET", url, params=params, context="list_time_entries")

    async def get_time_entry(
        self,
        workspace_id: str,
        entry_id: str,
    ) -> Dict[str, Any]:
        """GET /workspaces/{wid}/time-entries/{eid}."""
        url = f"{self._base_url}/workspaces/{workspace_id}/time-entries/{entry_id}"
        return await self._request("GET", url, context="get_time_entry")

    async def create_time_entry(
        self,
        workspace_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/workspaces/{workspace_id}/time-entries"
        return await self._request("POST", url, json=payload, context="create_time_entry")

    async def stop_time_entry(
        self,
        workspace_id: str,
        user_id: str,
        end: str,
    ) -> Dict[str, Any]:
        """PATCH /workspaces/{wid}/user/{uid}/time-entries — stop the running timer."""
        url = (
            f"{self._base_url}/workspaces/{workspace_id}/user/{user_id}/time-entries"
        )
        return await self._request(
            "PATCH", url, json={"end": end}, context="stop_time_entry"
        )

    async def list_users(
        self,
        workspace_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """GET /workspaces/{wid}/users — list workspace members."""
        url = f"{self._base_url}/workspaces/{workspace_id}/users"
        return await self._request("GET", url, params=params, context="list_users")

    async def update_time_entry(
        self,
        workspace_id: str,
        entry_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/workspaces/{workspace_id}/time-entries/{entry_id}"
        return await self._request("PUT", url, json=payload, context="update_time_entry")

    async def delete_time_entry(
        self,
        workspace_id: str,
        entry_id: str,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/workspaces/{workspace_id}/time-entries/{entry_id}"
        return await self._request("DELETE", url, context="delete_time_entry")

    # ── Reports (different base URL) ──────────────────────────────────────

    async def summary_report(
        self,
        workspace_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST {reports_base}/workspaces/{id}/reports/summary."""
        url = f"{self._reports_base_url}/workspaces/{workspace_id}/reports/summary"
        return await self._request("POST", url, json=payload, context="summary_report")
