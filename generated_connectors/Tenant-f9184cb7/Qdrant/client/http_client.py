"""All Qdrant API HTTP calls — zero business logic, zero normalization.

httpx async client. The Qdrant REST API expects:
  api-key:        <api_key>          (lowercase header — NOT 'Authorization')
  Content-Type:   application/json
  Accept:         application/json

When the api_key is empty (default self-hosted no-auth deployment), the
`api-key` header is omitted entirely.

Retry on 429/5xx with exponential backoff and Retry-After awareness.
"""
import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    QdrantAuthError,
    QdrantError,
    QdrantNetworkError,
    QdrantNotFound,
)

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class QdrantHTTPClient:
    """Thin async HTTP client for the Qdrant REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._api_key = api_key or ""
        self._base_url = (base_url or "").rstrip("/")
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        # Qdrant Cloud expects lowercase `api-key`. NOT 'Authorization',
        # NOT 'Bearer'. When empty (self-hosted no-auth), the header is
        # omitted entirely so the server does not see an empty credential.
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["api-key"] = self._api_key
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
            # Qdrant returns either {"status": {"error": "..."}} for typed
            # errors, or a flat {"error": "..."} message on simple 4xx.
            message = ""
            status_obj = body.get("status")
            if isinstance(status_obj, dict):
                message = status_obj.get("error") or ""
            if not message:
                message = (
                    body.get("error")
                    or body.get("message")
                    or body.get("description")
                    or str(body)
                )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        if status == 401 or status == 403:
            raise QdrantAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 404:
            raise QdrantNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        raise QdrantError(
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
                        retry_after = response.headers.get("retry-after")
                        if retry_after:
                            try:
                                delay = min(float(retry_after), 30.0)
                            except ValueError:
                                delay = _BACKOFF_BASE * (2 ** attempt)
                        else:
                            delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "qdrant.http.retry",
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
                        "qdrant.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise QdrantNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise QdrantNetworkError(str(last_exc)) from last_exc
        raise QdrantNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Service / health ───────────────────────────────────────────────────

    async def healthz(self) -> Dict[str, Any]:
        """GET /healthz — Qdrant liveness probe (200 + 'healthz check passed')."""
        return await self._request("GET", "/healthz", context="healthz")

    async def readyz(self) -> Dict[str, Any]:
        """GET /readyz — readiness probe."""
        return await self._request("GET", "/readyz", context="readyz")

    async def root(self) -> Dict[str, Any]:
        """GET / — returns Qdrant version + title."""
        return await self._request("GET", "/", context="root")

    async def telemetry(self) -> Dict[str, Any]:
        """GET /telemetry — runtime telemetry (collections, requests, etc.)."""
        return await self._request("GET", "/telemetry", context="telemetry")

    # ── Cluster ────────────────────────────────────────────────────────────

    async def get_cluster_info(self) -> Dict[str, Any]:
        """GET /cluster — cluster topology + peer info."""
        return await self._request("GET", "/cluster", context="get_cluster_info")

    # ── Collections ────────────────────────────────────────────────────────

    async def list_collections(self) -> Dict[str, Any]:
        """GET /collections — list all collections."""
        return await self._request("GET", "/collections", context="list_collections")

    async def get_collection(self, collection_name: str) -> Dict[str, Any]:
        """GET /collections/{name}."""
        return await self._request(
            "GET",
            f"/collections/{collection_name}",
            context=f"get_collection({collection_name})",
        )

    async def create_collection(
        self,
        collection_name: str,
        vectors: Dict[str, Any],
        optimizers_config: Optional[Dict[str, Any]] = None,
        shard_number: int = 1,
        replication_factor: int = 1,
        write_consistency_factor: int = 1,
        on_disk_payload: bool = False,
    ) -> Dict[str, Any]:
        """PUT /collections/{name} — create a new collection."""
        body: Dict[str, Any] = {
            "vectors": vectors,
            "shard_number": shard_number,
            "replication_factor": replication_factor,
            "write_consistency_factor": write_consistency_factor,
            "on_disk_payload": on_disk_payload,
        }
        if optimizers_config is not None:
            body["optimizers_config"] = optimizers_config
        return await self._request(
            "PUT",
            f"/collections/{collection_name}",
            json_body=body,
            context=f"create_collection({collection_name})",
        )

    async def delete_collection(self, collection_name: str) -> Dict[str, Any]:
        """DELETE /collections/{name}."""
        return await self._request(
            "DELETE",
            f"/collections/{collection_name}",
            context=f"delete_collection({collection_name})",
        )

    async def update_collection(
        self,
        collection_name: str,
        optimizers_config: Optional[Dict[str, Any]] = None,
        vectors_config: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """PATCH /collections/{name}."""
        body: Dict[str, Any] = {}
        if optimizers_config is not None:
            body["optimizers_config"] = optimizers_config
        if vectors_config is not None:
            body["vectors"] = vectors_config
        if params is not None:
            body["params"] = params
        return await self._request(
            "PATCH",
            f"/collections/{collection_name}",
            json_body=body,
            context=f"update_collection({collection_name})",
        )

    # ── Points (write) ─────────────────────────────────────────────────────

    async def upsert_points(
        self,
        collection_name: str,
        points: List[Dict[str, Any]],
        wait: bool = True,
    ) -> Dict[str, Any]:
        """PUT /collections/{name}/points — upsert a batch of points."""
        return await self._request(
            "PUT",
            f"/collections/{collection_name}/points",
            json_body={"points": points},
            params={"wait": str(wait).lower()},
            context=f"upsert_points({collection_name})",
        )

    async def delete_points(
        self,
        collection_name: str,
        points: Optional[List[Any]] = None,
        filter: Optional[Dict[str, Any]] = None,
        wait: bool = True,
    ) -> Dict[str, Any]:
        """POST /collections/{name}/points/delete — delete by ids or filter."""
        body: Dict[str, Any] = {}
        if points is not None:
            body["points"] = points
        if filter is not None:
            body["filter"] = filter
        return await self._request(
            "POST",
            f"/collections/{collection_name}/points/delete",
            json_body=body,
            params={"wait": str(wait).lower()},
            context=f"delete_points({collection_name})",
        )

    # ── Points (read) ──────────────────────────────────────────────────────

    async def get_points(
        self,
        collection_name: str,
        ids: List[Any],
        with_payload: bool = True,
        with_vector: bool = False,
    ) -> Dict[str, Any]:
        """POST /collections/{name}/points — retrieve points by id."""
        body: Dict[str, Any] = {
            "ids": ids,
            "with_payload": with_payload,
            "with_vector": with_vector,
        }
        return await self._request(
            "POST",
            f"/collections/{collection_name}/points",
            json_body=body,
            context=f"get_points({collection_name})",
        )

    async def search_points(
        self,
        collection_name: str,
        vector: List[float],
        limit: int = 10,
        with_payload: bool = True,
        with_vector: bool = False,
        score_threshold: Optional[float] = None,
        filter: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /collections/{name}/points/search — top-k similarity search."""
        body: Dict[str, Any] = {
            "vector": vector,
            "limit": limit,
            "with_payload": with_payload,
            "with_vector": with_vector,
        }
        if score_threshold is not None:
            body["score_threshold"] = score_threshold
        if filter is not None:
            body["filter"] = filter
        if params is not None:
            body["params"] = params
        return await self._request(
            "POST",
            f"/collections/{collection_name}/points/search",
            json_body=body,
            context=f"search_points({collection_name})",
        )

    async def scroll_points(
        self,
        collection_name: str,
        limit: int = 100,
        offset: Optional[Any] = None,
        with_payload: bool = True,
        with_vector: bool = False,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /collections/{name}/points/scroll — cursor-paged points."""
        body: Dict[str, Any] = {
            "limit": limit,
            "with_payload": with_payload,
            "with_vector": with_vector,
        }
        if offset is not None:
            body["offset"] = offset
        if filter is not None:
            body["filter"] = filter
        return await self._request(
            "POST",
            f"/collections/{collection_name}/points/scroll",
            json_body=body,
            context=f"scroll_points({collection_name})",
        )

    async def count_points(
        self,
        collection_name: str,
        filter: Optional[Dict[str, Any]] = None,
        exact: bool = True,
    ) -> Dict[str, Any]:
        """POST /collections/{name}/points/count."""
        body: Dict[str, Any] = {"exact": exact}
        if filter is not None:
            body["filter"] = filter
        return await self._request(
            "POST",
            f"/collections/{collection_name}/points/count",
            json_body=body,
            context=f"count_points({collection_name})",
        )

    # ── Indexes ────────────────────────────────────────────────────────────

    async def create_payload_index(
        self,
        collection_name: str,
        field_name: str,
        field_schema: str,
        wait: bool = True,
    ) -> Dict[str, Any]:
        """PUT /collections/{name}/index — create a payload-field index."""
        body: Dict[str, Any] = {
            "field_name": field_name,
            "field_schema": field_schema,
        }
        return await self._request(
            "PUT",
            f"/collections/{collection_name}/index",
            json_body=body,
            params={"wait": str(wait).lower()},
            context=f"create_payload_index({collection_name},{field_name})",
        )

    async def delete_payload_index(
        self,
        collection_name: str,
        field_name: str,
        wait: bool = True,
    ) -> Dict[str, Any]:
        """DELETE /collections/{name}/index/{field_name}."""
        return await self._request(
            "DELETE",
            f"/collections/{collection_name}/index/{field_name}",
            params={"wait": str(wait).lower()},
            context=f"delete_payload_index({collection_name},{field_name})",
        )

    # ── Snapshots ──────────────────────────────────────────────────────────

    async def list_snapshots(self, collection_name: str) -> Dict[str, Any]:
        """GET /collections/{name}/snapshots."""
        return await self._request(
            "GET",
            f"/collections/{collection_name}/snapshots",
            context=f"list_snapshots({collection_name})",
        )

    async def create_snapshot(self, collection_name: str) -> Dict[str, Any]:
        """POST /collections/{name}/snapshots."""
        return await self._request(
            "POST",
            f"/collections/{collection_name}/snapshots",
            context=f"create_snapshot({collection_name})",
        )
