"""All Shortcut API HTTP calls — zero business logic, zero normalization.

httpx async client. The Shortcut REST API v3 expects:

    Shortcut-Token: <api_token>
    Content-Type:   application/json
    Accept:         application/json

Retry on 429/5xx with exponential backoff, honouring ``Retry-After``.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    ShortcutAuthError,
    ShortcutBadRequestError,
    ShortcutConflictError,
    ShortcutError,
    ShortcutNetworkError,
    ShortcutNotFoundError,
    ShortcutRateLimitError,
    ShortcutServerError,
)

logger = structlog.get_logger(__name__)


SHORTCUT_BASE_URL: str = "https://api.app.shortcut.com/api/v3"
_DEFAULT_TIMEOUT: float = 30.0
_MAX_RETRIES: int = 3
_BACKOFF_BASE: float = 0.5  # seconds
_BACKOFF_MAX: float = 8.0


class ShortcutHTTPClient:
    """Thin async HTTP client for the Shortcut REST API.

    All methods are awaitable and return raw response dicts/lists. Auth + retry
    are owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        *,
        api_token: str = "",
        base_url: str = SHORTCUT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._api_token = api_token or ""
        self._base_url = (base_url or SHORTCUT_BASE_URL).rstrip("/")
        self._timeout = float(timeout)
        self._max_retries = int(max_retries)

    # ── Public introspection ───────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return self._base_url

    def url(self, path: str) -> str:
        """Build a full URL from a path fragment (e.g. ``/member``)."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"

    # ── Headers ────────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Shortcut-Token": self._api_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Error mapping ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_message(body: Any) -> str:
        if isinstance(body, dict):
            return str(
                body.get("message")
                or body.get("error")
                or body.get("error_description")
                or body.get("errors")
                or body
            )
        return str(body)

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
        body_dict = body if isinstance(body, dict) else {"raw": body}
        ctx = f": {context}" if context else ""

        if status == 401:
            raise ShortcutAuthError(
                f"401 Unauthorized{ctx}: {message}",
                status_code=401,
                response_body=body_dict,
            )
        if status == 403:
            raise ShortcutAuthError(
                f"403 Forbidden{ctx}: {message}",
                status_code=403,
                response_body=body_dict,
            )
        if status == 404:
            raise ShortcutNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise ShortcutConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status in (400, 422):
            raise ShortcutBadRequestError(
                f"{status} Bad Request{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after) if retry_after else 5.0
            except ValueError:
                retry_after_s = 5.0
            raise ShortcutRateLimitError(
                f"429 Rate limit exceeded{ctx}: {message}",
                status_code=429,
                response_body=body_dict,
                retry_after_s=retry_after_s,
            )
        if 500 <= status < 600:
            raise ShortcutServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise ShortcutError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── Backoff helper (overridable in tests) ──────────────────────────────

    @staticmethod
    async def _sleep_backoff(seconds: float) -> None:
        await asyncio.sleep(seconds)

    # ── Core request loop ──────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        """Internal request with retry on 429 / 5xx (exponential backoff).

        Returns the parsed JSON body, or ``{}`` on 204 No Content.
        """
        url = self.url(path)
        headers = self._headers()

        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
                # Retry on 429 / 5xx
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self._max_retries - 1:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            try:
                                delay = float(retry_after)
                            except ValueError:
                                delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                        else:
                            delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                        logger.warning(
                            "shortcut.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await self._sleep_backoff(delay)
                        continue
                self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                    logger.warning(
                        "shortcut.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await self._sleep_backoff(delay)
                    continue
                raise ShortcutNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise ShortcutNetworkError(str(last_exc)) from last_exc
        raise ShortcutNetworkError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── Members ────────────────────────────────────────────────────────────

    async def get_current_member(self) -> Dict[str, Any]:
        """GET /member — currently authenticated member."""
        return await self._request("GET", "/member", context="get_current_member")

    async def list_members(self) -> List[Dict[str, Any]]:
        """GET /members."""
        return await self._request("GET", "/members", context="list_members")

    async def get_member(self, member_id: str) -> Dict[str, Any]:
        """GET /members/{member-public-id}."""
        return await self._request(
            "GET", f"/members/{member_id}", context=f"get_member({member_id})"
        )

    # ── Groups (Teams) ─────────────────────────────────────────────────────

    async def list_groups(self) -> List[Dict[str, Any]]:
        """GET /groups."""
        return await self._request("GET", "/groups", context="list_groups")

    # ── Workflows ──────────────────────────────────────────────────────────

    async def list_workflows(self) -> List[Dict[str, Any]]:
        """GET /workflows."""
        return await self._request("GET", "/workflows", context="list_workflows")

    # ── Projects ───────────────────────────────────────────────────────────

    async def list_projects(self) -> List[Dict[str, Any]]:
        """GET /projects."""
        return await self._request("GET", "/projects", context="list_projects")

    # ── Iterations ─────────────────────────────────────────────────────────

    async def list_iterations(self) -> List[Dict[str, Any]]:
        """GET /iterations."""
        return await self._request("GET", "/iterations", context="list_iterations")

    # ── Milestones ─────────────────────────────────────────────────────────

    async def list_milestones(self) -> List[Dict[str, Any]]:
        """GET /milestones."""
        return await self._request("GET", "/milestones", context="list_milestones")

    # ── Epics ──────────────────────────────────────────────────────────────

    async def list_epics(
        self, includes_description: bool = False
    ) -> List[Dict[str, Any]]:
        """GET /epics."""
        params: Dict[str, Any] = {}
        if includes_description:
            params["includes_description"] = "true"
        return await self._request(
            "GET", "/epics", params=params or None, context="list_epics"
        )

    async def get_epic(self, epic_id: int) -> Dict[str, Any]:
        """GET /epics/{epic-public-id}."""
        return await self._request(
            "GET", f"/epics/{epic_id}", context=f"get_epic({epic_id})"
        )

    async def create_epic(
        self,
        name: str,
        description: Optional[str] = None,
        state: str = "to do",
    ) -> Dict[str, Any]:
        """POST /epics."""
        body: Dict[str, Any] = {"name": name, "state": state}
        if description is not None:
            body["description"] = description
        return await self._request(
            "POST", "/epics", json_body=body, context="create_epic"
        )

    # ── Stories ────────────────────────────────────────────────────────────

    async def search_stories(
        self,
        query: Optional[str] = None,
        page_size: int = 25,
        next_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /search/stories — search stories with cursor pagination."""
        body: Dict[str, Any] = {"page_size": page_size}
        if query:
            body["query"] = query
        if next_token:
            body["next"] = next_token
        return await self._request(
            "POST", "/search/stories", json_body=body, context="search_stories"
        )

    async def get_story(self, story_id: int) -> Dict[str, Any]:
        """GET /stories/{story-public-id}."""
        return await self._request(
            "GET", f"/stories/{story_id}", context=f"get_story({story_id})"
        )

    async def create_story(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /stories."""
        return await self._request(
            "POST", "/stories", json_body=payload, context="create_story"
        )

    async def update_story(
        self, story_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /stories/{story-public-id}."""
        return await self._request(
            "PUT",
            f"/stories/{story_id}",
            json_body=fields,
            context=f"update_story({story_id})",
        )

    async def delete_story(self, story_id: int) -> Dict[str, Any]:
        """DELETE /stories/{story-public-id} — 204 No Content."""
        return await self._request(
            "DELETE",
            f"/stories/{story_id}",
            context=f"delete_story({story_id})",
        )

    # ── Labels ─────────────────────────────────────────────────────────────

    async def list_labels(self) -> List[Dict[str, Any]]:
        """GET /labels."""
        return await self._request("GET", "/labels", context="list_labels")

    async def create_label(
        self,
        name: str,
        color: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /labels."""
        body: Dict[str, Any] = {"name": name}
        if color:
            body["color"] = color
        return await self._request(
            "POST", "/labels", json_body=body, context="create_label"
        )

    # ── Files ──────────────────────────────────────────────────────────────

    async def list_files(self) -> List[Dict[str, Any]]:
        """GET /files."""
        return await self._request("GET", "/files", context="list_files")
