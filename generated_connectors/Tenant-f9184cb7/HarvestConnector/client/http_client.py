"""All Harvest API HTTP calls — zero business logic, zero normalization.

httpx async client. The Harvest REST API expects:
  Authorization:       Bearer <personal_access_token>
  Harvest-Account-Id:  <account_id>
  User-Agent:          <user_agent>          (Harvest REQUIRES this header)
  Content-Type:        application/json

Retry on 429/5xx with exponential backoff.
"""
import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    HarvestAuthError,
    HarvestBadRequestError,
    HarvestError,
    HarvestNetworkError,
    HarvestNotFound,
    HarvestRateLimitError,
    HarvestServerError,
)

logger = structlog.get_logger(__name__)

_HARVEST_BASE = "https://api.harvestapp.com/v2"
_DEFAULT_USER_AGENT = "Shielva Harvest Connector (support@shielva.ai)"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class HarvestHTTPClient:
    """Thin async HTTP client for the Harvest v2 REST API.

    All methods are awaitable and return raw response dicts. Auth + retry
    are owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        access_token: str = "",
        account_id: str = "",
        base_url: str = _HARVEST_BASE,
        user_agent: str = _DEFAULT_USER_AGENT,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._access_token = access_token or ""
        self._account_id = str(account_id or "")
        self._user_agent = user_agent or _DEFAULT_USER_AGENT
        self._base_url = (base_url or _HARVEST_BASE).rstrip("/")
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        if self._account_id:
            headers["Harvest-Account-Id"] = self._account_id
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
                or body.get("error_description")
                or body.get("error")
                or body.get("details")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status in (401, 403):
            raise HarvestAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise HarvestNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status in (400, 422):
            raise HarvestBadRequestError(
                f"{status} Bad Request{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = 5.0
            try:
                retry_after = float(response.headers.get("Retry-After", "5") or 5)
            except (TypeError, ValueError):
                retry_after = 5.0
            raise HarvestRateLimitError(
                f"429 Rate Limited{ctx}: {message}",
                retry_after_s=retry_after,
            )
        if status >= 500:
            raise HarvestServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise HarvestError(
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
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers()

        # Drop None-valued query params — httpx serialises them as empty.
        clean_params: Optional[Dict[str, Any]] = None
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=clean_params,
                        json=json_body,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "harvest.http.retry",
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
                        "harvest.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise HarvestNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise HarvestNetworkError(str(last_exc)) from last_exc
        raise HarvestNetworkError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── Users ──────────────────────────────────────────────────────────────

    async def get_user_me(self) -> Dict[str, Any]:
        """GET /users/me — authenticated user."""
        return await self._request("GET", "/users/me", context="get_user_me")

    async def list_users(
        self,
        is_active: Optional[bool] = True,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /users."""
        return await self._request(
            "GET",
            "/users",
            params={"is_active": is_active, "page": page, "per_page": per_page},
            context="list_users",
        )

    # ── Clients ────────────────────────────────────────────────────────────

    async def list_clients(
        self,
        is_active: Optional[bool] = True,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /clients."""
        return await self._request(
            "GET",
            "/clients",
            params={"is_active": is_active, "page": page, "per_page": per_page},
            context="list_clients",
        )

    # ── Projects ───────────────────────────────────────────────────────────

    async def list_projects(
        self,
        is_active: Optional[bool] = True,
        client_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /projects."""
        return await self._request(
            "GET",
            "/projects",
            params={
                "is_active": is_active,
                "client_id": client_id,
                "page": page,
                "per_page": per_page,
            },
            context="list_projects",
        )

    async def get_project(self, project_id: int) -> Dict[str, Any]:
        """GET /projects/{id}."""
        return await self._request(
            "GET",
            f"/projects/{project_id}",
            context=f"get_project({project_id})",
        )

    # ── Tasks ──────────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        is_active: Optional[bool] = True,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /tasks."""
        return await self._request(
            "GET",
            "/tasks",
            params={"is_active": is_active, "page": page, "per_page": per_page},
            context="list_tasks",
        )

    # ── Time entries ───────────────────────────────────────────────────────

    async def list_time_entries(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        user_id: Optional[int] = None,
        project_id: Optional[int] = None,
        client_id: Optional[int] = None,
        is_billed: Optional[bool] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /time_entries."""
        return await self._request(
            "GET",
            "/time_entries",
            params={
                "from": from_date,
                "to": to_date,
                "user_id": user_id,
                "project_id": project_id,
                "client_id": client_id,
                "is_billed": is_billed,
                "page": page,
                "per_page": per_page,
            },
            context="list_time_entries",
        )

    async def get_time_entry(self, time_entry_id: int) -> Dict[str, Any]:
        """GET /time_entries/{id}."""
        return await self._request(
            "GET",
            f"/time_entries/{time_entry_id}",
            context=f"get_time_entry({time_entry_id})",
        )

    async def create_time_entry(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /time_entries."""
        return await self._request(
            "POST",
            "/time_entries",
            json_body=body,
            context="create_time_entry",
        )

    async def update_time_entry(
        self,
        time_entry_id: int,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /time_entries/{id}."""
        return await self._request(
            "PATCH",
            f"/time_entries/{time_entry_id}",
            json_body=body,
            context=f"update_time_entry({time_entry_id})",
        )

    async def delete_time_entry(self, time_entry_id: int) -> Dict[str, Any]:
        """DELETE /time_entries/{id}."""
        return await self._request(
            "DELETE",
            f"/time_entries/{time_entry_id}",
            context=f"delete_time_entry({time_entry_id})",
        )

    # ── Invoices ───────────────────────────────────────────────────────────

    async def list_invoices(
        self,
        state: Optional[str] = None,
        client_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /invoices."""
        return await self._request(
            "GET",
            "/invoices",
            params={
                "state": state,
                "client_id": client_id,
                "page": page,
                "per_page": per_page,
            },
            context="list_invoices",
        )

    # ── Estimates ──────────────────────────────────────────────────────────

    async def list_estimates(
        self,
        state: Optional[str] = None,
        client_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /estimates."""
        return await self._request(
            "GET",
            "/estimates",
            params={
                "state": state,
                "client_id": client_id,
                "page": page,
                "per_page": per_page,
            },
            context="list_estimates",
        )

    # ── Expenses ───────────────────────────────────────────────────────────

    async def list_expenses(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        user_id: Optional[int] = None,
        project_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /expenses."""
        return await self._request(
            "GET",
            "/expenses",
            params={
                "from": from_date,
                "to": to_date,
                "user_id": user_id,
                "project_id": project_id,
                "page": page,
                "per_page": per_page,
            },
            context="list_expenses",
        )
