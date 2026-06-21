"""All Pinecone API HTTP calls — zero business logic, zero normalization.

Pinecone has two planes:
  * Control plane    — https://api.pinecone.io  (indexes, collections)
  * Data plane (per-index) — https://{index_host}  (vectors, query, stats)

This client maintains BOTH base URLs and caches the index→host map after the
first describe_index() call so subsequent data-plane calls go straight to the
correct host.

All methods return the raw response JSON (dict). Retry logic for 429 / 5xx is
handled in-process by `_request_with_retry()` with exponential backoff +
random jitter to avoid synchronised reconnect storms.

Header contract (every request, control AND data plane):
    Api-Key:                <api_key>      ← raw key, NOT Authorization
    Content-Type:           application/json
    Accept:                 application/json
    X-Pinecone-API-Version: <api_version>  ← default 2025-01
"""
import asyncio
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    PineconeAuthError,
    PineconeBadRequestError,
    PineconeConflictError,
    PineconeError,
    PineconeNetworkError,
    PineconeNotFoundError,
    PineconeRateLimitError,
    PineconeServerError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_CONTROL_URL = "https://api.pinecone.io"
_DEFAULT_API_VERSION = "2025-01"
_DEFAULT_TIMEOUT_S = 30.0

# OCP: retry constants — change here, nowhere else
_RETRY_BASE_DELAY_S: float = 1.0
_RETRY_BACKOFF: float = 2.0
_RETRY_MAX_DELAY_S: float = 32.0
_RETRY_MAX_ATTEMPTS: int = 3


class PineconeHTTPClient:
    """Thin async HTTP client for the Pinecone REST API.

    Maintains TWO base URLs:
      * control plane (`api.pinecone.io`) for index/collection CRUD
      * data plane (`{index_host}`) for vector operations

    The index→host mapping is populated lazily by `describe_index()` and
    cached on the instance.
    """

    def __init__(
        self,
        api_key: str,
        control_url: str = _DEFAULT_CONTROL_URL,
        api_version: str = _DEFAULT_API_VERSION,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key or ""
        self._control_url = (control_url or _DEFAULT_CONTROL_URL).rstrip("/")
        self._api_version = api_version or _DEFAULT_API_VERSION
        self._timeout_s = timeout_s
        # index_name → fully-qualified data-plane host (e.g. "https://my-idx-xxxx.svc.aped-…")
        self._index_host_cache: Dict[str, str] = {}

    # ── Headers ────────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Api-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Pinecone-API-Version": self._api_version,
        }

    # ── Host cache helpers ─────────────────────────────────────────────────

    def cache_index_host(self, index_name: str, host: str) -> None:
        """Manually seed the index→host cache (used by the connector when a
        default_index host is supplied in config)."""
        if not host:
            return
        normalized = host if host.startswith("http") else f"https://{host}"
        self._index_host_cache[index_name] = normalized.rstrip("/")

    def get_cached_host(self, index_name: str) -> Optional[str]:
        return self._index_host_cache.get(index_name)

    async def _resolve_index_host(self, index_name: str) -> str:
        """Return cached host for *index_name* or fetch it via describe_index()."""
        host = self._index_host_cache.get(index_name)
        if host:
            return host
        spec = await self.describe_index(index_name)
        host_value = spec.get("host", "")
        if not host_value:
            raise PineconeError(
                f"describe_index({index_name}) returned no 'host' field",
                response_body=spec,
            )
        self.cache_index_host(index_name, host_value)
        return self._index_host_cache[index_name]

    # ── Error mapping ──────────────────────────────────────────────────────

    def _extract_message(self, body: Any, response: httpx.Response) -> str:
        if isinstance(body, dict):
            # Pinecone often nests error info inside body["error"]
            err = body.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code") or ""
                if msg:
                    return str(msg)
            for k in ("message", "details", "code"):
                v = body.get(k)
                if v:
                    return str(v)
        return response.text or f"HTTP {response.status_code}"

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}

        message = self._extract_message(body, response)
        body_dict = body if isinstance(body, dict) else {"raw": body}
        ctx = f": {context}" if context else ""

        if status == 400:
            raise PineconeBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status in (401, 403):
            raise PineconeAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise PineconeNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise PineconeConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = 5.0
            try:
                retry_after = float(response.headers.get("Retry-After") or 5.0)
            except (TypeError, ValueError):
                retry_after = 5.0
            raise PineconeRateLimitError(
                f"429 Rate limit exceeded{ctx}: {message}",
                status_code=429,
                response_body=body_dict,
                retry_after_s=retry_after,
            )
        if 500 <= status < 600:
            raise PineconeServerError(
                f"{status} Server Error{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise PineconeError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── Core request with retry ────────────────────────────────────────────

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Send a request and retry on 429 / 5xx with exponential backoff + jitter."""
        last_exc: Optional[Exception] = None
        for attempt in range(_RETRY_MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                        json=json_body,
                    )
                # Map non-2xx to typed exceptions; retry-eligible ones are caught below.
                self._raise_for_status(response, context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (PineconeRateLimitError, PineconeServerError) as exc:
                last_exc = exc
                if attempt == _RETRY_MAX_ATTEMPTS:
                    break
                delay = min(
                    _RETRY_BASE_DELAY_S * (_RETRY_BACKOFF ** attempt)
                    + random.uniform(0, 0.5),
                    _RETRY_MAX_DELAY_S,
                )
                logger.warning(
                    "pinecone.retry",
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                    status=getattr(exc, "status_code", 0),
                )
                await asyncio.sleep(delay)
            except httpx.HTTPError as exc:
                # Transport error — wrap and retry.
                last_exc = PineconeNetworkError(
                    f"Network error{(': ' + context) if context else ''}: {exc}"
                )
                if attempt == _RETRY_MAX_ATTEMPTS:
                    break
                delay = min(
                    _RETRY_BASE_DELAY_S * (_RETRY_BACKOFF ** attempt)
                    + random.uniform(0, 0.5),
                    _RETRY_MAX_DELAY_S,
                )
                logger.warning(
                    "pinecone.network_retry",
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    # ── Control plane: indexes ─────────────────────────────────────────────

    async def list_indexes(self) -> Dict[str, Any]:
        """GET {control}/indexes — list all indexes for this project."""
        url = f"{self._control_url}/indexes"
        return await self._request_with_retry("GET", url, context="list_indexes")

    async def describe_index(self, index_name: str) -> Dict[str, Any]:
        """GET {control}/indexes/{name} — fetch index spec including host."""
        url = f"{self._control_url}/indexes/{index_name}"
        result = await self._request_with_retry(
            "GET", url, context=f"describe_index({index_name})"
        )
        host = result.get("host", "")
        if host:
            self.cache_index_host(index_name, host)
        return result

    async def create_index(
        self,
        name: str,
        dimension: int,
        metric: str = "cosine",
        cloud: str = "aws",
        region: str = "us-east-1",
    ) -> Dict[str, Any]:
        """POST {control}/indexes — create a new serverless index."""
        url = f"{self._control_url}/indexes"
        body = {
            "name": name,
            "dimension": dimension,
            "metric": metric,
            "spec": {"serverless": {"cloud": cloud, "region": region}},
        }
        return await self._request_with_retry(
            "POST", url, json_body=body, context=f"create_index({name})"
        )

    async def delete_index(self, index_name: str) -> Dict[str, Any]:
        """DELETE {control}/indexes/{name}."""
        url = f"{self._control_url}/indexes/{index_name}"
        result = await self._request_with_retry(
            "DELETE", url, context=f"delete_index({index_name})"
        )
        self._index_host_cache.pop(index_name, None)
        return result

    async def configure_index(
        self,
        index_name: str,
        replicas: Optional[int] = None,
        pod_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PATCH {control}/indexes/{name} — reconfigure pod-based indexes.

        Body shape: `{spec: {pod: {replicas?, pod_type?}}}`. At least one of
        `replicas` / `pod_type` must be provided.
        """
        url = f"{self._control_url}/indexes/{index_name}"
        pod_spec: Dict[str, Any] = {}
        if replicas is not None:
            pod_spec["replicas"] = replicas
        if pod_type is not None:
            pod_spec["pod_type"] = pod_type
        if not pod_spec:
            raise PineconeBadRequestError(
                "configure_index requires at least one of replicas / pod_type"
            )
        body = {"spec": {"pod": pod_spec}}
        return await self._request_with_retry(
            "PATCH", url, json_body=body, context=f"configure_index({index_name})"
        )

    async def list_collections(self) -> Dict[str, Any]:
        """GET {control}/collections."""
        url = f"{self._control_url}/collections"
        return await self._request_with_retry("GET", url, context="list_collections")

    async def create_collection(self, name: str, source: str) -> Dict[str, Any]:
        """POST {control}/collections — create a collection from a source index."""
        url = f"{self._control_url}/collections"
        body = {"name": name, "source": source}
        return await self._request_with_retry(
            "POST", url, json_body=body, context=f"create_collection({name})"
        )

    async def delete_collection(self, collection_name: str) -> Dict[str, Any]:
        """DELETE {control}/collections/{name}."""
        url = f"{self._control_url}/collections/{collection_name}"
        return await self._request_with_retry(
            "DELETE", url, context=f"delete_collection({collection_name})"
        )

    # ── Data plane: vectors ────────────────────────────────────────────────

    async def upsert_vectors(
        self,
        index_name: str,
        vectors: List[Dict[str, Any]],
        namespace: str = "",
    ) -> Dict[str, Any]:
        """POST {host}/vectors/upsert — insert or update vectors."""
        host = await self._resolve_index_host(index_name)
        url = f"{host}/vectors/upsert"
        body: Dict[str, Any] = {"vectors": vectors}
        if namespace:
            body["namespace"] = namespace
        return await self._request_with_retry(
            "POST", url, json_body=body, context=f"upsert_vectors({index_name})"
        )

    async def query(
        self,
        index_name: str,
        vector: List[float],
        top_k: int = 10,
        namespace: str = "",
        filter: Optional[Dict[str, Any]] = None,
        include_metadata: bool = True,
        include_values: bool = False,
    ) -> Dict[str, Any]:
        """POST {host}/query — nearest-neighbor search."""
        host = await self._resolve_index_host(index_name)
        url = f"{host}/query"
        body: Dict[str, Any] = {
            "vector": vector,
            "topK": top_k,
            "includeMetadata": include_metadata,
            "includeValues": include_values,
        }
        if namespace:
            body["namespace"] = namespace
        if filter:
            body["filter"] = filter
        return await self._request_with_retry(
            "POST", url, json_body=body, context=f"query({index_name})"
        )

    async def fetch_vectors(
        self,
        index_name: str,
        ids: List[str],
        namespace: str = "",
    ) -> Dict[str, Any]:
        """GET {host}/vectors/fetch?ids=&namespace= — fetch vectors by ID."""
        host = await self._resolve_index_host(index_name)
        url = f"{host}/vectors/fetch"
        params: Dict[str, Any] = {"ids": ids}
        if namespace:
            params["namespace"] = namespace
        return await self._request_with_retry(
            "GET", url, params=params, context=f"fetch_vectors({index_name})"
        )

    async def delete_vectors(
        self,
        index_name: str,
        ids: Optional[List[str]] = None,
        delete_all: bool = False,
        namespace: str = "",
    ) -> Dict[str, Any]:
        """POST {host}/vectors/delete — delete by id list or delete-all."""
        host = await self._resolve_index_host(index_name)
        url = f"{host}/vectors/delete"
        body: Dict[str, Any] = {}
        if delete_all:
            body["deleteAll"] = True
        if ids:
            body["ids"] = ids
        if namespace:
            body["namespace"] = namespace
        return await self._request_with_retry(
            "POST", url, json_body=body, context=f"delete_vectors({index_name})"
        )

    async def update_vector(
        self,
        index_name: str,
        id: str,
        values: Optional[List[float]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        namespace: str = "",
    ) -> Dict[str, Any]:
        """POST {host}/vectors/update — partial update of a single vector."""
        host = await self._resolve_index_host(index_name)
        url = f"{host}/vectors/update"
        body: Dict[str, Any] = {"id": id}
        if values is not None:
            body["values"] = values
        if metadata is not None:
            body["setMetadata"] = metadata
        if namespace:
            body["namespace"] = namespace
        return await self._request_with_retry(
            "POST", url, json_body=body, context=f"update_vector({index_name}/{id})"
        )

    async def describe_index_stats(
        self,
        index_name: str,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST {host}/describe_index_stats — index dimension / fullness / namespaces."""
        host = await self._resolve_index_host(index_name)
        url = f"{host}/describe_index_stats"
        body: Dict[str, Any] = {}
        if filter:
            body["filter"] = filter
        return await self._request_with_retry(
            "POST", url, json_body=body, context=f"describe_index_stats({index_name})"
        )

    async def list_namespaces(self, index_name: str) -> Dict[str, Any]:
        """GET {host}/namespaces — list namespaces inside the index.

        Falls back to deriving the namespace list from describe_index_stats when
        the endpoint returns 404 (older index versions).
        """
        host = await self._resolve_index_host(index_name)
        url = f"{host}/namespaces"
        try:
            return await self._request_with_retry(
                "GET", url, context=f"list_namespaces({index_name})"
            )
        except PineconeNotFoundError:
            stats = await self.describe_index_stats(index_name)
            ns_map = stats.get("namespaces", {}) or {}
            return {"namespaces": [{"name": k, **(v or {})} for k, v in ns_map.items()]}
