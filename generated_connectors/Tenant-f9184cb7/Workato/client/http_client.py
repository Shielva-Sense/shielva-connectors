"""All Workato API HTTP calls — zero business logic, zero normalization.

httpx async client. The Workato REST API expects:
  Authorization: Bearer <api_token>
  Content-Type:  application/json
  Accept:        application/json

Retry on 429/5xx with exponential backoff.
"""
import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    WorkatoAuthError,
    WorkatoError,
    WorkatoNetworkError,
    WorkatoNotFound,
)

logger = structlog.get_logger(__name__)

_WORKATO_BASE_US = "https://www.workato.com/api"
_WORKATO_BASE_EU = "https://app.eu.workato.com/api"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


def _region_to_base_url(region: str) -> str:
    r = (region or "").strip().lower()
    if r == "eu":
        return _WORKATO_BASE_EU
    return _WORKATO_BASE_US


class WorkatoHTTPClient:
    """Thin async HTTP client for the Workato REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        api_token: str = "",
        region: str = "us",
        base_url: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._api_token = api_token or ""
        self._region = region or "us"
        self._base_url = (base_url or _region_to_base_url(self._region)).rstrip("/")
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
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
        if status == 401 or status == 403:
            raise WorkatoAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 404:
            raise WorkatoNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        raise WorkatoError(
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
                            "workato.http.retry",
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
                        "workato.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise WorkatoNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise WorkatoNetworkError(str(last_exc)) from last_exc
        raise WorkatoNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Identity / health probe ────────────────────────────────────────────

    async def get_me(self) -> Dict[str, Any]:
        """GET /users/me — current workspace user (auth probe)."""
        return await self._request("GET", "/users/me", context="get_me")

    # ── Recipes ────────────────────────────────────────────────────────────

    async def list_recipes(
        self,
        page: int = 1,
        per_page: int = 100,
        folder_id: Optional[int] = None,
        order: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /recipes?page=&per_page=&folder_id=&order=."""
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if folder_id is not None:
            params["folder_id"] = folder_id
        if order is not None:
            params["order"] = order
        return await self._request("GET", "/recipes", params=params, context="list_recipes")

    async def get_recipe(self, recipe_id: int) -> Dict[str, Any]:
        """GET /recipes/{id}."""
        return await self._request(
            "GET",
            f"/recipes/{recipe_id}",
            context=f"get_recipe({recipe_id})",
        )

    async def start_recipe(self, recipe_id: int) -> Dict[str, Any]:
        """PUT /recipes/{id}/start."""
        return await self._request(
            "PUT",
            f"/recipes/{recipe_id}/start",
            context=f"start_recipe({recipe_id})",
        )

    async def stop_recipe(self, recipe_id: int) -> Dict[str, Any]:
        """PUT /recipes/{id}/stop."""
        return await self._request(
            "PUT",
            f"/recipes/{recipe_id}/stop",
            context=f"stop_recipe({recipe_id})",
        )

    # ── Connections ────────────────────────────────────────────────────────

    async def list_connections(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /connections?page=&per_page=."""
        params = {"page": page, "per_page": per_page}
        return await self._request(
            "GET",
            "/connections",
            params=params,
            context="list_connections",
        )

    async def get_connection(self, connection_id: int) -> Dict[str, Any]:
        """GET /connections/{id}."""
        return await self._request(
            "GET",
            f"/connections/{connection_id}",
            context=f"get_connection({connection_id})",
        )

    async def create_connection(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /connections."""
        return await self._request(
            "POST",
            "/connections",
            json_body=payload,
            context="create_connection",
        )

    # ── Folders ────────────────────────────────────────────────────────────

    async def list_folders(
        self,
        page: int = 1,
        per_page: int = 100,
        parent_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """GET /folders?page=&per_page=&parent_id=."""
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if parent_id is not None:
            params["parent_id"] = parent_id
        return await self._request(
            "GET",
            "/folders",
            params=params,
            context="list_folders",
        )

    # ── Jobs (recipe-scoped) ───────────────────────────────────────────────

    async def list_jobs(
        self,
        recipe_id: int,
        page: int = 1,
        per_page: int = 100,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /recipes/{id}/jobs."""
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if status is not None:
            params["status"] = status
        return await self._request(
            "GET",
            f"/recipes/{recipe_id}/jobs",
            params=params,
            context=f"list_jobs({recipe_id})",
        )

    async def get_job(self, recipe_id: int, job_id: int) -> Dict[str, Any]:
        """GET /recipes/{id}/jobs/{job_id}."""
        return await self._request(
            "GET",
            f"/recipes/{recipe_id}/jobs/{job_id}",
            context=f"get_job({recipe_id},{job_id})",
        )

    # ── Lookup tables / tags / users / OPA / customers ─────────────────────

    async def list_lookup_tables(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /lookup_tables."""
        params = {"page": page, "per_page": per_page}
        return await self._request(
            "GET",
            "/lookup_tables",
            params=params,
            context="list_lookup_tables",
        )

    async def list_tags(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /tags."""
        params = {"page": page, "per_page": per_page}
        return await self._request(
            "GET",
            "/tags",
            params=params,
            context="list_tags",
        )

    async def list_users(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /users."""
        params = {"page": page, "per_page": per_page}
        return await self._request(
            "GET",
            "/users",
            params=params,
            context="list_users",
        )

    async def list_on_prem_agents(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /on_prem_agents."""
        params = {"page": page, "per_page": per_page}
        return await self._request(
            "GET",
            "/on_prem_agents",
            params=params,
            context="list_on_prem_agents",
        )

    async def list_customers(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /managed_users — embedded / white-label customer accounts."""
        params = {"page": page, "per_page": per_page}
        return await self._request(
            "GET",
            "/managed_users",
            params=params,
            context="list_customers",
        )
