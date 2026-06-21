"""All Grafana API HTTP calls — zero business logic, zero normalization.

Uses httpx.AsyncClient with Bearer (Service Account token) auth.
Retries on 429 and 5xx with exponential backoff; honors Retry-After when present.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    GrafanaAPIError,
    GrafanaAuthError,
    GrafanaNetworkError,
    GrafanaNotFound,
    GrafanaRateLimitError,
)

logger = structlog.get_logger(__name__)

# OCP: retry constants — change here, nowhere else
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_MAX_RETRIES: int = 3
RETRY_BASE_DELAY_S: float = 1.0
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_MAX_DELAY_S: float = 32.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GrafanaHTTPClient:
    """Thin async HTTP client for the Grafana REST API.

    Construction takes the tenant-specific *base_url* (e.g. https://myorg.grafana.net)
    and the Service Account *token*. All methods return raw response dicts/lists.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self._base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self._timeout_s = timeout_s
        self._max_retries = max_retries

    # ── Internal plumbing ──────────────────────────────────────────────────

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _full_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}{path if path.startswith('/') else '/' + path}"

    @staticmethod
    def _retry_after(resp: httpx.Response) -> Optional[float]:
        ra = resp.headers.get("Retry-After")
        if not ra:
            return None
        try:
            return float(ra)
        except ValueError:
            return None

    async def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            message = body.get("message") or body.get("error") or str(body)
        else:
            message = str(body)

        if status == 401:
            raise GrafanaAuthError(f"401 Unauthorized: {context}: {message}")
        if status == 404:
            raise GrafanaNotFound(f"404 Not Found: {context}: {message}")
        if status == 429:
            raise GrafanaRateLimitError(f"429 Rate limit exceeded: {context}: {message}")
        raise GrafanaAPIError(
            f"HTTP {status}: {context}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {"body": body},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str,
    ) -> Any:
        """Perform an HTTP request with retry on 429/5xx."""
        url = self._full_url(path)
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                        json=json_body,
                    )

                if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                    retry_after = self._retry_after(response)
                    delay = retry_after if retry_after is not None else min(
                        RETRY_BASE_DELAY_S * (RETRY_BACKOFF_FACTOR ** attempt)
                        + random.uniform(0, 0.5),
                        RETRY_MAX_DELAY_S,
                    )
                    logger.warning(
                        "grafana.retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue

                await self._raise_for_status(response, context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}

            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                delay = min(
                    RETRY_BASE_DELAY_S * (RETRY_BACKOFF_FACTOR ** attempt)
                    + random.uniform(0, 0.5),
                    RETRY_MAX_DELAY_S,
                )
                logger.warning(
                    "grafana.network_retry",
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(exc),
                    context=context,
                )
                await asyncio.sleep(delay)

        if last_exc is not None:
            raise GrafanaNetworkError(f"Network error after {self._max_retries + 1} attempts: {last_exc}") from last_exc
        # If we fell through the retry loop on RETRYABLE_STATUS without breaking,
        # one last attempt is made above. If even that fails, surface the error.
        raise GrafanaAPIError(f"Exhausted retries: {context}", status_code=0)

    # ── Public endpoints ───────────────────────────────────────────────────

    async def get_health(self) -> Dict[str, Any]:
        """GET /api/health — server status."""
        return await self._request("GET", "/api/health", context="get_health")

    async def get_org(self) -> Dict[str, Any]:
        """GET /api/org — current organization for the auth token."""
        return await self._request("GET", "/api/org", context="get_org")

    async def search_dashboards(
        self,
        limit: int = 100,
        page: int = 1,
        query: Optional[str] = None,
        tag: Optional[List[str]] = None,
        folder_uids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """GET /api/search?type=dash-db — list dashboards."""
        params: Dict[str, Any] = {"type": "dash-db", "limit": limit, "page": page}
        if query:
            params["query"] = query
        if tag:
            params["tag"] = tag
        if folder_uids:
            params["folderUIDs"] = folder_uids
        result = await self._request("GET", "/api/search", params=params, context="search_dashboards")
        return result if isinstance(result, list) else []

    async def get_dashboard(self, uid: str) -> Dict[str, Any]:
        """GET /api/dashboards/uid/{uid} — fetch one dashboard."""
        return await self._request(
            "GET", f"/api/dashboards/uid/{uid}", context=f"get_dashboard({uid})"
        )

    async def post_dashboard(
        self,
        dashboard: Dict[str, Any],
        folder_uid: Optional[str] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """POST /api/dashboards/db — create/update a dashboard."""
        body: Dict[str, Any] = {"dashboard": dashboard, "overwrite": overwrite}
        if folder_uid is not None:
            body["folderUid"] = folder_uid
        return await self._request(
            "POST", "/api/dashboards/db", json_body=body, context="post_dashboard"
        )

    async def delete_dashboard(self, uid: str) -> Dict[str, Any]:
        """DELETE /api/dashboards/uid/{uid}."""
        return await self._request(
            "DELETE", f"/api/dashboards/uid/{uid}", context=f"delete_dashboard({uid})"
        )

    async def list_folders(self, limit: int = 100, page: int = 1) -> List[Dict[str, Any]]:
        """GET /api/folders — list folders."""
        params: Dict[str, Any] = {"limit": limit, "page": page}
        result = await self._request("GET", "/api/folders", params=params, context="list_folders")
        return result if isinstance(result, list) else []

    async def create_folder(self, title: str, uid: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/folders — create a folder."""
        body: Dict[str, Any] = {"title": title}
        if uid:
            body["uid"] = uid
        return await self._request(
            "POST", "/api/folders", json_body=body, context="create_folder"
        )

    async def list_datasources(self) -> List[Dict[str, Any]]:
        """GET /api/datasources — list configured datasources."""
        result = await self._request("GET", "/api/datasources", context="list_datasources")
        return result if isinstance(result, list) else []

    async def create_datasource(
        self,
        name: str,
        type_: str,
        url: str,
        access: str = "proxy",
        is_default: bool = False,
        basic_auth: bool = False,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /api/datasources — register a new datasource."""
        body: Dict[str, Any] = {
            "name": name,
            "type": type_,
            "url": url,
            "access": access,
            "isDefault": is_default,
            "basicAuth": basic_auth,
        }
        if json_data is not None:
            body["jsonData"] = json_data
        return await self._request(
            "POST", "/api/datasources", json_body=body, context="create_datasource"
        )

    async def query_datasource(
        self,
        datasource_id: int,
        queries: List[Dict[str, Any]],
        from_time: Optional[int] = None,
        to_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /api/ds/query — run datasource queries."""
        # Each query must carry the datasource id.
        enriched_queries: List[Dict[str, Any]] = []
        for q in queries:
            q2 = dict(q)
            q2.setdefault("datasourceId", datasource_id)
            q2.setdefault("refId", q2.get("refId", "A"))
            enriched_queries.append(q2)

        body: Dict[str, Any] = {"queries": enriched_queries}
        if from_time is not None:
            body["from"] = str(from_time)
        if to_time is not None:
            body["to"] = str(to_time)
        return await self._request(
            "POST", "/api/ds/query", json_body=body, context="query_datasource"
        )

    async def list_alert_rules(self, limit: int = 100) -> List[Dict[str, Any]]:
        """GET /api/v1/provisioning/alert-rules — list provisioned alert rules."""
        params: Dict[str, Any] = {"limit": limit}
        result = await self._request(
            "GET", "/api/v1/provisioning/alert-rules", params=params, context="list_alert_rules"
        )
        return result if isinstance(result, list) else []

    async def list_users(self, perpage: int = 1000, page: int = 1) -> List[Dict[str, Any]]:
        """GET /api/users — paginated list of users."""
        params: Dict[str, Any] = {"perpage": perpage, "page": page}
        result = await self._request("GET", "/api/users", params=params, context="list_users")
        return result if isinstance(result, list) else []

    async def search_teams(
        self,
        perpage: int = 1000,
        page: int = 1,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/teams/search — search teams."""
        params: Dict[str, Any] = {"perpage": perpage, "page": page}
        if query:
            params["query"] = query
        return await self._request(
            "GET", "/api/teams/search", params=params, context="search_teams"
        )
