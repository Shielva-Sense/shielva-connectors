"""All Loggly API HTTP calls — zero business logic, zero normalization.

httpx async client. Loggly exposes two distinct surfaces:

  • Management / Search (`https://{subdomain}.loggly.com/apiv2`)
      → HTTP Basic auth: Authorization: Basic base64(username:password)
      → JSON in/out

  • Bulk send (`https://logs-01.loggly.com/bulk/{customer_token}/tag/{tag}/`)
      → No header auth (token sits in the URL path)
      → newline-delimited JSON body, up to 5 MB

Retry on 429/5xx with exponential backoff.
"""
import asyncio
import base64
import json
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    LogglyAuthError,
    LogglyBadRequestError,
    LogglyConflictError,
    LogglyError,
    LogglyNotFoundError,
    LogglyRateLimitError,
    LogglyServerError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_INGEST_BASE = "https://logs-01.loggly.com"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class LogglyHTTPClient:
    """Thin async HTTP client for the Loggly REST API.

    All methods are awaitable and return raw response dicts (or `{}` for 204).
    Auth + retry are owned here — the connector layer only orchestrates.
    """

    def __init__(
        self,
        subdomain: str = "",
        username: str = "",
        password: str = "",
        customer_token: str = "",
        base_url: str = "",
        ingest_base_url: str = _DEFAULT_INGEST_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._subdomain = (subdomain or "").strip()
        self._username = username or ""
        self._password = password or ""
        self._customer_token = customer_token or ""
        # Allow `base_url` override; otherwise derive from subdomain.
        self._base_url = (
            (base_url or f"https://{self._subdomain}.loggly.com/apiv2").rstrip("/")
        )
        self._ingest_base_url = (ingest_base_url or _DEFAULT_INGEST_BASE).rstrip("/")
        self._timeout = timeout

    # ── header builders ────────────────────────────────────────────────────

    def _basic_header(self) -> str:
        token = base64.b64encode(
            f"{self._username}:{self._password}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {token}"

    def _mgmt_headers(self) -> Dict[str, str]:
        return {
            "Authorization": self._basic_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _bulk_headers(self) -> Dict[str, str]:
        # Bulk endpoint takes raw NDJSON; auth via URL token.
        return {"Content-Type": "application/json"}

    # ── error mapping ──────────────────────────────────────────────────────

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
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        wrapped = body if isinstance(body, dict) else {"raw": body}
        ctx = f": {context}" if context else ""

        if status == 400:
            raise LogglyBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=wrapped,
            )
        if status in (401, 403):
            raise LogglyAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=wrapped,
            )
        if status == 404:
            raise LogglyNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=wrapped,
            )
        if status == 409:
            raise LogglyConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=wrapped,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after) if retry_after else 5.0
            except ValueError:
                retry_after_s = 5.0
            raise LogglyRateLimitError(
                f"429 Rate Limited{ctx}: {message}",
                retry_after_s=retry_after_s,
            )
        if status >= 500:
            raise LogglyServerError(
                f"{status} Server Error{ctx}: {message}",
                status_code=status,
                response_body=wrapped,
            )
        raise LogglyError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=wrapped,
        )

    # ── transport ──────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        raw_body: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        base: Optional[str] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
        base_url = (base or self._base_url).rstrip("/")
        url = path if path.startswith("http") else f"{base_url}{path}"
        hdrs = headers if headers is not None else self._mgmt_headers()

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=hdrs,
                        params=params,
                        json=json_body if raw_body is None else None,
                        content=raw_body,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        # Honour Retry-After on 429 when present, else exp backoff.
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = float(retry_after) if retry_after else _BACKOFF_BASE * (2 ** attempt)
                        except ValueError:
                            delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "loggly.http.retry",
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
                        "loggly.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LogglyServerError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise LogglyServerError(str(last_exc)) from last_exc
        raise LogglyServerError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── Search ─────────────────────────────────────────────────────────────

    async def search_logs(
        self,
        q: str = "*",
        from_: str = "-24h",
        until: str = "now",
        size: int = 100,
        order: str = "desc",
    ) -> Dict[str, Any]:
        """GET /apiv2/search — RSID-based log search."""
        params: Dict[str, Any] = {
            "q": q,
            "from": from_,
            "until": until,
            "size": size,
            "order": order,
        }
        return await self._request(
            "GET",
            "/search",
            params=params,
            context="search_logs",
        )

    async def get_search_field_stats(
        self,
        field: str,
        q: str = "*",
        from_: str = "-24h",
        until: str = "now",
        facet_size: int = 100,
    ) -> Dict[str, Any]:
        """GET /apiv2/fields/{field} — field-level aggregation."""
        params: Dict[str, Any] = {
            "q": q,
            "from": from_,
            "until": until,
            "facet_size": facet_size,
        }
        return await self._request(
            "GET",
            f"/fields/{field}",
            params=params,
            context=f"get_search_field_stats({field})",
        )

    async def iterate_events(
        self,
        q: str = "*",
        from_: str = "-24h",
        until: str = "now",
        size: int = 100,
        next_: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /apiv2/events/iterate — cursor-paginated event stream."""
        params: Dict[str, Any] = {
            "q": q,
            "from": from_,
            "until": until,
            "size": size,
        }
        if next_:
            params["next"] = next_
        return await self._request(
            "GET",
            "/events/iterate",
            params=params,
            context="iterate_events",
        )

    # ── Saved searches ─────────────────────────────────────────────────────

    async def list_saved_searches(self) -> Dict[str, Any]:
        """GET /apiv2/savedsearches."""
        return await self._request(
            "GET",
            "/savedsearches",
            context="list_saved_searches",
        )

    async def create_saved_search(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /apiv2/savedsearches."""
        return await self._request(
            "POST",
            "/savedsearches",
            json_body=payload,
            context="create_saved_search",
        )

    # ── Alerts ─────────────────────────────────────────────────────────────

    async def list_alerts(self) -> Dict[str, Any]:
        """GET /apiv2/alerts."""
        return await self._request(
            "GET",
            "/alerts",
            context="list_alerts",
        )

    async def create_alert(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /apiv2/alerts."""
        return await self._request(
            "POST",
            "/alerts",
            json_body=payload,
            context="create_alert",
        )

    # ── Dashboards ─────────────────────────────────────────────────────────

    async def list_dashboards(self) -> Dict[str, Any]:
        """GET /apiv2/dashboards."""
        return await self._request(
            "GET",
            "/dashboards",
            context="list_dashboards",
        )

    async def get_dashboard(self, dashboard_id: str) -> Dict[str, Any]:
        """GET /apiv2/dashboards/{id}."""
        return await self._request(
            "GET",
            f"/dashboards/{dashboard_id}",
            context=f"get_dashboard({dashboard_id})",
        )

    # ── Source groups ──────────────────────────────────────────────────────

    async def list_source_groups(self) -> Dict[str, Any]:
        """GET /apiv2/sourcegroups."""
        return await self._request(
            "GET",
            "/sourcegroups",
            context="list_source_groups",
        )

    # ── Users ──────────────────────────────────────────────────────────────

    async def list_users(self) -> Dict[str, Any]:
        """GET /apiv2/users."""
        return await self._request(
            "GET",
            "/users",
            context="list_users",
        )

    # ── Bulk event send ────────────────────────────────────────────────────

    async def send_events_bulk(
        self,
        events: List[Dict[str, Any]],
        tag: str = "bulk",
    ) -> Dict[str, Any]:
        """POST https://logs-01.loggly.com/bulk/{token}/tag/{tag}/ — NDJSON body.

        Raises if `customer_token` was not configured.
        """
        if not self._customer_token:
            raise LogglyError(
                "send_events_bulk requires customer_token install_field to be set",
                status_code=0,
            )
        if not events:
            return {"response": "ok", "sent": 0}
        # Newline-delimited JSON
        ndjson = "\n".join(json.dumps(e, default=str) for e in events)
        path = f"/bulk/{self._customer_token}/tag/{tag}/"
        return await self._request(
            "POST",
            path,
            raw_body=ndjson,
            headers=self._bulk_headers(),
            base=self._ingest_base_url,
            context=f"send_events_bulk(tag={tag},count={len(events)})",
        )
