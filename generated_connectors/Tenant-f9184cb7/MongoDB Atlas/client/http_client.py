"""All MongoDB Atlas Admin API HTTP calls — zero business logic, zero normalization.

Atlas requires HTTP Digest authentication (RFC 7616) with the (public_key,
private_key) pair issued from Organization → Access Manager → API Keys. The
two-round-trip 401 → digest-challenge → 200 dance is handled transparently by
``httpx.DigestAuth``.

Atlas also REQUIRES a versioned media-type Accept header:
    Accept: application/vnd.atlas.<YYYY-MM-DD>+json

Retry on 429/5xx with exponential backoff (Retry-After honoured).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    MongoDBAtlasAuthError,
    MongoDBAtlasBadRequestError,
    MongoDBAtlasConflictError,
    MongoDBAtlasError,
    MongoDBAtlasNetworkError,
    MongoDBAtlasNotFoundError,
    MongoDBAtlasRateLimitError,
    MongoDBAtlasServerError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://cloud.mongodb.com/api/atlas/v2"
_DEFAULT_API_VERSION = "2025-03-12"
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5
_DEFAULT_TIMEOUT_S = 30.0


class MongoDBAtlasHTTPClient:
    """Thin async HTTP client for the MongoDB Atlas Admin API v2.

    Uses HTTP Digest auth via ``httpx.DigestAuth``. Retries on 429 / 5xx with
    bounded exponential backoff. Raises a typed exception on every 4xx.
    """

    def __init__(
        self,
        public_key: str,
        private_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        api_version: str = _DEFAULT_API_VERSION,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._api_version = api_version or _DEFAULT_API_VERSION
        self._timeout_s = timeout_s
        self._public_key = public_key or ""
        self._private_key = private_key or ""
        # DigestAuth performs the 401 → Authorization: Digest … dance natively.
        self._auth = httpx.DigestAuth(self._public_key, self._private_key)

    # ── headers ─────────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": f"application/vnd.atlas.{self._api_version}+json",
            "Content-Type": "application/json",
        }

    # ── error mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _raise_for_status(response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return

        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"detail": response.text}

        if isinstance(body, dict):
            detail = (
                body.get("detail")
                or body.get("error")
                or body.get("reason")
                or body.get("errorCode")
                or str(body)
            )
        else:
            detail = str(body)
            body = {"raw": body}

        ctx = f": {context}" if context else ""

        if status == 400:
            raise MongoDBAtlasBadRequestError(
                f"HTTP 400{ctx}: {detail}",
                status_code=400,
                response_body=body,
            )
        if status in (401, 403):
            raise MongoDBAtlasAuthError(
                f"HTTP {status}{ctx}: {detail}",
                status_code=status,
                response_body=body,
            )
        if status == 404:
            raise MongoDBAtlasNotFoundError(
                f"HTTP 404{ctx}: {detail}",
                status_code=404,
                response_body=body,
            )
        if status == 409:
            raise MongoDBAtlasConflictError(
                f"HTTP 409{ctx}: {detail}",
                status_code=409,
                response_body=body,
            )
        if status == 429:
            raise MongoDBAtlasRateLimitError(f"HTTP 429{ctx}: {detail}")
        if status >= 500:
            raise MongoDBAtlasServerError(
                f"HTTP {status}{ctx}: {detail}",
                status_code=status,
                response_body=body,
            )
        raise MongoDBAtlasError(
            f"HTTP {status}{ctx}: {detail}",
            status_code=status,
            response_body=body,
        )

    # ── core request with retry ─────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        context: str = "",
    ) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        attempt = 0
        last_exc: Optional[Exception] = None

        while attempt <= _MAX_RETRIES:
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout_s, auth=self._auth
                ) as client:
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=self._headers(),
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt >= _MAX_RETRIES:
                    raise MongoDBAtlasNetworkError(
                        f"transport failure{': ' + context if context else ''}: {exc!s}"
                    ) from exc
                logger.warning(
                    "mongodb_atlas.transport_retry",
                    attempt=attempt + 1,
                    context=context,
                    error=str(exc),
                )
                await asyncio.sleep(_BACKOFF_BASE_S * (2 ** attempt))
                attempt += 1
                continue
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= _MAX_RETRIES:
                    raise MongoDBAtlasNetworkError(
                        f"transport failure{': ' + context if context else ''}: {exc!s}"
                    ) from exc
                await asyncio.sleep(_BACKOFF_BASE_S * (2 ** attempt))
                attempt += 1
                continue

            if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else _BACKOFF_BASE_S * (2 ** attempt)
                except ValueError:
                    delay = _BACKOFF_BASE_S * (2 ** attempt)
                logger.warning(
                    "mongodb_atlas.retry",
                    status=response.status_code,
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            self._raise_for_status(response, context)
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except Exception:
                return {"raw": response.text}

        raise MongoDBAtlasNetworkError(
            f"exhausted retries{': ' + context if context else ''}: {last_exc!s}"
        )

    # ── verb shortcuts ──────────────────────────────────────────────────────

    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request("GET", path, params=params, context=context or path)

    async def post(
        self,
        path: str,
        json_body: Any = None,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request(
            "POST", path, params=params, json_body=json_body, context=context or path
        )

    async def delete(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request("DELETE", path, params=params, context=context or path)

    async def patch(
        self,
        path: str,
        json_body: Any = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request(
            "PATCH", path, json_body=json_body, context=context or path
        )

    # ── high-level helpers used by the connector ────────────────────────────

    async def health_probe(self) -> Dict[str, Any]:
        """GET /orgs?itemsPerPage=1 — minimal Digest credential verification."""
        return await self.get("/orgs", params={"itemsPerPage": 1}, context="health_probe")

    # Organizations
    async def list_organizations(
        self, page_num: int = 1, items_per_page: int = 100
    ) -> Dict[str, Any]:
        return await self.get(
            "/orgs",
            params={"pageNum": page_num, "itemsPerPage": items_per_page},
            context="list_organizations",
        )

    async def get_organization(self, org_id: str) -> Dict[str, Any]:
        return await self.get(
            f"/orgs/{org_id}", context=f"get_organization({org_id})"
        )

    # Projects (Groups)
    async def list_projects(
        self, page_num: int = 1, items_per_page: int = 100
    ) -> Dict[str, Any]:
        return await self.get(
            "/groups",
            params={"pageNum": page_num, "itemsPerPage": items_per_page},
            context="list_projects",
        )

    async def get_project(self, project_id: str) -> Dict[str, Any]:
        return await self.get(
            f"/groups/{project_id}", context=f"get_project({project_id})"
        )

    async def create_project(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self.post("/groups", json_body=body, context="create_project")

    async def delete_project(self, project_id: str) -> Dict[str, Any]:
        return await self.delete(
            f"/groups/{project_id}", context=f"delete_project({project_id})"
        )

    # Clusters
    async def list_clusters(self, project_id: str) -> Dict[str, Any]:
        return await self.get(
            f"/groups/{project_id}/clusters", context=f"list_clusters({project_id})"
        )

    async def get_cluster(self, project_id: str, cluster_name: str) -> Dict[str, Any]:
        return await self.get(
            f"/groups/{project_id}/clusters/{cluster_name}",
            context=f"get_cluster({project_id}/{cluster_name})",
        )

    async def create_cluster(
        self, project_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self.post(
            f"/groups/{project_id}/clusters",
            json_body=body,
            context=f"create_cluster({project_id})",
        )

    async def modify_cluster(
        self, project_id: str, cluster_name: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self.patch(
            f"/groups/{project_id}/clusters/{cluster_name}",
            json_body=body,
            context=f"modify_cluster({project_id}/{cluster_name})",
        )

    async def delete_cluster(
        self, project_id: str, cluster_name: str
    ) -> Dict[str, Any]:
        return await self.delete(
            f"/groups/{project_id}/clusters/{cluster_name}",
            context=f"delete_cluster({project_id}/{cluster_name})",
        )

    # Database users
    async def list_database_users(
        self, project_id: str, items_per_page: int = 100
    ) -> Dict[str, Any]:
        return await self.get(
            f"/groups/{project_id}/databaseUsers",
            params={"itemsPerPage": items_per_page},
            context=f"list_database_users({project_id})",
        )

    async def create_database_user(
        self, project_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self.post(
            f"/groups/{project_id}/databaseUsers",
            json_body=body,
            context=f"create_database_user({project_id})",
        )

    async def delete_database_user(
        self, project_id: str, database_name: str, username: str
    ) -> Dict[str, Any]:
        return await self.delete(
            f"/groups/{project_id}/databaseUsers/{database_name}/{username}",
            context=f"delete_database_user({project_id}/{database_name}/{username})",
        )

    # Network access
    async def list_network_access(self, project_id: str) -> Dict[str, Any]:
        return await self.get(
            f"/groups/{project_id}/accessList",
            context=f"list_network_access({project_id})",
        )

    async def add_network_access(
        self, project_id: str, entries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        return await self.post(
            f"/groups/{project_id}/accessList",
            json_body=entries,  # Atlas accepts a JSON array here
            context=f"add_network_access({project_id})",
        )

    # Cloud Backup snapshots
    async def list_snapshots(
        self, project_id: str, cluster_name: str
    ) -> Dict[str, Any]:
        return await self.get(
            f"/groups/{project_id}/clusters/{cluster_name}/backup/snapshots",
            context=f"list_snapshots({project_id}/{cluster_name})",
        )

    # Alerts
    async def list_alerts(
        self,
        project_id: str,
        status: Optional[str] = None,
        page_num: int = 1,
        items_per_page: int = 100,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageNum": page_num, "itemsPerPage": items_per_page}
        if status:
            params["status"] = status
        return await self.get(
            f"/groups/{project_id}/alerts",
            params=params,
            context=f"list_alerts({project_id})",
        )

    # Programmatic API keys (read-only here)
    async def list_api_keys(
        self,
        org_id: str,
        page_num: int = 1,
        items_per_page: int = 100,
    ) -> Dict[str, Any]:
        return await self.get(
            f"/orgs/{org_id}/apiKeys",
            params={"pageNum": page_num, "itemsPerPage": items_per_page},
            context=f"list_api_keys({org_id})",
        )

    # Billing
    async def list_invoices(
        self,
        org_id: str,
        page_num: int = 1,
        items_per_page: int = 100,
    ) -> Dict[str, Any]:
        return await self.get(
            f"/orgs/{org_id}/invoices",
            params={"pageNum": page_num, "itemsPerPage": items_per_page},
            context=f"list_invoices({org_id})",
        )
