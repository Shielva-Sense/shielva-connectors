"""All Toggl Track API HTTP calls — zero business logic, zero normalization.

httpx async client. The Toggl Track REST API v9 expects HTTP Basic auth where
the api_token is the username and the literal string "api_token" is the
password:

    Authorization: Basic base64("<api_token>:api_token")

Retry on 429/5xx with exponential backoff.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    TogglAuthError,
    TogglError,
    TogglNetworkError,
    TogglNotFound,
    TogglRateLimitError,
)

logger = structlog.get_logger(__name__)

_TOGGL_BASE = "https://api.track.toggl.com/api/v9"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class TogglHTTPClient:
    """Thin async HTTP client for the Toggl Track REST API v9.

    All methods are awaitable and return raw response dicts (or lists).
    Auth + retry are owned here — the connector layer only orchestrates
    business calls.
    """

    def __init__(
        self,
        api_token: str = "",
        base_url: str = _TOGGL_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_token = api_token or ""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ── Internals ──────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _auth(self) -> Optional[httpx.BasicAuth]:
        # Toggl quirk: api_token as username, the literal "api_token" as password.
        if not self._api_token:
            return None
        return httpx.BasicAuth(self._api_token, "api_token")

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
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
                or body.get("error_message")
                or body.get("details")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        elif isinstance(body, str):
            message = body
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status in (401, 403):
            raise TogglAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise TogglNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 429:
            raise TogglRateLimitError(
                f"429 Too Many Requests{ctx}: {message}",
                retry_after_s=5.0,
            )
        raise TogglError(
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
        auth = self._auth()

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
                        auth=auth,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "toggl.http.retry",
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
                        "toggl.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise TogglNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise TogglNetworkError(str(last_exc)) from last_exc
        raise TogglNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Me ─────────────────────────────────────────────────────────────────

    async def get_me(self) -> Dict[str, Any]:
        """GET /me — authenticated user profile."""
        return await self._request("GET", "/me", context="get_me")

    # ── Workspaces ─────────────────────────────────────────────────────────

    async def list_workspaces(self) -> Any:
        """GET /workspaces — workspaces the user belongs to."""
        return await self._request("GET", "/workspaces", context="list_workspaces")

    async def get_workspace(self, workspace_id: Any) -> Dict[str, Any]:
        """GET /workspaces/{wid}."""
        return await self._request(
            "GET",
            f"/workspaces/{workspace_id}",
            context=f"get_workspace({workspace_id})",
        )

    # ── Projects ───────────────────────────────────────────────────────────

    async def list_projects(
        self,
        workspace_id: Any,
        *,
        active: Optional[bool] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
    ) -> Any:
        """GET /workspaces/{wid}/projects."""
        params: Dict[str, Any] = {}
        if active is not None:
            params["active"] = "true" if active else "false"
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        return await self._request(
            "GET",
            f"/workspaces/{workspace_id}/projects",
            params=params or None,
            context=f"list_projects({workspace_id})",
        )

    async def get_project(self, workspace_id: Any, project_id: Any) -> Dict[str, Any]:
        """GET /workspaces/{wid}/projects/{pid}."""
        return await self._request(
            "GET",
            f"/workspaces/{workspace_id}/projects/{project_id}",
            context=f"get_project({workspace_id},{project_id})",
        )

    async def create_project(
        self,
        workspace_id: Any,
        project: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /workspaces/{wid}/projects."""
        return await self._request(
            "POST",
            f"/workspaces/{workspace_id}/projects",
            json_body=project,
            context=f"create_project({workspace_id})",
        )

    # ── Time entries ───────────────────────────────────────────────────────

    async def list_time_entries(
        self,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        since: Optional[int] = None,
    ) -> Any:
        """GET /me/time_entries."""
        params: Dict[str, Any] = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if since is not None:
            params["since"] = since
        return await self._request(
            "GET",
            "/me/time_entries",
            params=params or None,
            context="list_time_entries",
        )

    async def get_current_time_entry(self) -> Any:
        """GET /me/time_entries/current — running entry or null."""
        return await self._request(
            "GET",
            "/me/time_entries/current",
            context="get_current_time_entry",
        )

    async def create_time_entry(
        self,
        workspace_id: Any,
        entry: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /workspaces/{wid}/time_entries."""
        return await self._request(
            "POST",
            f"/workspaces/{workspace_id}/time_entries",
            json_body=entry,
            context=f"create_time_entry({workspace_id})",
        )

    async def update_time_entry(
        self,
        workspace_id: Any,
        time_entry_id: Any,
        entry: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /workspaces/{wid}/time_entries/{teid}."""
        return await self._request(
            "PUT",
            f"/workspaces/{workspace_id}/time_entries/{time_entry_id}",
            json_body=entry,
            context=f"update_time_entry({workspace_id},{time_entry_id})",
        )

    async def stop_time_entry(
        self,
        workspace_id: Any,
        time_entry_id: Any,
    ) -> Dict[str, Any]:
        """PATCH /workspaces/{wid}/time_entries/{teid}/stop."""
        return await self._request(
            "PATCH",
            f"/workspaces/{workspace_id}/time_entries/{time_entry_id}/stop",
            context=f"stop_time_entry({workspace_id},{time_entry_id})",
        )

    async def delete_time_entry(
        self,
        workspace_id: Any,
        time_entry_id: Any,
    ) -> Dict[str, Any]:
        """DELETE /workspaces/{wid}/time_entries/{teid}."""
        return await self._request(
            "DELETE",
            f"/workspaces/{workspace_id}/time_entries/{time_entry_id}",
            context=f"delete_time_entry({workspace_id},{time_entry_id})",
        )

    # ── Tags ───────────────────────────────────────────────────────────────

    async def list_tags(self, workspace_id: Any) -> Any:
        """GET /workspaces/{wid}/tags."""
        return await self._request(
            "GET",
            f"/workspaces/{workspace_id}/tags",
            context=f"list_tags({workspace_id})",
        )

    # ── Clients ────────────────────────────────────────────────────────────

    async def list_clients(self, workspace_id: Any) -> Any:
        """GET /workspaces/{wid}/clients."""
        return await self._request(
            "GET",
            f"/workspaces/{workspace_id}/clients",
            context=f"list_clients({workspace_id})",
        )

    async def create_client(
        self,
        workspace_id: Any,
        client: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /workspaces/{wid}/clients."""
        return await self._request(
            "POST",
            f"/workspaces/{workspace_id}/clients",
            json_body=client,
            context=f"create_client({workspace_id})",
        )

    # ── Tasks ──────────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        workspace_id: Any,
        project_id: Any,
    ) -> Any:
        """GET /workspaces/{wid}/projects/{pid}/tasks."""
        return await self._request(
            "GET",
            f"/workspaces/{workspace_id}/projects/{project_id}/tasks",
            context=f"list_tasks({workspace_id},{project_id})",
        )
