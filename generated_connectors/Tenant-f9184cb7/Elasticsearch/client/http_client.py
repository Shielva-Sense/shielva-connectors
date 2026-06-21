"""All Elasticsearch HTTP calls — zero business logic, zero normalization.

Authenticates via the `Authorization` header (ApiKey or Basic). Retry on
429/5xx is handled by the caller via helpers.utils.with_retry(); transport
errors and 5xx responses are mapped to ElasticsearchNetworkError /
ElasticsearchRateLimitError so the retry helper can react.

NDJSON is the only body format that breaks the "JSON in, JSON out" contract:
`bulk()` serializes a list of operation dicts into NDJSON and POSTs with
`Content-Type: application/x-ndjson`. Every other endpoint speaks plain JSON.
"""
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
import structlog

from exceptions import (
    ElasticsearchAuthError,
    ElasticsearchError,
    ElasticsearchNetworkError,
    ElasticsearchNotFound,
    ElasticsearchRateLimitError,
)
from helpers.utils import build_auth_header, serialize_ndjson

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT_S = 30.0


class ElasticsearchHTTPClient:
    """Thin async HTTP client for the Elasticsearch REST API.

    Constructed with a *base_url* and credentials. The auth header is
    computed once at construction time (ApiKey wins over basic).
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        self._base_url = base_url.rstrip("/")
        self._verify_ssl = verify_ssl
        self._timeout_s = timeout_s
        # Pick auth header at construction time so the client surfaces a clear
        # ValueError if no credentials were supplied.
        self._auth_header = build_auth_header(
            api_key=api_key, username=username, password=password,
        )

    # ── Header builders ────────────────────────────────────────────────────

    def _headers(self, *, content_type: str = "application/json") -> Dict[str, str]:
        return {
            **self._auth_header,
            "Content-Type": content_type,
            "Accept": "application/json",
        }

    # ── Error mapping ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_message(body: Any) -> str:
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return err.get("reason") or err.get("type") or str(body)
            if isinstance(err, str):
                return err
            return body.get("message") or str(body)
        return str(body) if body else "(no body)"

    @classmethod
    def _raise_for_status(cls, status: int, body: Any, context: str) -> None:
        if status < 400:
            return
        message = cls._extract_message(body)
        body_dict = body if isinstance(body, dict) else {}

        if status in (401, 403):
            raise ElasticsearchAuthError(
                f"{status} {context}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise ElasticsearchNotFound(
                f"404 {context}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 429:
            raise ElasticsearchRateLimitError(
                f"429 {context}: rate limit / circuit breaker",
                status_code=429,
                response_body=body_dict,
            )
        if 500 <= status < 600:
            raise ElasticsearchNetworkError(
                f"{status} {context}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise ElasticsearchError(
            f"HTTP {status} {context}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── Core request ───────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        raw_body: Optional[bytes] = None,
        content_type: str = "application/json",
        context: str = "",
    ) -> Dict[str, Any]:
        """Issue one HTTP request to the cluster. Returns parsed JSON dict."""
        url = f"{self._base_url}{path}"
        headers = self._headers(content_type=content_type)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, verify=self._verify_ssl
            ) as client:
                if raw_body is not None:
                    resp = await client.request(
                        method, url, headers=headers, params=params, content=raw_body,
                    )
                else:
                    resp = await client.request(
                        method, url, headers=headers, params=params, json=json_body,
                    )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ElasticsearchNetworkError(
                f"transport error on {url}: {exc}",
            ) from exc

        try:
            body: Any = resp.json()
        except Exception:
            body = None

        self._raise_for_status(resp.status_code, body, context)
        return body if isinstance(body, dict) else {}

    # ── Endpoints ──────────────────────────────────────────────────────────

    async def info(self) -> Dict[str, Any]:
        """GET / — cluster info / liveness probe."""
        return await self._request("GET", "/", context="info")

    async def cluster_health(self, level: str = "cluster") -> Dict[str, Any]:
        """GET /_cluster/health?level=… — cluster health summary."""
        return await self._request(
            "GET",
            "/_cluster/health",
            params={"level": level},
            context="cluster_health",
        )

    async def cat_indices(
        self, index_pattern: str = "*", format: str = "json",
    ) -> Any:
        """GET /_cat/indices/{pattern}?format=… — returns list (not dict)."""
        pattern = quote(index_pattern, safe="*")
        url = f"{self._base_url}/_cat/indices/{pattern}"
        headers = self._headers()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, verify=self._verify_ssl,
            ) as client:
                resp = await client.get(
                    url, headers=headers, params={"format": format},
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ElasticsearchNetworkError(
                f"transport error on {url}: {exc}",
            ) from exc

        try:
            body: Any = resp.json()
        except Exception:
            body = None
        self._raise_for_status(resp.status_code, body, "cat_indices")
        # _cat returns a JSON array — pass it through.
        return body if body is not None else []

    async def create_index(
        self,
        index: str,
        settings: Optional[Dict[str, Any]] = None,
        mappings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """PUT /{index} — create index with optional settings + mappings."""
        body: Dict[str, Any] = {}
        if settings:
            body["settings"] = settings
        if mappings:
            body["mappings"] = mappings
        return await self._request(
            "PUT",
            f"/{quote(index, safe='')}",
            json_body=body or None,
            context=f"create_index({index})",
        )

    async def delete_index(self, index: str) -> Dict[str, Any]:
        """DELETE /{index} — delete index."""
        return await self._request(
            "DELETE",
            f"/{quote(index, safe='')}",
            context=f"delete_index({index})",
        )

    async def index_document(
        self,
        index: str,
        document: Dict[str, Any],
        doc_id: Optional[str] = None,
        refresh: str = "false",
    ) -> Dict[str, Any]:
        """Index a document.

        - When *doc_id* is provided → PUT /{index}/_doc/{id} (upsert).
        - Otherwise               → POST /{index}/_doc (auto-id).
        *refresh* maps to ?refresh=true|false|wait_for.
        """
        params = {"refresh": refresh}
        idx = quote(index, safe="")
        if doc_id:
            return await self._request(
                "PUT",
                f"/{idx}/_doc/{quote(doc_id, safe='')}",
                params=params,
                json_body=document,
                context=f"index_document({index}/{doc_id})",
            )
        return await self._request(
            "POST",
            f"/{idx}/_doc",
            params=params,
            json_body=document,
            context=f"index_document({index})",
        )

    async def get_document(
        self, index: str, doc_id: str,
    ) -> Dict[str, Any]:
        """GET /{index}/_doc/{id}."""
        return await self._request(
            "GET",
            f"/{quote(index, safe='')}/_doc/{quote(doc_id, safe='')}",
            context=f"get_document({index}/{doc_id})",
        )

    async def update_document(
        self,
        index: str,
        doc_id: str,
        doc: Dict[str, Any],
        doc_as_upsert: bool = False,
    ) -> Dict[str, Any]:
        """POST /{index}/_update/{id} — partial update / upsert."""
        body: Dict[str, Any] = {"doc": doc}
        if doc_as_upsert:
            body["doc_as_upsert"] = True
        return await self._request(
            "POST",
            f"/{quote(index, safe='')}/_update/{quote(doc_id, safe='')}",
            json_body=body,
            context=f"update_document({index}/{doc_id})",
        )

    async def delete_document(
        self, index: str, doc_id: str,
    ) -> Dict[str, Any]:
        """DELETE /{index}/_doc/{id}."""
        return await self._request(
            "DELETE",
            f"/{quote(index, safe='')}/_doc/{quote(doc_id, safe='')}",
            context=f"delete_document({index}/{doc_id})",
        )

    async def search(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 10,
        from_: int = 0,
        sort: Optional[List[Any]] = None,
        aggs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /{index}/_search — execute a DSL query."""
        body: Dict[str, Any] = {
            "query": query,
            "size": size,
            "from": from_,
        }
        if sort is not None:
            body["sort"] = sort
        if aggs is not None:
            body["aggs"] = aggs
        return await self._request(
            "POST",
            f"/{quote(index, safe='')}/_search",
            json_body=body,
            context=f"search({index})",
        )

    async def bulk(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /_bulk — NDJSON-encoded bulk operations.

        The *operations* list is the already-paired sequence of action +
        source documents the Bulk API expects (e.g.
        `[{"index": {"_index": "x"}}, {"field": "v"}, ...]`). It is NOT
        re-paired here — the caller owns the semantics.
        """
        ndjson = serialize_ndjson(operations)
        return await self._request(
            "POST",
            "/_bulk",
            raw_body=ndjson,
            content_type="application/x-ndjson",
            context="bulk",
        )

    async def count(
        self, index: str, query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /{index}/_count — count docs (optionally matching *query*)."""
        body: Optional[Dict[str, Any]] = None
        if query is not None:
            body = {"query": query}
        return await self._request(
            "POST",
            f"/{quote(index, safe='')}/_count",
            json_body=body,
            context=f"count({index})",
        )

    async def get_mapping(self, index: str) -> Dict[str, Any]:
        """GET /{index}/_mapping."""
        return await self._request(
            "GET",
            f"/{quote(index, safe='')}/_mapping",
            context=f"get_mapping({index})",
        )

    async def put_mapping(
        self, index: str, properties: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /{index}/_mapping — add or update field mappings."""
        return await self._request(
            "PUT",
            f"/{quote(index, safe='')}/_mapping",
            json_body={"properties": properties},
            context=f"put_mapping({index})",
        )

    async def get_index(self, index: str) -> Dict[str, Any]:
        """GET /{index} — full index info (settings + mappings + aliases)."""
        return await self._request(
            "GET",
            f"/{quote(index, safe='')}",
            context=f"get_index({index})",
        )

    async def cat_aliases(
        self, name: str = "*", format: str = "json",
    ) -> Any:
        """GET /_cat/aliases[/{name}]?format=… — returns list (not dict)."""
        # When name is "*" or blank, hit /_cat/aliases (no path segment).
        if not name or name == "*":
            path = "/_cat/aliases"
        else:
            path = f"/_cat/aliases/{quote(name, safe='*')}"
        url = f"{self._base_url}{path}"
        headers = self._headers()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, verify=self._verify_ssl,
            ) as client:
                resp = await client.get(
                    url, headers=headers, params={"format": format},
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ElasticsearchNetworkError(
                f"transport error on {url}: {exc}",
            ) from exc

        try:
            body: Any = resp.json()
        except Exception:
            body = None
        self._raise_for_status(resp.status_code, body, "cat_aliases")
        return body if body is not None else []

    async def list_snapshots(self, repository: str) -> Dict[str, Any]:
        """GET /_snapshot/{repository}/_all — list snapshots."""
        return await self._request(
            "GET",
            f"/_snapshot/{quote(repository, safe='')}/_all",
            context=f"list_snapshots({repository})",
        )
