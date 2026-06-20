from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    ServiceNowAuthError,
    ServiceNowError,
    ServiceNowNetworkError,
    ServiceNowNotFoundError,
    ServiceNowRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
SERVICENOW_TABLE_API: str = "/api/now/table"


def _build_base_url(instance: str) -> str:
    return f"https://{instance}.service-now.com"


class ServiceNowHTTPClient:
    """Low-level async HTTP client for the ServiceNow Table REST API.

    Uses HTTP Basic Auth (username + password) with Accept: application/json.
    ServiceNow returns {"result": [...]} for list endpoints and
    {"result": {...}} for single-record endpoints.
    API base: https://{instance}.service-now.com/api/now/
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(
        self,
        method: str,
        url: str,
        auth: aiohttp.BasicAuth,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, auth=auth, params=params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise ServiceNowNetworkError(f"Network error: {exc}") from exc
        except (
            ServiceNowError,
            ServiceNowAuthError,
            ServiceNowRateLimitError,
            ServiceNowNotFoundError,
            ServiceNowNetworkError,
        ):
            raise
        except Exception as exc:
            raise ServiceNowNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status == 200:
            return await response.json()

        # Attempt to read error body
        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        error_detail: dict[str, Any] = body.get("error", {})
        err_msg: str = (
            error_detail.get("detail", "")
            or error_detail.get("message", "")
            or body.get("message", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise ServiceNowAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise ServiceNowNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise ServiceNowRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise ServiceNowNetworkError(
                f"ServiceNow server error {status}: {err_msg}",
                status_code=status,
            )
        raise ServiceNowError(
            f"ServiceNow error {status}: {err_msg}", status_code=status
        )

    def _make_auth(self, username: str, password: str) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(username, password)

    # ── Health / validation ──────────────────────────────────────────────────

    async def get_current_user(
        self, instance: str, username: str, password: str
    ) -> dict[str, Any]:
        """GET /api/now/table/sys_user?sysparm_query=user_name={username}&sysparm_limit=1 — verify credentials."""
        url = f"{_build_base_url(instance)}{SERVICENOW_TABLE_API}/sys_user"
        auth = self._make_auth(username, password)
        params: dict[str, Any] = {
            "sysparm_query": f"user_name={username}",
            "sysparm_limit": 1,
        }
        return await self._request("GET", url, auth, params=params)

    # ── Incidents ────────────────────────────────────────────────────────────

    async def list_incidents(
        self,
        instance: str,
        username: str,
        password: str,
        limit: int = 100,
        offset: int = 0,
        query: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/now/table/incident — paginated incident listing.

        Uses sysparm_fields to return only the most relevant columns.
        """
        url = f"{_build_base_url(instance)}{SERVICENOW_TABLE_API}/incident"
        auth = self._make_auth(username, password)
        params: dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_offset": offset,
            "sysparm_fields": (
                "sys_id,number,short_description,state,priority,"
                "assigned_to,sys_created_on,resolved_at"
            ),
        }
        if query:
            params["sysparm_query"] = query
        return await self._request("GET", url, auth, params=params)

    async def get_incident(
        self, instance: str, username: str, password: str, sys_id: str
    ) -> dict[str, Any]:
        """GET /api/now/table/incident/{sys_id} — fetch a single incident."""
        url = f"{_build_base_url(instance)}{SERVICENOW_TABLE_API}/incident/{sys_id}"
        auth = self._make_auth(username, password)
        return await self._request("GET", url, auth)

    # ── Problems ─────────────────────────────────────────────────────────────

    async def list_problems(
        self,
        instance: str,
        username: str,
        password: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /api/now/table/problem — paginated problem listing."""
        url = f"{_build_base_url(instance)}{SERVICENOW_TABLE_API}/problem"
        auth = self._make_auth(username, password)
        params: dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_offset": offset,
        }
        return await self._request("GET", url, auth, params=params)

    # ── Change requests ──────────────────────────────────────────────────────

    async def list_changes(
        self,
        instance: str,
        username: str,
        password: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /api/now/table/change_request — paginated change request listing."""
        url = f"{_build_base_url(instance)}{SERVICENOW_TABLE_API}/change_request"
        auth = self._make_auth(username, password)
        params: dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_offset": offset,
        }
        return await self._request("GET", url, auth, params=params)

    async def get_change(
        self, instance: str, username: str, password: str, sys_id: str
    ) -> dict[str, Any]:
        """GET /api/now/table/change_request/{sys_id} — fetch a single change request."""
        url = f"{_build_base_url(instance)}{SERVICENOW_TABLE_API}/change_request/{sys_id}"
        auth = self._make_auth(username, password)
        return await self._request("GET", url, auth)

    # ── Service Catalog ──────────────────────────────────────────────────────

    async def list_service_catalog_items(
        self,
        instance: str,
        username: str,
        password: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /api/now/table/sc_cat_item — paginated service catalog item listing."""
        url = f"{_build_base_url(instance)}{SERVICENOW_TABLE_API}/sc_cat_item"
        auth = self._make_auth(username, password)
        params: dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_offset": offset,
            "sysparm_fields": "sys_id,name,short_description,category",
        }
        return await self._request("GET", url, auth, params=params)

    # ── Users ────────────────────────────────────────────────────────────────

    async def list_users(
        self,
        instance: str,
        username: str,
        password: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /api/now/table/sys_user — paginated user listing."""
        url = f"{_build_base_url(instance)}{SERVICENOW_TABLE_API}/sys_user"
        auth = self._make_auth(username, password)
        params: dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_offset": offset,
            "sysparm_fields": "sys_id,user_name,name,email,title,department",
        }
        return await self._request("GET", url, auth, params=params)

    # ── CMDB ─────────────────────────────────────────────────────────────────

    async def list_cmdb_items(
        self,
        instance: str,
        username: str,
        password: str,
        class_name: str = "cmdb_ci",
        limit: int = 100,
    ) -> dict[str, Any]:
        """GET /api/now/table/{class_name} — list CMDB configuration items."""
        url = f"{_build_base_url(instance)}{SERVICENOW_TABLE_API}/{class_name}"
        auth = self._make_auth(username, password)
        params: dict[str, Any] = {"sysparm_limit": limit}
        return await self._request("GET", url, auth, params=params)
