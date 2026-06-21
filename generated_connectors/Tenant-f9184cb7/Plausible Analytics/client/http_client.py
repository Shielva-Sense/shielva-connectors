"""All Plausible Analytics HTTP calls — zero business logic, zero normalization.

The Plausible API splits into three surfaces, all spoken here:

  • Stats API   — GET {base_url}/stats/...  (Bearer-auth required)
  • Sites API   — GET/POST/PUT/DELETE {base_url}/sites/...  (Bearer-auth required)
  • Events API  — POST {base_url}/events  (NO Bearer; user-agent + body only)

This client owns:
  • httpx.AsyncClient lifecycle
  • Auth-header injection for Stats/Sites
  • Status → exception mapping
  • Retry on 429 + 5xx with exponential backoff
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, List, Mapping, Optional

import httpx
import structlog

from exceptions import (
    PlausibleAPIError,
    PlausibleAuthError,
    PlausibleNetworkError,
    PlausibleNotFound,
    PlausibleRateLimitError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BASE = "https://plausible.io/api/v1"

# Retry tuning — change here, nowhere else
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BASE_DELAY_S = 0.5
_BACKOFF_FACTOR = 2.0
_MAX_DELAY_S = 16.0


class PlausibleHTTPClient:
    """Async HTTP client for the Plausible Analytics API.

    Every method returns either the parsed JSON dict or — for the events
    endpoint, which returns 202 with no body — an empty dict on success.
    Retry is built in (caller does NOT need to wrap with_retry()).
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE,
        api_key: str = "",
        timeout_s: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s

    # ── Header builders ────────────────────────────────────────────────────

    def _bearer_headers(self) -> Dict[str, str]:
        """Headers for Stats + Sites API (Bearer-auth required)."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _event_headers(self, user_agent: str) -> Dict[str, str]:
        """Headers for the Events API (no Bearer auth — anonymous endpoint)."""
        return {
            "User-Agent": user_agent,
            "Content-Type": "application/json",
        }

    # ── Status → exception mapping ─────────────────────────────────────────

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
            body = {}

        # Plausible returns either {"error": "..."} or a richer object
        err_field = body.get("error") if isinstance(body, dict) else None
        if isinstance(err_field, dict):
            message = err_field.get("message", "") or str(body)
        else:
            message = str(err_field) if err_field else str(body)

        ctx = f": {context}" if context else ""
        if status == 401:
            raise PlausibleAuthError(f"401 Unauthorized{ctx}: {message}")
        if status == 404:
            raise PlausibleNotFound(f"404 Not Found{ctx}: {message}")
        if status == 429:
            raise PlausibleRateLimitError(f"429 Rate limit exceeded{ctx}")
        if 500 <= status < 600:
            raise PlausibleNetworkError(f"HTTP {status}{ctx}: {message}")
        raise PlausibleAPIError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {},
        )

    # ── Retry envelope ─────────────────────────────────────────────────────

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        context: str = "",
    ) -> httpx.Response:
        """Issue a single HTTP call with retry on 429 / 5xx / transport errors."""
        last_exc: Optional[BaseException] = None
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    resp = await client.request(
                        method,
                        url,
                        headers=dict(headers),
                        params=dict(params) if params else None,
                        json=dict(json_body) if json_body else None,
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == _MAX_RETRIES:
                        break
                    await asyncio.sleep(self._backoff(attempt))
                    continue

                if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    retry_after = self._parse_retry_after(resp)
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                    logger.warning(
                        "plausible.retry",
                        status=resp.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                return resp

        # Transport failure exhausted retries
        raise PlausibleNetworkError(f"Network error{f': {context}' if context else ''}: {last_exc}")

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(
            _BASE_DELAY_S * (_BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
            _MAX_DELAY_S,
        )

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> Optional[float]:
        raw = resp.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    # ── Stats API ──────────────────────────────────────────────────────────

    async def get_realtime_visitors(self, site_id: str) -> Dict[str, Any]:
        """GET /stats/realtime/visitors?site_id=...

        Plausible returns the integer count as a JSON-encoded number, not an
        object. We wrap it in a dict for consistent typing.
        """
        url = f"{self._base_url}/stats/realtime/visitors"
        params = {"site_id": site_id}
        resp = await self._request_with_retry(
            "GET",
            url,
            headers=self._bearer_headers(),
            params=params,
            context="get_realtime_visitors",
        )
        await self._raise_for_status(resp, "get_realtime_visitors")
        try:
            body = resp.json()
        except Exception:
            body = 0
        if isinstance(body, dict):
            return body
        # Integer payload — wrap.
        return {"visitors": int(body or 0)}

    async def get_aggregate(
        self,
        site_id: str,
        period: str = "30d",
        date: Optional[str] = None,
        metrics: Optional[List[str]] = None,
        filters: Optional[str] = None,
        compare: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /stats/aggregate — returns {results: {metric_name: {value, change?}}}."""
        url = f"{self._base_url}/stats/aggregate"
        params: Dict[str, Any] = {
            "site_id": site_id,
            "period": period,
            "metrics": ",".join(metrics or ["visitors"]),
        }
        if date:
            params["date"] = date
        if filters:
            params["filters"] = filters
        if compare:
            params["compare"] = compare
        resp = await self._request_with_retry(
            "GET",
            url,
            headers=self._bearer_headers(),
            params=params,
            context="get_aggregate",
        )
        await self._raise_for_status(resp, "get_aggregate")
        return resp.json()

    async def get_timeseries(
        self,
        site_id: str,
        period: str = "30d",
        date: Optional[str] = None,
        metrics: Optional[List[str]] = None,
        filters: Optional[str] = None,
        interval: str = "date",
    ) -> Dict[str, Any]:
        """GET /stats/timeseries — returns {results: [{date, metric, ...}, ...]}."""
        url = f"{self._base_url}/stats/timeseries"
        params: Dict[str, Any] = {
            "site_id": site_id,
            "period": period,
            "metrics": ",".join(metrics or ["visitors"]),
            "interval": interval,
        }
        if date:
            params["date"] = date
        if filters:
            params["filters"] = filters
        resp = await self._request_with_retry(
            "GET",
            url,
            headers=self._bearer_headers(),
            params=params,
            context="get_timeseries",
        )
        await self._raise_for_status(resp, "get_timeseries")
        return resp.json()

    async def get_breakdown(
        self,
        site_id: str,
        period: str = "30d",
        date: Optional[str] = None,
        property: str = "event:page",
        metrics: Optional[List[str]] = None,
        filters: Optional[str] = None,
        page: int = 1,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /stats/breakdown — paginated property/metric breakdown."""
        url = f"{self._base_url}/stats/breakdown"
        params: Dict[str, Any] = {
            "site_id": site_id,
            "period": period,
            "property": property,
            "metrics": ",".join(metrics or ["visitors"]),
            "page": page,
            "limit": limit,
        }
        if date:
            params["date"] = date
        if filters:
            params["filters"] = filters
        resp = await self._request_with_retry(
            "GET",
            url,
            headers=self._bearer_headers(),
            params=params,
            context="get_breakdown",
        )
        await self._raise_for_status(resp, "get_breakdown")
        return resp.json()

    # ── Events API (no Bearer auth) ────────────────────────────────────────

    async def post_event(
        self,
        domain: str,
        name: str,
        url: str,
        user_agent: str = "Shielva/1.0",
        referrer: Optional[str] = None,
        screen_width: Optional[int] = None,
        props: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /events — record a pageview or custom event.

        The events endpoint is the only Plausible surface that does NOT
        authenticate via the API key; identity is derived from the User-Agent
        header. Plausible returns 202 with an empty body on success.
        """
        endpoint = f"{self._base_url}/events"
        body: Dict[str, Any] = {"name": name, "url": url, "domain": domain}
        if referrer is not None:
            body["referrer"] = referrer
        if screen_width is not None:
            body["screen_width"] = screen_width
        if props is not None:
            body["props"] = props
        resp = await self._request_with_retry(
            "POST",
            endpoint,
            headers=self._event_headers(user_agent),
            json_body=body,
            context=f"post_event({name})",
        )
        await self._raise_for_status(resp, f"post_event({name})")
        # 202 Accepted typically has an empty body.
        try:
            return resp.json() if resp.content else {"accepted": True}
        except Exception:
            return {"accepted": True}

    # ── Sites Provisioning API ─────────────────────────────────────────────

    async def list_sites(self) -> Dict[str, Any]:
        """GET /sites — list the sites visible to the API key."""
        url = f"{self._base_url}/sites"
        resp = await self._request_with_retry(
            "GET",
            url,
            headers=self._bearer_headers(),
            context="list_sites",
        )
        await self._raise_for_status(resp, "list_sites")
        return resp.json()

    async def get_site(self, site_id: str) -> Dict[str, Any]:
        """GET /sites/{site_id}."""
        url = f"{self._base_url}/sites/{site_id}"
        resp = await self._request_with_retry(
            "GET",
            url,
            headers=self._bearer_headers(),
            context=f"get_site({site_id})",
        )
        await self._raise_for_status(resp, f"get_site({site_id})")
        return resp.json()

    async def create_site(self, domain: str, timezone: str = "UTC") -> Dict[str, Any]:
        """POST /sites — provision a new tracked site."""
        url = f"{self._base_url}/sites"
        body = {"domain": domain, "timezone": timezone}
        resp = await self._request_with_retry(
            "POST",
            url,
            headers=self._bearer_headers(),
            json_body=body,
            context=f"create_site({domain})",
        )
        await self._raise_for_status(resp, f"create_site({domain})")
        return resp.json()

    async def update_site(
        self,
        site_id: str,
        timezone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PUT /sites/{site_id} — update site fields (timezone is the only writeable one today)."""
        url = f"{self._base_url}/sites/{site_id}"
        body: Dict[str, Any] = {}
        if timezone is not None:
            body["timezone"] = timezone
        resp = await self._request_with_retry(
            "PUT",
            url,
            headers=self._bearer_headers(),
            json_body=body,
            context=f"update_site({site_id})",
        )
        await self._raise_for_status(resp, f"update_site({site_id})")
        return resp.json()

    async def delete_site(self, site_id: str) -> Dict[str, Any]:
        """DELETE /sites/{site_id}."""
        url = f"{self._base_url}/sites/{site_id}"
        resp = await self._request_with_retry(
            "DELETE",
            url,
            headers=self._bearer_headers(),
            context=f"delete_site({site_id})",
        )
        await self._raise_for_status(resp, f"delete_site({site_id})")
        try:
            return resp.json() if resp.content else {"deleted": True}
        except Exception:
            return {"deleted": True}

    # ── Goals (Sites API extension) ────────────────────────────────────────

    async def list_goals(self, site_id: str) -> Dict[str, Any]:
        """GET /sites/{site_id}/goals."""
        url = f"{self._base_url}/sites/{site_id}/goals"
        resp = await self._request_with_retry(
            "GET",
            url,
            headers=self._bearer_headers(),
            context=f"list_goals({site_id})",
        )
        await self._raise_for_status(resp, f"list_goals({site_id})")
        return resp.json()

    async def create_goal(
        self,
        site_id: str,
        goal_type: str,
        event_name: Optional[str] = None,
        page_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /sites/{site_id}/goals — create either an event or page goal."""
        url = f"{self._base_url}/sites/{site_id}/goals"
        body: Dict[str, Any] = {"goal_type": goal_type}
        if event_name is not None:
            body["event_name"] = event_name
        if page_path is not None:
            body["page_path"] = page_path
        resp = await self._request_with_retry(
            "POST",
            url,
            headers=self._bearer_headers(),
            json_body=body,
            context=f"create_goal({site_id})",
        )
        await self._raise_for_status(resp, f"create_goal({site_id})")
        return resp.json()
