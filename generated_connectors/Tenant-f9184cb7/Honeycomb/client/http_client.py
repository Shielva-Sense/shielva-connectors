"""All Honeycomb API HTTP calls — zero business logic, zero normalization.

Honeycomb's wire contract:
    base:   https://api.honeycomb.io/1   (us)
            https://api.eu1.honeycomb.io/1   (eu)
    auth:   X-Honeycomb-Team: <api_key>     (NOT Authorization: Bearer ...)
    body:   JSON in / JSON out for management; the events ingest endpoint
            takes a single JSON object per event row.

Retries 429 + 5xx + transient httpx errors with exponential backoff + jitter,
honoring `Retry-After` when Honeycomb provides it.
"""
import asyncio
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    HoneycombAuthError,
    HoneycombBadRequestError,
    HoneycombConflictError,
    HoneycombError,
    HoneycombNetworkError,
    HoneycombNotFoundError,
    HoneycombRateLimitError,
    HoneycombServerError,
)

logger = structlog.get_logger(__name__)

_HONEYCOMB_BASE_US = "https://api.honeycomb.io/1"
_HONEYCOMB_BASE_EU = "https://api.eu1.honeycomb.io/1"

# Retry tunables (OCP — change here, nowhere else)
_DEFAULT_MAX_RETRIES = 3
_BASE_DELAY_S = 1.0
_MAX_DELAY_S = 32.0
_BACKOFF_FACTOR = 2.0
_REQUEST_TIMEOUT_S = 30.0


def base_url_for_region(region: str) -> str:
    """Return the canonical Honeycomb base URL for `region` ('us' or 'eu').

    Anything other than 'eu' (case-insensitive) falls through to the US base.
    """
    if (region or "").strip().lower() == "eu":
        return _HONEYCOMB_BASE_EU
    return _HONEYCOMB_BASE_US


class HoneycombHTTPClient:
    """Thin async HTTP client for the Honeycomb REST API.

    All methods raise the connector-local exception hierarchy on error and
    return raw dict/list payloads on success. Retries on 429 / 5xx / transient
    httpx errors with exponential backoff + jitter, capped at `_MAX_DELAY_S`.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = _HONEYCOMB_BASE_US,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        timeout_s: float = _REQUEST_TIMEOUT_S,
    ):
        self._api_key = api_key or ""
        self._base_url = (base_url or _HONEYCOMB_BASE_US).rstrip("/")
        self._max_retries = max(0, int(max_retries))
        self._timeout = httpx.Timeout(timeout_s)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        """The Honeycomb-mandated header set.

        `X-Honeycomb-Team` carries the api_key; we deliberately do NOT send
        `Authorization` so a stray Bearer header from middleware can't override
        the team key.
        """
        return {
            "X-Honeycomb-Team": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _parse_json(response: httpx.Response) -> Any:
        if not response.content:
            return {}
        try:
            return response.json()
        except Exception:
            return {}

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return
        body = self._parse_json(response)
        if isinstance(body, dict):
            message = (
                body.get("error")
                or body.get("message")
                or body.get("error_description")
                or ""
            )
        else:
            message = ""
        message = str(message) or response.text or ""
        ctx = f" ({context})" if context else ""

        if status in (401, 403):
            raise HoneycombAuthError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 400:
            raise HoneycombBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 404:
            raise HoneycombNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 409:
            raise HoneycombConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After") or response.headers.get(
                "retry-after"
            )
            try:
                retry_after_s = float(retry_after) if retry_after else 5.0
            except (TypeError, ValueError):
                retry_after_s = 5.0
            raise HoneycombRateLimitError(
                f"429 Rate limit exceeded{ctx}: {message}",
                status_code=429,
                response_body=body if isinstance(body, dict) else {},
                retry_after_s=retry_after_s,
            )
        if status >= 500:
            raise HoneycombServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {},
            )
        raise HoneycombError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        context: str = "",
    ) -> Any:
        """Send a request, retrying on 429 / 5xx / transient httpx errors."""
        url = f"{self._base_url}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                        json=json_body,
                    )
                # Retry on 429 / 5xx, otherwise raise or return
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self._max_retries:
                        delay = self._compute_delay(response, attempt)
                        logger.warning(
                            "honeycomb.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                self._raise_for_status(response, context=context)
                return self._parse_json(response)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    delay = min(
                        _BASE_DELAY_S * (_BACKOFF_FACTOR ** attempt)
                        + random.uniform(0, 0.5),
                        _MAX_DELAY_S,
                    )
                    logger.warning(
                        "honeycomb.network_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        error=str(exc),
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise HoneycombNetworkError(
                    f"Network error{(' (' + context + ')') if context else ''}: {exc}",
                    status_code=0,
                ) from exc

        # Exhausted retries — surface the last error
        if last_exc is not None:
            raise HoneycombNetworkError(str(last_exc))
        raise HoneycombError(
            f"Exhausted retries{(' (' + context + ')') if context else ''}"
        )

    @staticmethod
    def _compute_delay(response: httpx.Response, attempt: int) -> float:
        """Compute the delay before the next retry, honoring Retry-After."""
        retry_after = response.headers.get("Retry-After") or response.headers.get(
            "retry-after"
        )
        if retry_after:
            try:
                return min(float(retry_after), _MAX_DELAY_S)
            except (TypeError, ValueError):
                pass
        return min(
            _BASE_DELAY_S * (_BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
            _MAX_DELAY_S,
        )

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def get_auth(self) -> Dict[str, Any]:
        """GET /auth — returns {api_key_access, team, environment}."""
        return await self._request("GET", "/auth", context="get_auth")

    # ── Datasets ──────────────────────────────────────────────────────────────

    async def list_datasets(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/datasets", context="list_datasets")

    async def get_dataset(self, dataset_slug: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/datasets/{dataset_slug}",
            context=f"get_dataset({dataset_slug})",
        )

    async def create_dataset(
        self,
        name: str,
        description: str = "",
        expand_json_depth: int = 0,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        if expand_json_depth:
            body["expand_json_depth"] = expand_json_depth
        return await self._request(
            "POST", "/datasets", json_body=body, context="create_dataset"
        )

    # ── Columns ───────────────────────────────────────────────────────────────

    async def list_columns(self, dataset_slug: str) -> List[Dict[str, Any]]:
        return await self._request(
            "GET",
            f"/datasets/{dataset_slug}/columns",
            context=f"list_columns({dataset_slug})",
        )

    # ── Queries ───────────────────────────────────────────────────────────────

    async def list_queries(self, dataset_slug: str) -> List[Dict[str, Any]]:
        return await self._request(
            "GET",
            f"/queries/{dataset_slug}",
            context=f"list_queries({dataset_slug})",
        )

    async def create_query(
        self,
        dataset_slug: str,
        breakdowns: Optional[List[str]] = None,
        calculations: Optional[List[Dict[str, Any]]] = None,
        filters: Optional[List[Dict[str, Any]]] = None,
        time_range: int = 7200,
        granularity: Optional[int] = None,
        orders: Optional[List[Dict[str, Any]]] = None,
        having: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "breakdowns": list(breakdowns or []),
            "calculations": list(calculations or []),
            "filters": list(filters or []),
            "time_range": int(time_range),
        }
        if granularity is not None:
            body["granularity"] = int(granularity)
        if orders:
            body["orders"] = list(orders)
        if having:
            body["having"] = list(having)
        return await self._request(
            "POST",
            f"/queries/{dataset_slug}",
            json_body=body,
            context=f"create_query({dataset_slug})",
        )

    async def get_query(self, dataset_slug: str, query_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/queries/{dataset_slug}/{query_id}",
            context=f"get_query({dataset_slug}/{query_id})",
        )

    async def run_query(
        self,
        dataset_slug: str,
        query_id: str,
        disable_series: bool = False,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        """Alias for run_query_result — kept for parity with the public API spec."""
        return await self.run_query_result(
            dataset_slug=dataset_slug,
            query_id=query_id,
            disable_series=disable_series,
            limit=limit,
        )

    # ── Query results ─────────────────────────────────────────────────────────

    async def run_query_result(
        self,
        dataset_slug: str,
        query_id: str,
        disable_series: bool = False,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        body = {
            "query_id": query_id,
            "disable_series": bool(disable_series),
            "limit": int(limit),
        }
        return await self._request(
            "POST",
            f"/query_results/{dataset_slug}",
            json_body=body,
            context=f"run_query_result({dataset_slug})",
        )

    async def get_query_result(
        self, dataset_slug: str, result_id: str
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/query_results/{dataset_slug}/{result_id}",
            context=f"get_query_result({dataset_slug}/{result_id})",
        )

    # ── Markers ───────────────────────────────────────────────────────────────

    async def list_markers(self, dataset_slug: str) -> List[Dict[str, Any]]:
        return await self._request(
            "GET",
            f"/markers/{dataset_slug}",
            context=f"list_markers({dataset_slug})",
        )

    async def create_marker(
        self,
        dataset_slug: str,
        message: str,
        type: str = "deploy",
        url: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"message": message, "type": type}
        if url:
            body["url"] = url
        if start_time is not None:
            body["start_time"] = int(start_time)
        if end_time is not None:
            body["end_time"] = int(end_time)
        return await self._request(
            "POST",
            f"/markers/{dataset_slug}",
            json_body=body,
            context=f"create_marker({dataset_slug})",
        )

    # ── Triggers ──────────────────────────────────────────────────────────────

    async def list_triggers(self, dataset_slug: str) -> List[Dict[str, Any]]:
        return await self._request(
            "GET",
            f"/triggers/{dataset_slug}",
            context=f"list_triggers({dataset_slug})",
        )

    async def create_trigger(
        self,
        dataset_slug: str,
        name: str,
        query_id: str,
        threshold: Dict[str, Any],
        frequency: int = 900,
        alert_type: str = "on_change",
        recipients: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "name": name,
            "query_id": query_id,
            "threshold": threshold,
            "frequency": int(frequency),
            "alert_type": alert_type,
            "recipients": list(recipients or []),
        }
        return await self._request(
            "POST",
            f"/triggers/{dataset_slug}",
            json_body=body,
            context=f"create_trigger({dataset_slug})",
        )

    # ── Boards ────────────────────────────────────────────────────────────────

    async def list_boards(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/boards", context="list_boards")

    async def get_board(self, board_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/boards/{board_id}",
            context=f"get_board({board_id})",
        )

    async def create_board(
        self,
        name: str,
        description: str = "",
        style: str = "list",
        queries: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "style": style,
            "queries": list(queries or []),
        }
        return await self._request(
            "POST", "/boards", json_body=body, context="create_board"
        )

    # ── SLOs ──────────────────────────────────────────────────────────────────

    async def list_slos(self, dataset_slug: str) -> List[Dict[str, Any]]:
        return await self._request(
            "GET", f"/slos/{dataset_slug}", context=f"list_slos({dataset_slug})"
        )

    # ── Recipients ────────────────────────────────────────────────────────────

    async def list_recipients(self) -> List[Dict[str, Any]]:
        """GET /recipients — list notification destinations (email / Slack / webhook)."""
        return await self._request("GET", "/recipients", context="list_recipients")

    # ── Events (ingest) ───────────────────────────────────────────────────────

    async def send_event(
        self,
        dataset_slug: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /events/{dataset_slug} — ingest a single event row.

        Honeycomb's ingest path is `/1/events/{dataset_slug}`. The body is the
        event payload itself (free-form JSON); Honeycomb stores each posted
        object as one row, indexing every key as a column.
        """
        return await self._request(
            "POST",
            f"/events/{dataset_slug}",
            json_body=event,
            context=f"send_event({dataset_slug})",
        )
