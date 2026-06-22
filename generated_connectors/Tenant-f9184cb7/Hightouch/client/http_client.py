"""All Hightouch HTTP calls — zero business logic, zero normalization.

Auth surface: single Bearer ``api_token`` against
``https://api.hightouch.com/api/v1``.

Retry on 429 / 5xx with exponential backoff + jitter.
"""
import asyncio
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    HightouchAuthError,
    HightouchBadRequestError,
    HightouchConflictError,
    HightouchError,
    HightouchNotFoundError,
    HightouchRateLimitError,
    HightouchServerError,
)

logger = structlog.get_logger(__name__)

_HIGHTOUCH_BASE = "https://api.hightouch.com/api/v1"

# Retry tuning — kept in one place (OCP).
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_TIMEOUT_S: float = 30.0


class HightouchHTTPClient:
    """Thin async HTTP client for the Hightouch REST API.

    All public methods are awaitable and return raw response dicts. Auth +
    retry are owned here — the connector layer only orchestrates business
    calls.
    """

    def __init__(
        self,
        api_token: str = "",
        base_url: str = _HIGHTOUCH_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._api_token = api_token or ""
        self._base_url = (base_url or _HIGHTOUCH_BASE).rstrip("/")
        self._timeout_s = timeout_s

    # ── auth header builder ──────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        if not self._api_token:
            raise HightouchAuthError(
                "api_token is required for Hightouch API calls"
            )
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── error mapping ────────────────────────────────────────────────────────

    def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
        resource_id: str = "",
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
                or body.get("detail")
                or body.get("details")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        resp_body = body if isinstance(body, dict) else {"raw": body}

        if status == 400:
            raise HightouchBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=resp_body,
            )
        if status in (401, 403):
            raise HightouchAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=resp_body,
            )
        if status == 404:
            raise HightouchNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=resp_body,
                resource_id=resource_id,
            )
        if status == 409:
            raise HightouchConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=resp_body,
            )
        if status == 429:
            raise HightouchRateLimitError(
                f"429 Rate limit exceeded{ctx}",
                response_body=resp_body,
            )
        if 500 <= status < 600:
            raise HightouchServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=resp_body,
            )
        raise HightouchError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=resp_body,
        )

    # ── core request with retry on 429 / 5xx ─────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
        resource_id: str = "",
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> Dict[str, Any]:
        """Perform an HTTP request and return parsed JSON.

        Retries on 429 (rate limit) and 5xx (transient server error) with
        exponential backoff + jitter. All other errors raise immediately via
        ``_raise_for_status``.
        """
        headers = self._headers()
        url = f"{self._base_url}{path}"

        last_exc: Exception = RuntimeError("request never executed")
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    resp = await client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
                # Retry on transient statuses BEFORE raising.
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    if attempt < max_retries:
                        delay = min(
                            RETRY_DELAY_S * (BACKOFF_FACTOR ** attempt)
                            + random.uniform(0, 0.5),
                            MAX_RETRY_DELAY_S,
                        )
                        logger.warning(
                            "hightouch.http.retry",
                            status=resp.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                self._raise_for_status(resp, context=context, resource_id=resource_id)
                if resp.status_code == 204 or not resp.content:
                    return {}
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = HightouchServerError(
                    f"network error{': ' + context if context else ''}: {exc}",
                )
                if attempt < max_retries:
                    delay = min(
                        RETRY_DELAY_S * (BACKOFF_FACTOR ** attempt)
                        + random.uniform(0, 0.5),
                        MAX_RETRY_DELAY_S,
                    )
                    logger.warning(
                        "hightouch.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        error=str(exc),
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise last_exc

        # Fell through after retries — surface a network error.
        raise HightouchServerError(
            f"max retries exhausted for {method} {path}",
            status_code=0,
        )

    # ── Workspaces ───────────────────────────────────────────────────────────

    async def list_workspaces(self) -> Dict[str, Any]:
        """GET /workspaces — list workspaces accessible to the API token."""
        return await self._request(
            "GET",
            "/workspaces",
            context="list_workspaces",
        )

    # ── Sources ──────────────────────────────────────────────────────────────

    async def list_sources(
        self,
        page: int = 1,
        per_page: int = 50,
        slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /sources — list workspace sources."""
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if slug:
            params["slug"] = slug
        return await self._request(
            "GET",
            "/sources",
            params=params,
            context="list_sources",
        )

    async def get_source(self, source_id: Any) -> Dict[str, Any]:
        """GET /sources/{id}."""
        sid = str(source_id)
        return await self._request(
            "GET",
            f"/sources/{sid}",
            context=f"get_source({sid})",
            resource_id=sid,
        )

    # ── Models ───────────────────────────────────────────────────────────────

    async def list_models(
        self,
        page: int = 1,
        per_page: int = 50,
        slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /models — list workspace models."""
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if slug:
            params["slug"] = slug
        return await self._request(
            "GET",
            "/models",
            params=params,
            context="list_models",
        )

    async def get_model(self, model_id: Any) -> Dict[str, Any]:
        """GET /models/{id}."""
        mid = str(model_id)
        return await self._request(
            "GET",
            f"/models/{mid}",
            context=f"get_model({mid})",
            resource_id=mid,
        )

    # ── Destinations ─────────────────────────────────────────────────────────

    async def list_destinations(
        self,
        page: int = 1,
        per_page: int = 50,
        slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /destinations — list workspace destinations."""
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if slug:
            params["slug"] = slug
        return await self._request(
            "GET",
            "/destinations",
            params=params,
            context="list_destinations",
        )

    async def get_destination(self, destination_id: Any) -> Dict[str, Any]:
        """GET /destinations/{id}."""
        did = str(destination_id)
        return await self._request(
            "GET",
            f"/destinations/{did}",
            context=f"get_destination({did})",
            resource_id=did,
        )

    # ── Syncs ────────────────────────────────────────────────────────────────

    async def list_syncs(
        self,
        page: int = 1,
        per_page: int = 50,
        slug: Optional[str] = None,
        model_id: Optional[Any] = None,
        destination_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """GET /syncs — list workspace syncs (optionally filtered)."""
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if slug:
            params["slug"] = slug
        if model_id is not None:
            params["modelId"] = model_id
        if destination_id is not None:
            params["destinationId"] = destination_id
        return await self._request(
            "GET",
            "/syncs",
            params=params,
            context="list_syncs",
        )

    async def get_sync(self, sync_id: Any) -> Dict[str, Any]:
        """GET /syncs/{id}."""
        sid = str(sync_id)
        return await self._request(
            "GET",
            f"/syncs/{sid}",
            context=f"get_sync({sid})",
            resource_id=sid,
        )

    async def create_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /syncs — create a new sync."""
        return await self._request(
            "POST",
            "/syncs",
            json_body=payload,
            context="create_sync",
        )

    async def run_sync(
        self,
        sync_id: Any,
        full_resync: bool = False,
    ) -> Dict[str, Any]:
        """POST /syncs/{id}/trigger — kick off a sync run."""
        sid = str(sync_id)
        return await self._request(
            "POST",
            f"/syncs/{sid}/trigger",
            json_body={"fullResync": bool(full_resync)},
            context=f"run_sync({sid})",
            resource_id=sid,
        )

    # ── Sync Runs ────────────────────────────────────────────────────────────

    async def list_sync_runs(
        self,
        sync_id: Any,
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """GET /syncs/{id}/runs — list run history for a sync."""
        sid = str(sync_id)
        return await self._request(
            "GET",
            f"/syncs/{sid}/runs",
            params={"page": page, "per_page": per_page},
            context=f"list_sync_runs({sid})",
            resource_id=sid,
        )

    async def get_sync_run(
        self,
        sync_id: Any,
        run_id: Any,
    ) -> Dict[str, Any]:
        """GET /syncs/{sid}/runs/{rid}."""
        sid = str(sync_id)
        rid = str(run_id)
        return await self._request(
            "GET",
            f"/syncs/{sid}/runs/{rid}",
            context=f"get_sync_run({sid},{rid})",
            resource_id=rid,
        )

    # ── Sequences ────────────────────────────────────────────────────────────

    async def list_sequences(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """GET /sequences — list workspace sequences."""
        return await self._request(
            "GET",
            "/sequences",
            params={"page": page, "per_page": per_page},
            context="list_sequences",
        )

    # ── Events ───────────────────────────────────────────────────────────────

    async def send_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """POST /events — forward a single Customer Studio event."""
        return await self._request(
            "POST",
            "/events",
            json_body=event,
            context="send_event",
        )
