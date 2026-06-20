from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    AsanaAuthError,
    AsanaError,
    AsanaNetworkError,
    AsanaNotFoundError,
    AsanaRateLimitError,
)

BASE_URL: str = "https://app.asana.com/api/1.0"
DEFAULT_TIMEOUT_S: float = 30.0


class AsanaHTTPClient:
    """Low-level async HTTP client for the Asana REST API v1.

    Authentication uses Bearer token (Personal Access Token) sent in the
    Authorization header, per Asana's specification.

    Asana wraps all successful responses in ``{"data": ...}``; this client
    unwraps that envelope automatically before returning.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    def _extract_error_msg(self, body_raw: dict[str, Any], status: int) -> str:
        errors = body_raw.get("errors", [])
        return (errors[0].get("message", "") if errors else "") or f"HTTP {status}"

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse, path: str
    ) -> None:
        """Map HTTP error codes to typed AsanaError subclasses."""
        if response.status in (200, 201, 204):
            return

        body_raw: dict[str, Any] = {}
        try:
            body_raw = await response.json(content_type=None)
        except Exception:
            pass

        err_msg = self._extract_error_msg(body_raw, response.status)

        if response.status in (401, 403):
            raise AsanaAuthError(
                f"Authentication failed: {err_msg}",
                status_code=response.status,
                code="auth_error",
            )
        if response.status == 404:
            raise AsanaNotFoundError("resource", path)
        if response.status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise AsanaRateLimitError(f"Rate limited: {err_msg}", retry_after=retry_after)
        if response.status >= 500:
            raise AsanaNetworkError(
                f"Asana server error {response.status}: {err_msg}",
                status_code=response.status,
            )
        raise AsanaError(
            f"Asana error {response.status}: {err_msg}",
            status_code=response.status,
        )

    async def _request(
        self,
        method: str,
        api_key: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        """Execute a request and unwrap the Asana ``{"data": ...}`` envelope."""
        url = f"{BASE_URL}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(api_key),
                **kwargs,
            ) as response:
                if response.status == 204:
                    return {}
                await self._raise_for_status(response, path)
                body = await response.json(content_type=None)
                # Unwrap Asana's {"data": ...} envelope
                if isinstance(body, dict) and "data" in body:
                    return body["data"]
                return body
        except AsanaError:
            raise
        except aiohttp.ClientConnectorError as exc:
            raise AsanaNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise AsanaNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise AsanaNetworkError(f"Network error: {exc}") from exc

    async def _request_raw(
        self,
        method: str,
        api_key: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a request and return the raw body (preserves next_page for pagination)."""
        url = f"{BASE_URL}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(api_key),
                params=params,
            ) as response:
                await self._raise_for_status(response, path)
                result: dict[str, Any] = await response.json(content_type=None)
                return result
        except AsanaError:
            raise
        except aiohttp.ClientConnectorError as exc:
            raise AsanaNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise AsanaNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise AsanaNetworkError(f"Network error: {exc}") from exc

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_current_user(self, api_key: str) -> dict[str, Any]:
        """GET /users/me?opt_fields=gid,name,email,workspaces"""
        return await self._request(
            "GET", api_key, "/users/me",
            params={"opt_fields": "gid,name,email,workspaces"},
        )

    async def list_users(
        self,
        api_key: str,
        workspace_gid: str,
        offset: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """GET /users?workspace={w}&limit={n}&offset={o}&opt_fields=gid,name,email"""
        params: dict[str, Any] = {
            "workspace": workspace_gid,
            "limit": limit,
            "opt_fields": "gid,name,email",
        }
        if offset:
            params["offset"] = offset
        return await self._request_raw("GET", api_key, "/users", params=params)

    # ── Workspaces ────────────────────────────────────────────────────────────

    async def list_workspaces(
        self,
        api_key: str,
        offset: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """GET /workspaces?limit={n}&offset={o} — list all workspaces.

        Returns the raw response dict with ``data`` list and optional
        ``next_page`` for cursor-based pagination.
        """
        params: dict[str, Any] = {"limit": limit}
        if offset:
            params["offset"] = offset
        return await self._request_raw("GET", api_key, "/workspaces", params=params)

    # ── Projects ──────────────────────────────────────────────────────────────

    async def list_projects(
        self,
        api_key: str,
        workspace_gid: str,
        archived: bool = False,
        offset: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """GET /projects?workspace={w}&archived={a}&limit={n}&offset={o}&opt_fields=..."""
        params: dict[str, Any] = {
            "workspace": workspace_gid,
            "archived": str(archived).lower(),
            "limit": limit,
            "opt_fields": "gid,name,color,created_at,due_date,owner",
        }
        if offset:
            params["offset"] = offset
        return await self._request_raw("GET", api_key, "/projects", params=params)

    async def get_project(self, api_key: str, project_gid: str) -> dict[str, Any]:
        """GET /projects/{gid}?opt_fields=gid,name,notes,status,owner,team,created_at"""
        return await self._request(
            "GET", api_key, f"/projects/{project_gid}",
            params={"opt_fields": "gid,name,notes,status,owner,team,created_at"},
        )

    # ── Tasks ─────────────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        api_key: str,
        project_gid: str,
        offset: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """GET /tasks?project={p}&limit={n}&offset={o}&opt_fields=..."""
        params: dict[str, Any] = {
            "project": project_gid,
            "limit": limit,
            "opt_fields": "gid,name,completed,due_on,assignee,created_at,notes",
        }
        if offset:
            params["offset"] = offset
        return await self._request_raw("GET", api_key, "/tasks", params=params)

    async def get_task(self, api_key: str, task_gid: str) -> dict[str, Any]:
        """GET /tasks/{gid}?opt_fields=gid,name,completed,due_on,assignee,notes,tags,parent"""
        return await self._request(
            "GET", api_key, f"/tasks/{task_gid}",
            params={"opt_fields": "gid,name,completed,due_on,assignee,notes,tags,parent"},
        )

    # ── Sections ─────────────────────────────────────────────────────────────

    async def list_sections(self, api_key: str, project_gid: str) -> list[dict[str, Any]]:
        """GET /projects/{gid}/sections?opt_fields=gid,name"""
        result = await self._request(
            "GET", api_key, f"/projects/{project_gid}/sections",
            params={"opt_fields": "gid,name"},
        )
        if isinstance(result, list):
            return result
        return []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> AsanaHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
