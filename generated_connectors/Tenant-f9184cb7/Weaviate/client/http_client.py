"""All Weaviate API HTTP calls — zero business logic, zero normalization.

httpx async client. Weaviate REST + GraphQL surfaces expect:
  Authorization: Bearer <api_key>     (when api_key is configured)
  Content-Type:  application/json
  Accept:        application/json

Retry on 429 / 5xx with exponential backoff.
"""
import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    WeaviateAuthError,
    WeaviateBadRequestError,
    WeaviateConflictError,
    WeaviateError,
    WeaviateNetworkError,
    WeaviateNotFoundError,
    WeaviateRateLimitError,
    WeaviateServerError,
    WeaviateValidationError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class WeaviateHTTPClient:
    """Thin async HTTP client for the Weaviate REST + GraphQL API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        if not base_url:
            raise ValueError("WeaviateHTTPClient: base_url is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
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
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            message = (
                body.get("message")
                or body.get("error")
                or body.get("details")
                or str(body)
            )
            if isinstance(message, list):
                message = "; ".join(str(m) for m in message)
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        body_dict = body if isinstance(body, dict) else {"raw": body}
        ctx = f": {context}" if context else ""

        if status in (401, 403):
            raise WeaviateAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise WeaviateNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 400:
            raise WeaviateBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 409:
            raise WeaviateConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 422:
            raise WeaviateValidationError(
                f"422 Unprocessable{ctx}: {message}",
                status_code=422,
                response_body=body_dict,
            )
        if status == 429:
            raise WeaviateRateLimitError(
                f"429 Rate Limited{ctx}: {message}",
            )
        if status >= 500:
            raise WeaviateServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise WeaviateError(
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
                            "weaviate.http.retry",
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
                        "weaviate.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise WeaviateNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise WeaviateNetworkError(str(last_exc)) from last_exc
        raise WeaviateNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Cluster ────────────────────────────────────────────────────────────

    async def get_ready(self) -> Dict[str, Any]:
        """GET /v1/.well-known/ready — liveness probe."""
        return await self._request("GET", "/v1/.well-known/ready", context="get_ready")

    async def get_meta(self) -> Dict[str, Any]:
        """GET /v1/meta — version + module info."""
        return await self._request("GET", "/v1/meta", context="get_meta")

    # ── Schema (classes) ───────────────────────────────────────────────────

    async def list_classes(self) -> Dict[str, Any]:
        """GET /v1/schema — list all classes."""
        return await self._request("GET", "/v1/schema", context="list_classes")

    async def create_class(self, class_body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v1/schema — create a class from the raw class definition."""
        return await self._request(
            "POST",
            "/v1/schema",
            json_body=class_body,
            context="create_class",
        )

    async def get_class(self, class_name: str) -> Dict[str, Any]:
        """GET /v1/schema/{className}."""
        return await self._request(
            "GET",
            f"/v1/schema/{class_name}",
            context=f"get_class({class_name})",
        )

    async def delete_class(self, class_name: str) -> Dict[str, Any]:
        """DELETE /v1/schema/{className}."""
        return await self._request(
            "DELETE",
            f"/v1/schema/{class_name}",
            context=f"delete_class({class_name})",
        )

    # ── Objects ────────────────────────────────────────────────────────────

    async def list_objects(
        self,
        *,
        class_name: Optional[str] = None,
        limit: int = 100,
        after: Optional[str] = None,
        include: Optional[str] = None,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/objects — list objects, optionally filtered by class."""
        params: Dict[str, Any] = {"limit": limit}
        if class_name:
            params["class"] = class_name
        if after:
            params["after"] = after
        if include:
            params["include"] = include
        if tenant:
            params["tenant"] = tenant
        return await self._request(
            "GET",
            "/v1/objects",
            params=params,
            context="list_objects",
        )

    async def create_object(
        self,
        class_name: str,
        properties: Dict[str, Any],
        *,
        vector: Optional[List[float]] = None,
        object_id: Optional[str] = None,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/objects — create a single object."""
        body: Dict[str, Any] = {
            "class": class_name,
            "properties": properties,
        }
        if vector is not None:
            body["vector"] = vector
        if object_id is not None:
            body["id"] = object_id
        if tenant is not None:
            body["tenant"] = tenant
        return await self._request(
            "POST",
            "/v1/objects",
            json_body=body,
            context="create_object",
        )

    async def get_object(
        self,
        class_name: str,
        object_id: str,
        *,
        include: Optional[str] = None,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/objects/{className}/{id}."""
        params: Dict[str, Any] = {}
        if include:
            params["include"] = include
        if tenant:
            params["tenant"] = tenant
        return await self._request(
            "GET",
            f"/v1/objects/{class_name}/{object_id}",
            params=params or None,
            context=f"get_object({class_name}/{object_id})",
        )

    async def update_object(
        self,
        class_name: str,
        object_id: str,
        properties: Dict[str, Any],
        *,
        vector: Optional[List[float]] = None,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PATCH /v1/objects/{className}/{id} — partial update."""
        body: Dict[str, Any] = {
            "class": class_name,
            "id": object_id,
            "properties": properties,
        }
        if vector is not None:
            body["vector"] = vector
        if tenant is not None:
            body["tenant"] = tenant
        return await self._request(
            "PATCH",
            f"/v1/objects/{class_name}/{object_id}",
            json_body=body,
            context=f"update_object({class_name}/{object_id})",
        )

    async def delete_object(
        self,
        class_name: str,
        object_id: str,
        *,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """DELETE /v1/objects/{className}/{id}."""
        params: Dict[str, Any] = {}
        if tenant:
            params["tenant"] = tenant
        return await self._request(
            "DELETE",
            f"/v1/objects/{class_name}/{object_id}",
            params=params or None,
            context=f"delete_object({class_name}/{object_id})",
        )

    # ── Batch ──────────────────────────────────────────────────────────────

    async def batch_create_objects(
        self,
        objects: List[Dict[str, Any]],
        *,
        consistency_level: Optional[str] = None,
    ) -> Any:
        """POST /v1/batch/objects — bulk insert objects."""
        params: Dict[str, Any] = {}
        if consistency_level:
            params["consistency_level"] = consistency_level
        body = {"objects": objects}
        return await self._request(
            "POST",
            "/v1/batch/objects",
            params=params or None,
            json_body=body,
            context="batch_create_objects",
        )

    # ── GraphQL ────────────────────────────────────────────────────────────

    async def graphql_query(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /v1/graphql — execute a GraphQL query (Get / Aggregate / Explore)."""
        body: Dict[str, Any] = {"query": query}
        if variables is not None:
            body["variables"] = variables
        return await self._request(
            "POST",
            "/v1/graphql",
            json_body=body,
            context="graphql_query",
        )

    # ── Multi-tenancy ──────────────────────────────────────────────────────

    async def list_tenants(self, class_name: str) -> Any:
        """GET /v1/schema/{className}/tenants."""
        return await self._request(
            "GET",
            f"/v1/schema/{class_name}/tenants",
            context=f"list_tenants({class_name})",
        )

    async def create_tenants(
        self,
        class_name: str,
        tenants: List[Dict[str, Any]],
    ) -> Any:
        """POST /v1/schema/{className}/tenants — body: [{name, activityStatus?}, ...]."""
        return await self._request(
            "POST",
            f"/v1/schema/{class_name}/tenants",
            json_body=tenants,
            context=f"create_tenants({class_name})",
        )

    async def delete_tenants(
        self,
        class_name: str,
        tenant_names: List[str],
    ) -> Any:
        """DELETE /v1/schema/{className}/tenants — body: ["name1", "name2"]."""
        return await self._request(
            "DELETE",
            f"/v1/schema/{class_name}/tenants",
            json_body=tenant_names,
            context=f"delete_tenants({class_name})",
        )

    # ── Backups ────────────────────────────────────────────────────────────

    async def create_backup(
        self,
        backend: str,
        backup_id: str,
        *,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /v1/backups/{backend}."""
        body: Dict[str, Any] = {"id": backup_id}
        if include is not None:
            body["include"] = include
        if exclude is not None:
            body["exclude"] = exclude
        return await self._request(
            "POST",
            f"/v1/backups/{backend}",
            json_body=body,
            context=f"create_backup({backend}/{backup_id})",
        )

    async def get_backup_status(
        self,
        backend: str,
        backup_id: str,
    ) -> Dict[str, Any]:
        """GET /v1/backups/{backend}/{id}."""
        return await self._request(
            "GET",
            f"/v1/backups/{backend}/{backup_id}",
            context=f"get_backup_status({backend}/{backup_id})",
        )
