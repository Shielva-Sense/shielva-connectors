"""Async HTTP client for the Microsoft Graph OneNote surface.

Single responsibility: HTTP. No business logic. No normalization. No state
beyond ``base_url`` and the timeout.

Key behaviours required by the spec:
  * httpx.AsyncClient under the hood.
  * Bearer token on every authenticated call (provided via *token_provider*).
  * Auto-refresh on 401 via a connector-supplied async callback (run once;
    a second 401 surfaces as :class:`OneNoteAuthError`).
  * Microsoft Graph throttling: when the server returns 429 with a
    ``Retry-After`` header we sleep for the advertised window and replay
    the request once. A second 429 surfaces as
    :class:`OneNoteRateLimitError` so the caller (or
    :func:`helpers.utils.with_retry`) can apply backoff.
  * GET /pages/{id}/content returns raw XHTML — we expose it as ``str``.
  * POST /sections/{id}/pages takes a raw XHTML body with a custom
    ``Content-Type`` (not JSON).
"""
from __future__ import annotations

import asyncio
import json as _json
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    OneNoteAuthError,
    OneNoteError,
    OneNoteNetworkError,
    OneNoteNotFound,
    OneNoteRateLimitError,
)

logger = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0/me/onenote"

# OAuth scopes Microsoft Graph expects for OneNote.
DEFAULT_SCOPES = "Notes.ReadWrite Notes.Read offline_access"

# RFC 7231 caps Retry-After we honour automatically. Beyond this the caller
# (with_retry) decides whether to keep waiting — we never block longer than
# this in a single HTTP call.
_MAX_AUTO_RETRY_AFTER_S: float = 30.0


TokenProvider = Callable[[], Awaitable[str]]
TokenRefresher = Callable[[], Awaitable[str]]


class OneNoteHTTPClient:
    """Thin async HTTP client for the Microsoft Graph OneNote API."""

    def __init__(
        self,
        base_url: str = _GRAPH_BASE,
        timeout: float = 30.0,
    ):
        self._base_url = (base_url or _GRAPH_BASE).rstrip("/")
        self._timeout = timeout

    # ── Header / URL helpers ───────────────────────────────────────────────

    def _auth_headers(self, access_token: str, *, json_body: bool = True) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    # ── Error mapping ──────────────────────────────────────────────────────

    async def _parse_body(self, response: httpx.Response) -> Dict[str, Any]:
        try:
            return response.json()
        except Exception:
            return {}

    def _extract_message(self, body: Dict[str, Any]) -> str:
        err = body.get("error", {})
        if isinstance(err, dict):
            return err.get("message") or err.get("code") or str(body)
        return str(err) or str(body)

    async def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return
        body = await self._parse_body(response)
        message = self._extract_message(body)
        ctx_suffix = f": {context}" if context else ""

        if status == 401:
            raise OneNoteAuthError(
                f"401 Unauthorized{ctx_suffix}: {message}",
                status_code=401,
                response_body=body,
            )
        if status == 404:
            raise OneNoteNotFound(
                f"404 Not Found{ctx_suffix}: {message}",
                status_code=404,
                response_body=body,
            )
        if status == 429:
            retry_after = self._retry_after_seconds(response)
            raise OneNoteRateLimitError(
                f"429 Throttled{ctx_suffix}: {message}",
                retry_after=retry_after,
                response_body=body,
            )
        if 500 <= status < 600:
            raise OneNoteNetworkError(
                f"HTTP {status}{ctx_suffix}: {message}",
                status_code=status,
                response_body=body,
            )
        raise OneNoteError(
            f"HTTP {status}{ctx_suffix}: {message}",
            status_code=status,
            response_body=body,
        )

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        header = response.headers.get("Retry-After")
        if not header:
            return 0.0
        try:
            return float(header)
        except (TypeError, ValueError):
            return 0.0

    # ── Core authenticated request ────────────────────────────────────────

    async def request(
        self,
        method: str,
        path: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        content: Optional[str] = None,
        headers_override: Optional[Dict[str, str]] = None,
        context: str = "",
        expect_json: bool = True,
    ) -> Any:
        """Issue an authenticated request and return the parsed body.

        Auto-handles a single 401 (via ``token_refresher``) and a single 429
        (by honouring ``Retry-After`` up to :data:`_MAX_AUTO_RETRY_AFTER_S`).
        Subsequent failures of the same kind surface as exceptions so the
        caller can decide whether to retry with backoff.
        """
        access_token = await token_provider()
        url = self._url(path)
        attempts_401 = 0
        attempts_429 = 0

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while True:
                headers = self._auth_headers(
                    access_token,
                    json_body=content is None,
                )
                if headers_override:
                    headers.update(headers_override)
                try:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json if content is None else None,
                        content=content,
                    )
                except httpx.RequestError as exc:
                    raise OneNoteNetworkError(
                        f"network error{': ' + context if context else ''}: {exc}"
                    ) from exc

                if response.status_code == 401 and token_refresher and attempts_401 == 0:
                    attempts_401 += 1
                    logger.warning(
                        "onenote.refresh_on_401", context=context, attempt=attempts_401,
                    )
                    access_token = await token_refresher()
                    continue

                if response.status_code == 429 and attempts_429 == 0:
                    attempts_429 += 1
                    delay = self._retry_after_seconds(response)
                    if delay and delay <= _MAX_AUTO_RETRY_AFTER_S:
                        logger.warning(
                            "onenote.throttled — sleeping",
                            context=context,
                            retry_after=delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # fall through to raise so caller-level backoff applies

                await self._raise_for_status(response, context=context)
                if not expect_json:
                    return response.text
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError:
                    return response.text

    # ── Token endpoint (no Bearer) ────────────────────────────────────────

    async def post_form_data(
        self,
        url: str,
        payload: Dict[str, str],
        context: str = "post_form_data",
    ) -> Dict[str, Any]:
        """POST form-encoded data — used for the OAuth token endpoint."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(url, data=payload)
            except httpx.RequestError as exc:
                raise OneNoteNetworkError(
                    f"network error: {context}: {exc}"
                ) from exc
            await self._raise_for_status(response, context=context)
            return await self._parse_body(response)

    # ── Notebooks ─────────────────────────────────────────────────────────

    async def list_notebooks(
        self,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
        top: int = 25,
        skip: int = 0,
        filter: Optional[str] = None,
        orderby: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /notebooks."""
        params: Dict[str, Any] = {"$top": top, "$skip": skip}
        if filter:
            params["$filter"] = filter
        if orderby:
            params["$orderby"] = orderby
        return await self.request(
            "GET", "/notebooks",
            token_provider=token_provider, token_refresher=token_refresher,
            params=params, context="list_notebooks",
        )

    async def get_notebook(
        self,
        notebook_id: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """GET /notebooks/{id}."""
        return await self.request(
            "GET", f"/notebooks/{notebook_id}",
            token_provider=token_provider, token_refresher=token_refresher,
            context=f"get_notebook({notebook_id})",
        )

    async def create_notebook(
        self,
        display_name: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """POST /notebooks."""
        return await self.request(
            "POST", "/notebooks",
            token_provider=token_provider, token_refresher=token_refresher,
            json={"displayName": display_name}, context="create_notebook",
        )

    # ── Sections ──────────────────────────────────────────────────────────

    async def list_sections(
        self,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
        notebook_id: Optional[str] = None,
        top: int = 25,
        skip: int = 0,
    ) -> Dict[str, Any]:
        """GET /notebooks/{id}/sections OR GET /sections."""
        path = (
            f"/notebooks/{notebook_id}/sections" if notebook_id else "/sections"
        )
        return await self.request(
            "GET", path,
            token_provider=token_provider, token_refresher=token_refresher,
            params={"$top": top, "$skip": skip}, context="list_sections",
        )

    async def get_section(
        self,
        section_id: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """GET /sections/{id}."""
        return await self.request(
            "GET", f"/sections/{section_id}",
            token_provider=token_provider, token_refresher=token_refresher,
            context=f"get_section({section_id})",
        )

    async def create_section(
        self,
        notebook_id: str,
        display_name: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """POST /notebooks/{id}/sections."""
        return await self.request(
            "POST", f"/notebooks/{notebook_id}/sections",
            token_provider=token_provider, token_refresher=token_refresher,
            json={"displayName": display_name}, context="create_section",
        )

    # ── Section groups ────────────────────────────────────────────────────

    async def list_section_groups(
        self,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
        notebook_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /notebooks/{id}/sectionGroups OR GET /sectionGroups."""
        path = (
            f"/notebooks/{notebook_id}/sectionGroups"
            if notebook_id
            else "/sectionGroups"
        )
        return await self.request(
            "GET", path,
            token_provider=token_provider, token_refresher=token_refresher,
            context="list_section_groups",
        )

    # ── Pages ─────────────────────────────────────────────────────────────

    async def list_pages(
        self,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
        section_id: Optional[str] = None,
        top: int = 25,
        skip: int = 0,
        filter: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /sections/{id}/pages OR GET /pages."""
        path = (
            f"/sections/{section_id}/pages" if section_id else "/pages"
        )
        params: Dict[str, Any] = {"$top": top, "$skip": skip}
        if filter:
            params["$filter"] = filter
        if search:
            params["$search"] = search
        return await self.request(
            "GET", path,
            token_provider=token_provider, token_refresher=token_refresher,
            params=params, context="list_pages",
        )

    async def get_page(
        self,
        page_id: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """GET /pages/{id}."""
        return await self.request(
            "GET", f"/pages/{page_id}",
            token_provider=token_provider, token_refresher=token_refresher,
            context=f"get_page({page_id})",
        )

    async def get_page_content(
        self,
        page_id: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> str:
        """GET /pages/{id}/content — returns raw XHTML."""
        return await self.request(
            "GET", f"/pages/{page_id}/content",
            token_provider=token_provider, token_refresher=token_refresher,
            context=f"get_page_content({page_id})",
            expect_json=False,
        )

    async def create_page(
        self,
        section_id: str,
        html_body: str,
        content_type: str = "application/xhtml+xml",
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """POST /sections/{id}/pages — raw XHTML body.

        OneNote requires the page body to be sent as XHTML (or multipart for
        embedded images) — JSON is NOT accepted.
        """
        return await self.request(
            "POST", f"/sections/{section_id}/pages",
            token_provider=token_provider, token_refresher=token_refresher,
            content=html_body,
            headers_override={"Content-Type": content_type},
            context=f"create_page(section={section_id})",
        )

    async def update_page(
        self,
        page_id: str,
        commands: List[Dict[str, Any]],
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """PATCH /pages/{id}/content — body is a JSON array of update commands."""
        return await self.request(
            "PATCH", f"/pages/{page_id}/content",
            token_provider=token_provider, token_refresher=token_refresher,
            content=_json.dumps(commands),
            headers_override={"Content-Type": "application/json"},
            context=f"update_page({page_id})",
        )

    async def delete_page(
        self,
        page_id: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """DELETE /pages/{id} — returns {} on 204."""
        return await self.request(
            "DELETE", f"/pages/{page_id}",
            token_provider=token_provider, token_refresher=token_refresher,
            context=f"delete_page({page_id})",
        )

    async def copy_page_to_section(
        self,
        page_id: str,
        target_section_id: str,
        group_id: Optional[str] = None,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """POST /pages/{id}/copyToSection."""
        body: Dict[str, Any] = {"id": target_section_id}
        if group_id:
            body["groupId"] = group_id
        return await self.request(
            "POST", f"/pages/{page_id}/copyToSection",
            token_provider=token_provider, token_refresher=token_refresher,
            json=body, context=f"copy_page_to_section({page_id})",
        )

    async def search_pages(
        self,
        query: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
        top: int = 25,
    ) -> Dict[str, Any]:
        """GET /pages?$search=… — full-text search across all pages."""
        params = {"$search": query, "$top": top}
        return await self.request(
            "GET", "/pages",
            token_provider=token_provider, token_refresher=token_refresher,
            params=params, context="search_pages",
        )
