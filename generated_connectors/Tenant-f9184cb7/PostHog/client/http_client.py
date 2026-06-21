"""All PostHog API HTTP calls — zero business logic, zero normalization.

httpx async client. PostHog has TWO auth modes:

    1. Management API (projects, persons, cohorts, flags, insights, dashboards,
       actions, annotations, experiments, events, query).
       Header: Authorization: Bearer <personal_api_key>

    2. Capture API (/capture/, /batch/).
       NO Authorization header. project_api_key is embedded in the JSON body
       as `api_key`. Same key shipped in posthog-js client bundles.

Retry on 429 (honoring Retry-After) and 5xx with exponential backoff.
"""
import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    PostHogAuthError,
    PostHogBadRequestError,
    PostHogConflictError,
    PostHogError,
    PostHogNetworkError,
    PostHogNotFoundError,
    PostHogRateLimitError,
    PostHogServerError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BASE = "https://app.posthog.com"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds
_RETRYABLE = {429, 500, 502, 503, 504}


class PostHogHTTPClient:
    """Thin async HTTP client for the PostHog REST + Capture APIs.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        personal_api_key: str = "",
        project_api_key: str = "",
        project_id: str = "",
        base_url: str = _DEFAULT_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._personal_api_key = personal_api_key or ""
        self._project_api_key = project_api_key or ""
        self._default_project_id = str(project_id or "")
        self._base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self._timeout = timeout

    def _bearer_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._personal_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _capture_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _project(self, project_id: Optional[str]) -> str:
        pid = str(project_id) if project_id else self._default_project_id
        if not pid:
            raise PostHogError("project_id is required (pass project_id= or set default at install)")
        return pid

    @staticmethod
    def _extract_message(body: Any) -> str:
        if isinstance(body, dict):
            return (
                body.get("detail")
                or body.get("message")
                or body.get("error")
                or body.get("code")
                or str(body)
            )
        return str(body) if body else ""

    def _raise_for_status(
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

        message = self._extract_message(body)
        if not isinstance(message, str):
            message = str(message)
        ctx = f": {context}" if context else ""

        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status == 400:
            raise PostHogBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 401:
            raise PostHogAuthError(
                f"401 Unauthorized{ctx}: {message}",
                status_code=401,
                response_body=body_dict,
            )
        if status == 403:
            raise PostHogAuthError(
                f"403 Forbidden{ctx}: {message}",
                status_code=403,
                response_body=body_dict,
            )
        if status == 404:
            raise PostHogNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise PostHogConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = 5.0
            ra = response.headers.get("Retry-After")
            if ra:
                try:
                    retry_after = float(ra)
                except ValueError:
                    pass
            raise PostHogRateLimitError(
                f"429 Too Many Requests{ctx}: {message}",
                retry_after_s=retry_after,
            )
        if 500 <= status < 600:
            raise PostHogServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise PostHogError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    @staticmethod
    def _compute_backoff(response: Optional[httpx.Response], attempt: int) -> float:
        if response is not None:
            ra = response.headers.get("Retry-After") if response.headers else None
            if ra:
                try:
                    return max(float(ra), 0.0)
                except ValueError:
                    pass
        return _BACKOFF_BASE * (2 ** attempt)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        hdrs = headers if headers is not None else self._bearer_headers()

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=hdrs,
                        params=params,
                        json=json_body,
                    )
                if response.status_code in _RETRYABLE and attempt < _MAX_RETRIES - 1:
                    delay = self._compute_backoff(response, attempt)
                    logger.warning(
                        "posthog.http.retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "posthog.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise PostHogNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise PostHogNetworkError(str(last_exc)) from last_exc
        raise PostHogNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Health probe ────────────────────────────────────────────────────────

    async def get_project(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/projects/{project_id} — also used as health probe."""
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}",
            context="get_project",
        )

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /api/users/@me — alternative identity probe."""
        return await self._request(
            "GET",
            "/api/users/@me",
            context="get_current_user",
        )

    async def list_projects(self) -> Dict[str, Any]:
        """GET /api/projects — list all projects visible to the personal key."""
        return await self._request(
            "GET",
            "/api/projects",
            context="list_projects",
        )

    # ── Persons ─────────────────────────────────────────────────────────────

    async def list_persons(
        self,
        project_id: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        pid = self._project(project_id)
        params: Dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        return await self._request(
            "GET",
            f"/api/projects/{pid}/persons",
            params=params,
            context="list_persons",
        )

    async def get_person(
        self,
        person_id: str,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/persons/{person_id}",
            context=f"get_person({person_id})",
        )

    # ── Cohorts ─────────────────────────────────────────────────────────────

    async def list_cohorts(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/cohorts",
            context="list_cohorts",
        )

    async def get_cohort(
        self,
        cohort_id: int,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/cohorts/{cohort_id}",
            context=f"get_cohort({cohort_id})",
        )

    # ── Feature Flags ───────────────────────────────────────────────────────

    async def list_feature_flags(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/feature_flags",
            context="list_feature_flags",
        )

    async def get_feature_flag(
        self,
        flag_id: int,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/feature_flags/{flag_id}",
            context=f"get_feature_flag({flag_id})",
        )

    async def create_feature_flag(
        self,
        key: str,
        name: str,
        filters: Optional[Dict[str, Any]] = None,
        active: bool = True,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pid = self._project(project_id)
        body: Dict[str, Any] = {
            "key": key,
            "name": name,
            "active": active,
            "filters": filters or {"groups": [{"properties": [], "rollout_percentage": 100}]},
        }
        return await self._request(
            "POST",
            f"/api/projects/{pid}/feature_flags",
            json_body=body,
            context=f"create_feature_flag({key})",
        )

    # ── Insights / Dashboards / Actions / Annotations / Experiments ─────────

    async def list_insights(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/insights",
            context="list_insights",
        )

    async def list_dashboards(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/dashboards",
            context="list_dashboards",
        )

    async def get_dashboard(
        self,
        dashboard_id: int,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/dashboards/{dashboard_id}",
            context=f"get_dashboard({dashboard_id})",
        )

    async def list_actions(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/actions",
            context="list_actions",
        )

    async def list_annotations(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/annotations",
            context="list_annotations",
        )

    async def list_experiments(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "GET",
            f"/api/projects/{pid}/experiments",
            context="list_experiments",
        )

    # ── Events + HogQL Query ────────────────────────────────────────────────

    async def list_events(
        self,
        project_id: Optional[str] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        pid = self._project(project_id)
        params: Dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        return await self._request(
            "GET",
            f"/api/projects/{pid}/events",
            params=params,
            context="list_events",
        )

    async def run_query(
        self,
        query: Dict[str, Any],
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pid = self._project(project_id)
        return await self._request(
            "POST",
            f"/api/projects/{pid}/query",
            json_body={"query": query},
            context="run_query",
        )

    # ── Capture (project_api_key in body, no Authorization header) ─────────

    async def capture(
        self,
        distinct_id: str,
        event: str,
        properties: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "api_key": self._project_api_key,
            "event": event,
            "distinct_id": distinct_id,
            "properties": properties or {},
        }
        if timestamp:
            body["timestamp"] = timestamp
        return await self._request(
            "POST",
            "/capture/",
            headers=self._capture_headers(),
            json_body=body,
            context="capture",
        )

    async def batch(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /batch/ — events is a list of capture dicts.

        We inject `api_key` at the top level.
        """
        body = {"api_key": self._project_api_key, "batch": events}
        return await self._request(
            "POST",
            "/batch/",
            headers=self._capture_headers(),
            json_body=body,
            context="batch",
        )
