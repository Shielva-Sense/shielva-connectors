"""All YouTrack REST API HTTP calls — zero business logic, zero normalization.

httpx async client. YouTrack uses Bearer permanent-token auth and the standard
`$skip` / `$top` / `fields` pagination. Retries 429 / 5xx and transient
transport errors with exponential backoff.
"""
import asyncio
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    YouTrackAuthError,
    YouTrackBadRequestError,
    YouTrackConflictError,
    YouTrackError,
    YouTrackNetworkError,
    YouTrackNotFound,
    YouTrackPreconditionError,
    YouTrackRateLimitError,
)

logger = structlog.get_logger(__name__)


# OCP — change retry behaviour here, nowhere else.
RETRY_DELAY_S: float = 0.5
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 16.0
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_MAX_RETRIES: int = 3


class YouTrackHTTPClient:
    """Thin async HTTP client for the YouTrack REST API.

    All methods are awaitable and return raw response payloads (dict / list).
    Auth + retry are owned here — the connector layer only orchestrates
    business calls.
    """

    def __init__(
        self,
        base_url: str,
        permanent_token: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self._base_url = (base_url or "").rstrip("/")
        self._token = permanent_token or ""
        self._timeout = httpx.Timeout(timeout_s)
        self._max_retries = max_retries

    # ── Internal helpers ────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            message = (
                body.get("error_description")
                or body.get("error")
                or body.get("value")
                or body.get("message")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
            response_body: Dict[str, Any] = body
        else:
            message = str(body)
            response_body = {"raw": body}

        ctx = f": {context}" if context else ""
        if status in (401, 403):
            raise YouTrackAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=response_body,
            )
        if status == 400:
            raise YouTrackBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=response_body,
            )
        if status == 404:
            raise YouTrackNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=response_body,
            )
        if status == 409:
            raise YouTrackConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=response_body,
            )
        if status == 428:
            raise YouTrackPreconditionError(
                f"428 Precondition Required{ctx}: {message}",
                status_code=428,
                response_body=response_body,
            )
        if status == 429:
            raise YouTrackRateLimitError(
                f"429 Rate limit{ctx}: {message}",
                response_body=response_body,
            )
        raise YouTrackError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=response_body,
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
        """Issue a single HTTP request with retry on 429/5xx + transport errors."""
        url = self._url(path)
        last_exc: Optional[BaseException] = None

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
                status = response.status_code
                if status == 429 or 500 <= status < 600:
                    if attempt == self._max_retries:
                        self._raise_for_status(response, context)
                    delay = min(
                        RETRY_DELAY_S * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
                        MAX_RETRY_DELAY_S,
                    )
                    logger.warning(
                        "youtrack.http.retry",
                        status=status,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue

                self._raise_for_status(response, context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.PoolTimeout,
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.NetworkError,
                httpx.TimeoutException,
            ) as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    raise YouTrackNetworkError(
                        f"Network error{': ' + context if context else ''}: {exc}"
                    ) from exc
                delay = min(
                    RETRY_DELAY_S * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
                    MAX_RETRY_DELAY_S,
                )
                logger.warning(
                    "youtrack.http.transport_retry",
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                    error=str(exc),
                )
                await asyncio.sleep(delay)

        if last_exc:
            raise YouTrackNetworkError(f"Network error: {last_exc}") from last_exc
        raise YouTrackError(f"Unreachable retry loop in _request({context})")

    @staticmethod
    def _paging(
        params: Dict[str, Any],
        skip: int,
        top: int,
        fields: Optional[str],
    ) -> Dict[str, Any]:
        params["$skip"] = skip
        params["$top"] = top
        if fields:
            params["fields"] = fields
        return params

    # ── Users ──────────────────────────────────────────────────────────────

    async def get_current_user(
        self,
        fields: Optional[str] = "id,login,fullName,email,banned",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if fields:
            params["fields"] = fields
        return await self._request(
            "GET", "/users/me", params=params, context="get_current_user"
        )

    async def list_users(
        self,
        query: Optional[str] = None,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,login,fullName,email,banned",
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if query:
            params["query"] = query
        self._paging(params, skip, top, fields)
        return await self._request(
            "GET", "/users", params=params, context="list_users"
        )

    async def get_user(
        self,
        user_id: str,
        fields: Optional[str] = "id,login,fullName,email,banned",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if fields:
            params["fields"] = fields
        return await self._request(
            "GET",
            f"/users/{user_id}",
            params=params,
            context=f"get_user({user_id})",
        )

    # ── Projects ───────────────────────────────────────────────────────────

    async def list_projects(
        self,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,shortName,name,description,archived",
    ) -> List[Dict[str, Any]]:
        params = self._paging({}, skip, top, fields)
        return await self._request(
            "GET", "/admin/projects", params=params, context="list_projects"
        )

    async def get_project(
        self,
        project_id: str,
        fields: Optional[str] = "id,shortName,name,description,archived,leader(login,fullName)",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if fields:
            params["fields"] = fields
        return await self._request(
            "GET",
            f"/admin/projects/{project_id}",
            params=params,
            context=f"get_project({project_id})",
        )

    # ── Issues ─────────────────────────────────────────────────────────────

    async def list_issues(
        self,
        query: Optional[str] = None,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,idReadable,summary,description,created,updated,reporter(login),customFields(name,value(name))",
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if query:
            params["query"] = query
        self._paging(params, skip, top, fields)
        return await self._request(
            "GET", "/issues", params=params, context="list_issues"
        )

    async def get_issue(
        self,
        issue_id: str,
        fields: Optional[str] = "id,idReadable,summary,description,created,updated,reporter(login),customFields(name,value(name))",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if fields:
            params["fields"] = fields
        return await self._request(
            "GET",
            f"/issues/{issue_id}",
            params=params,
            context=f"get_issue({issue_id})",
        )

    async def create_issue(
        self,
        project_id: str,
        summary: str,
        description: str = "",
        custom_fields: Optional[List[Dict[str, Any]]] = None,
        fields: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "project": {"id": project_id},
            "summary": summary,
            "description": description or "",
        }
        if custom_fields:
            body["customFields"] = custom_fields
        params: Dict[str, Any] = {}
        if fields:
            params["fields"] = fields
        return await self._request(
            "POST", "/issues", params=params, json_body=body, context="create_issue"
        )

    async def update_issue(
        self,
        issue_id: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        custom_fields: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if custom_fields is not None:
            body["customFields"] = custom_fields
        return await self._request(
            "POST",
            f"/issues/{issue_id}",
            json_body=body,
            context=f"update_issue({issue_id})",
        )

    async def delete_issue(self, issue_id: str) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/issues/{issue_id}",
            context=f"delete_issue({issue_id})",
        )

    # ── Comments ───────────────────────────────────────────────────────────

    async def add_comment(self, issue_id: str, text: str) -> Dict[str, Any]:
        body = {"text": text}
        return await self._request(
            "POST",
            f"/issues/{issue_id}/comments",
            json_body=body,
            context=f"add_comment({issue_id})",
        )

    async def list_comments(
        self,
        issue_id: str,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,text,author(login),created",
    ) -> List[Dict[str, Any]]:
        params = self._paging({}, skip, top, fields)
        return await self._request(
            "GET",
            f"/issues/{issue_id}/comments",
            params=params,
            context=f"list_comments({issue_id})",
        )

    # ── Tags ───────────────────────────────────────────────────────────────

    async def list_tags(
        self,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,name,owner(login)",
    ) -> List[Dict[str, Any]]:
        params = self._paging({}, skip, top, fields)
        return await self._request(
            "GET", "/issueTags", params=params, context="list_tags"
        )

    # ── Time tracking ──────────────────────────────────────────────────────

    async def list_time_tracking(
        self,
        issue_id: str,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,date,duration(minutes),text,author(login),type(name)",
    ) -> List[Dict[str, Any]]:
        params = self._paging({}, skip, top, fields)
        return await self._request(
            "GET",
            f"/issues/{issue_id}/timeTracking/workItems",
            params=params,
            context=f"list_time_tracking({issue_id})",
        )

    # ── Agile boards + sprints ─────────────────────────────────────────────

    async def list_boards(
        self,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,name,owner(login),projects(id,shortName)",
    ) -> List[Dict[str, Any]]:
        params = self._paging({}, skip, top, fields)
        return await self._request(
            "GET", "/agiles", params=params, context="list_boards"
        )

    async def list_sprints(
        self,
        board_id: str,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,name,start,finish,goal,archived",
    ) -> List[Dict[str, Any]]:
        params = self._paging({}, skip, top, fields)
        return await self._request(
            "GET",
            f"/agiles/{board_id}/sprints",
            params=params,
            context=f"list_sprints({board_id})",
        )

    # ── Knowledge base / Articles ──────────────────────────────────────────

    async def list_articles(
        self,
        query: Optional[str] = None,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,idReadable,summary,content,project(id,shortName),reporter(login),created,updated",
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if query:
            params["query"] = query
        self._paging(params, skip, top, fields)
        return await self._request(
            "GET", "/articles", params=params, context="list_articles"
        )
