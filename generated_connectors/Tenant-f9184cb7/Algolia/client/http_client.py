"""All Algolia API HTTP calls — zero business logic, zero normalization.

Implements the official Algolia host-rotation strategy:

  - Read paths first try ``<app_id>-dsn.algolia.net`` (DNS-balanced PoP);
    on 5xx or transport error they fall through to a shuffled ring of
    ``<app_id>-{1,2,3}.algolianet.com`` hosts (separate DNS zone).
  - Write paths first try ``<app_id>.algolia.net`` (single write primary);
    on failure they fall through to the same ``algolianet.com`` ring.

4xx errors (auth / not-found / bad-request / rate-limit) are not retried
on a different host — they are identical on every node — they raise
immediately.

Auth is via ``X-Algolia-Application-Id`` + ``X-Algolia-API-Key`` headers
on every request — never as query parameters.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    AlgoliaAuthError,
    AlgoliaBadRequestError,
    AlgoliaError,
    AlgoliaNetworkError,
    AlgoliaNotFound,
    AlgoliaRateLimitError,
    AlgoliaServerError,
)
from helpers.utils import build_read_hosts, build_write_hosts

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT_S = 30.0


class AlgoliaHTTPClient:
    """Thin async HTTP client for the Algolia REST API."""

    def __init__(
        self,
        app_id: str,
        api_key: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ):
        if not app_id:
            raise ValueError("app_id is required")
        if not api_key:
            raise ValueError("api_key is required")
        self._app_id = app_id
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._read_hosts = build_read_hosts(app_id)
        self._write_hosts = build_write_hosts(app_id)

    # ── Header builder ─────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Algolia-Application-Id": self._app_id,
            "X-Algolia-API-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Error mapping ──────────────────────────────────────────────────────

    @staticmethod
    def _raise_for_status(
        status: int,
        body: Any,
        context: str,
    ) -> None:
        """Map HTTP status to a typed exception. Never raises on <400."""
        if status < 400:
            return
        if isinstance(body, dict):
            message = body.get("message") or str(body)
        else:
            message = str(body) if body else "(no body)"
        body_dict = body if isinstance(body, dict) else {}

        if status == 400:
            raise AlgoliaBadRequestError(
                f"400 {context}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status in (401, 403):
            raise AlgoliaAuthError(
                f"{status} {context}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise AlgoliaNotFound(
                f"404 {context}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 429:
            raise AlgoliaRateLimitError(
                f"429 {context}: rate limit exceeded",
                response_body=body_dict,
            )
        if 500 <= status < 600:
            raise AlgoliaServerError(
                f"{status} {context}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise AlgoliaError(
            f"HTTP {status} {context}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── Core request loop with host rotation ───────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        read: bool,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Try every host in the appropriate rotation. Return parsed JSON.

        A host is skipped when:
          - the transport raises (DNS / connect / timeout) → captured
          - the server returns 5xx                          → captured

        4xx errors raise immediately — they are the same on every node.
        """
        hosts = self._read_hosts if read else self._write_hosts
        last_exc: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            for host in hosts:
                url = f"{host}{path}"
                try:
                    resp = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                        json=json_body,
                    )
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    last_exc = AlgoliaNetworkError(
                        f"transport error on {host}: {exc}"
                    )
                    logger.warning(
                        "algolia.http.host_transport_error",
                        host=host,
                        context=context,
                        error=str(exc),
                    )
                    continue

                # parse body best-effort
                try:
                    body: Any = resp.json()
                except (ValueError, Exception):
                    body = None

                if 500 <= resp.status_code < 600:
                    last_exc = AlgoliaServerError(
                        f"{resp.status_code} on {host}",
                        status_code=resp.status_code,
                        response_body=body if isinstance(body, dict) else {},
                    )
                    logger.warning(
                        "algolia.http.host_5xx",
                        host=host,
                        status=resp.status_code,
                        context=context,
                    )
                    continue

                # 4xx + 2xx → map and return
                self._raise_for_status(resp.status_code, body, context)
                return body if isinstance(body, dict) else {}

        # all hosts exhausted
        raise AlgoliaNetworkError(
            f"all hosts failed for {context}: {last_exc}"
        )

    # ── Liveness ───────────────────────────────────────────────────────────

    async def is_alive(self) -> Dict[str, Any]:
        """``GET /1/isalive`` — liveness probe."""
        return await self._request(
            "GET", "/1/isalive", read=True, context="is_alive"
        )

    # ── Indexes ────────────────────────────────────────────────────────────

    async def list_indexes(self) -> Dict[str, Any]:
        """``GET /1/indexes`` — list all indexes on the application."""
        return await self._request(
            "GET", "/1/indexes", read=True, context="list_indexes"
        )

    async def clear_index(self, index_name: str) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/clear`` — clear all objects (keep settings)."""
        return await self._request(
            "POST",
            f"/1/indexes/{index_name}/clear",
            read=False,
            json_body={},
            context=f"clear_index({index_name})",
        )

    async def delete_index(self, index_name: str) -> Dict[str, Any]:
        """``DELETE /1/indexes/{name}`` — delete an entire index."""
        return await self._request(
            "DELETE",
            f"/1/indexes/{index_name}",
            read=False,
            context=f"delete_index({index_name})",
        )

    async def copy_index(
        self,
        source_index: str,
        destination_index: str,
        scope: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{src}/operation`` — copy / move index."""
        body: Dict[str, Any] = {
            "operation": "copy",
            "destination": destination_index,
        }
        if scope:
            body["scope"] = scope
        return await self._request(
            "POST",
            f"/1/indexes/{source_index}/operation",
            read=False,
            json_body=body,
            context=f"copy_index({source_index}->{destination_index})",
        )

    # ── Settings ───────────────────────────────────────────────────────────

    async def get_index_settings(self, index_name: str) -> Dict[str, Any]:
        """``GET /1/indexes/{name}/settings``."""
        return await self._request(
            "GET",
            f"/1/indexes/{index_name}/settings",
            read=True,
            context=f"get_index_settings({index_name})",
        )

    async def create_index_settings(
        self, index_name: str, settings: Dict[str, Any]
    ) -> Dict[str, Any]:
        """``PUT /1/indexes/{name}/settings`` — replace index settings.

        Algolia creates the index lazily on the first settings PUT, so this
        method serves both 'create' and 'replace settings' use-cases.
        """
        return await self._request(
            "PUT",
            f"/1/indexes/{index_name}/settings",
            read=False,
            json_body=settings,
            context=f"create_index_settings({index_name})",
        )

    # ── Objects ────────────────────────────────────────────────────────────

    async def save_object(
        self,
        index_name: str,
        object_data: Dict[str, Any],
        object_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST`` (no id) or ``PUT`` (with id) ``/1/indexes/{name}[/{id}]``."""
        if object_id:
            return await self._request(
                "PUT",
                f"/1/indexes/{index_name}/{object_id}",
                read=False,
                json_body=object_data,
                context=f"save_object({index_name}/{object_id})",
            )
        return await self._request(
            "POST",
            f"/1/indexes/{index_name}",
            read=False,
            json_body=object_data,
            context=f"save_object({index_name})",
        )

    async def save_objects(
        self,
        index_name: str,
        objects: List[Dict[str, Any]],
        action: str = "addObject",
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/batch`` — bulk indexing.

        *action* is one of ``addObject``, ``updateObject``, ``partialUpdateObject``,
        ``partialUpdateObjectNoCreate``, ``deleteObject``.
        """
        body = {
            "requests": [{"action": action, "body": obj} for obj in objects],
        }
        return await self._request(
            "POST",
            f"/1/indexes/{index_name}/batch",
            read=False,
            json_body=body,
            context=f"save_objects({index_name},{action})",
        )

    async def get_object(
        self,
        index_name: str,
        object_id: str,
        attributes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """``GET /1/indexes/{name}/{id}`` with optional attribute projection."""
        params: Optional[Dict[str, Any]] = None
        if attributes:
            params = {"attributesToRetrieve": ",".join(attributes)}
        return await self._request(
            "GET",
            f"/1/indexes/{index_name}/{object_id}",
            read=True,
            params=params,
            context=f"get_object({index_name}/{object_id})",
        )

    async def delete_object(
        self, index_name: str, object_id: str
    ) -> Dict[str, Any]:
        """``DELETE /1/indexes/{name}/{id}``."""
        return await self._request(
            "DELETE",
            f"/1/indexes/{index_name}/{object_id}",
            read=False,
            context=f"delete_object({index_name}/{object_id})",
        )

    async def partial_update_object(
        self,
        index_name: str,
        object_id: str,
        attributes: Dict[str, Any],
        create_if_not_exists: bool = True,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/{id}/partial?createIfNotExists=<bool>``."""
        params = {"createIfNotExists": "true" if create_if_not_exists else "false"}
        return await self._request(
            "POST",
            f"/1/indexes/{index_name}/{object_id}/partial",
            read=False,
            params=params,
            json_body=attributes,
            context=f"partial_update_object({index_name}/{object_id})",
        )

    # ── Browse / Search ────────────────────────────────────────────────────

    async def browse_index(
        self,
        index_name: str,
        *,
        cursor: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/browse`` — cursor-paginated full export.

        First call: omit *cursor*. Subsequent calls: pass the ``cursor`` from
        the previous response. Iteration ends when the response omits ``cursor``.
        """
        body: Dict[str, Any] = {}
        if cursor:
            body["cursor"] = cursor
        if params:
            body.update(params)
        return await self._request(
            "POST",
            f"/1/indexes/{index_name}/browse",
            read=True,
            json_body=body,
            context=f"browse_index({index_name})",
        )

    async def search_index(
        self,
        index_name: str,
        query: str,
        *,
        filters: Optional[str] = None,
        hits_per_page: int = 20,
        page: int = 0,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/query`` — single-index keyword search."""
        body: Dict[str, Any] = {
            "query": query,
            "hitsPerPage": hits_per_page,
            "page": page,
        }
        if filters:
            body["filters"] = filters
        if extra:
            body.update(extra)
        return await self._request(
            "POST",
            f"/1/indexes/{index_name}/query",
            read=True,
            json_body=body,
            context=f"search_index({index_name})",
        )

    async def multi_search(
        self, requests: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """``POST /1/indexes/*/queries`` — federated multi-index search."""
        return await self._request(
            "POST",
            "/1/indexes/*/queries",
            read=True,
            json_body={"requests": requests},
            context="multi_search",
        )

    # ── Synonyms ───────────────────────────────────────────────────────────

    async def list_synonyms(
        self,
        index_name: str,
        *,
        query: str = "",
        type: Optional[str] = None,
        page: int = 0,
        hits_per_page: int = 100,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/synonyms/search``."""
        body: Dict[str, Any] = {
            "query": query,
            "page": page,
            "hitsPerPage": hits_per_page,
        }
        if type:
            body["type"] = type
        return await self._request(
            "POST",
            f"/1/indexes/{index_name}/synonyms/search",
            read=True,
            json_body=body,
            context=f"list_synonyms({index_name})",
        )

    async def save_synonym(
        self,
        index_name: str,
        synonym_id: str,
        synonym: Dict[str, Any],
        forward_to_replicas: bool = False,
    ) -> Dict[str, Any]:
        """``PUT /1/indexes/{name}/synonyms/{id}?forwardToReplicas=<bool>``."""
        params = {
            "forwardToReplicas": "true" if forward_to_replicas else "false"
        }
        return await self._request(
            "PUT",
            f"/1/indexes/{index_name}/synonyms/{synonym_id}",
            read=False,
            params=params,
            json_body=synonym,
            context=f"save_synonym({index_name}/{synonym_id})",
        )

    # ── Rules ──────────────────────────────────────────────────────────────

    async def list_rules(
        self,
        index_name: str,
        *,
        query: str = "",
        page: int = 0,
        hits_per_page: int = 100,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/rules/search``."""
        body: Dict[str, Any] = {
            "query": query,
            "page": page,
            "hitsPerPage": hits_per_page,
        }
        return await self._request(
            "POST",
            f"/1/indexes/{index_name}/rules/search",
            read=True,
            json_body=body,
            context=f"list_rules({index_name})",
        )

    async def save_rule(
        self,
        index_name: str,
        rule_id: str,
        rule: Dict[str, Any],
        forward_to_replicas: bool = False,
    ) -> Dict[str, Any]:
        """``PUT /1/indexes/{name}/rules/{id}?forwardToReplicas=<bool>``."""
        params = {
            "forwardToReplicas": "true" if forward_to_replicas else "false"
        }
        return await self._request(
            "PUT",
            f"/1/indexes/{index_name}/rules/{rule_id}",
            read=False,
            params=params,
            json_body=rule,
            context=f"save_rule({index_name}/{rule_id})",
        )

    # ── Task polling ───────────────────────────────────────────────────────

    async def get_task(
        self, index_name: str, task_id: int
    ) -> Dict[str, Any]:
        """``GET /1/indexes/{name}/task/{id}`` — fetch indexing task status."""
        return await self._request(
            "GET",
            f"/1/indexes/{index_name}/task/{task_id}",
            read=True,
            context=f"get_task({index_name}/{task_id})",
        )
