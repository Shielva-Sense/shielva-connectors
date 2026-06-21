"""Async HTTP client for the Microsoft Graph mail surface.

Single responsibility: HTTP. No business logic. No normalization. No state
beyond ``base_url`` and the token-refresh callback the connector supplies.

Key behaviours required by the spec:
  * httpx.AsyncClient under the hood.
  * Bearer token on every authenticated call.
  * Auto-refresh on 401 via a connector-provided async callback (run once;
    a second 401 surfaces as :class:`OutlookMailAuthError`).
  * Microsoft Graph throttling: when the server returns 429 with a
    ``Retry-After`` header we sleep for the advertised window and replay
    the request once. A second 429 surfaces as
    :class:`OutlookMailRateLimitError` so the caller (or
    :func:`helpers.utils.with_retry`) can apply backoff.
"""
from __future__ import annotations

import asyncio
import json as _json
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    OutlookMailAuthError,
    OutlookMailError,
    OutlookMailNetworkError,
    OutlookMailNotFound,
    OutlookMailRateLimitError,
)

logger = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# RFC 7231 caps Retry-After we honour automatically. Beyond this the caller
# (with_retry) decides whether to keep waiting — we never block longer than
# this in a single HTTP call.
_MAX_AUTO_RETRY_AFTER_S: float = 30.0


TokenProvider = Callable[[], Awaitable[str]]
TokenRefresher = Callable[[], Awaitable[str]]


class OutlookMailHTTPClient:
    """Thin async HTTP client for the Microsoft Graph mail API."""

    def __init__(
        self,
        base_url: str = _GRAPH_BASE,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ── Header / URL helpers ───────────────────────────────────────────────

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

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
            raise OutlookMailAuthError(
                f"401 Unauthorized{ctx_suffix}: {message}",
                status_code=401,
                response_body=body,
            )
        if status == 404:
            raise OutlookMailNotFound(
                f"404 Not Found{ctx_suffix}: {message}",
                status_code=404,
                response_body=body,
            )
        if status == 429:
            retry_after = self._retry_after_seconds(response)
            raise OutlookMailRateLimitError(
                f"429 Throttled{ctx_suffix}: {message}",
                retry_after=retry_after,
                response_body=body,
            )
        if 500 <= status < 600:
            raise OutlookMailNetworkError(
                f"HTTP {status}{ctx_suffix}: {message}",
                status_code=status,
                response_body=body,
            )
        raise OutlookMailError(
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
            # HTTP-date form is allowed by the RFC but Graph always sends an
            # integer; treat anything else as zero.
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
        context: str = "",
    ) -> Dict[str, Any]:
        """Issue an authenticated request and return the parsed JSON body.

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
                try:
                    response = await client.request(
                        method,
                        url,
                        headers=self._auth_headers(access_token),
                        params=params,
                        json=json,
                    )
                except httpx.RequestError as exc:
                    raise OutlookMailNetworkError(
                        f"network error{': ' + context if context else ''}: {exc}"
                    ) from exc

                if response.status_code == 401 and token_refresher and attempts_401 == 0:
                    attempts_401 += 1
                    logger.warning(
                        "outlook_mail.refresh_on_401", context=context, attempt=attempts_401,
                    )
                    access_token = await token_refresher()
                    continue

                if response.status_code == 429 and attempts_429 == 0:
                    attempts_429 += 1
                    delay = self._retry_after_seconds(response)
                    if delay and delay <= _MAX_AUTO_RETRY_AFTER_S:
                        logger.warning(
                            "outlook_mail.throttled — sleeping",
                            context=context,
                            retry_after=delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # fall through to raise so caller-level backoff applies

                await self._raise_for_status(response, context=context)
                if response.status_code == 202 or not response.content:
                    return {}
                return await self._parse_body(response)

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
                raise OutlookMailNetworkError(
                    f"network error: {context}: {exc}"
                ) from exc
            await self._raise_for_status(response, context=context)
            return await self._parse_body(response)

    # ── Mail surface (thin wrappers) ──────────────────────────────────────

    async def get_me(
        self,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """GET /me — fetch the signed-in user profile (used by health_check)."""
        return await self.request(
            "GET", "/me",
            token_provider=token_provider, token_refresher=token_refresher,
            context="get_me",
        )

    async def list_messages(
        self,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
        folder: str = "inbox",
        top: int = 25,
        skip: int = 0,
        filter: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /me/mailFolders/{folder}/messages."""
        params: Dict[str, Any] = {"$top": top, "$skip": skip}
        if filter:
            params["$filter"] = filter
        if search:
            # Graph requires quoted strings for $search.
            params["$search"] = f'"{search}"' if not search.startswith('"') else search
        return await self.request(
            "GET", f"/me/mailFolders/{folder}/messages",
            token_provider=token_provider, token_refresher=token_refresher,
            params=params, context="list_messages",
        )

    async def get_message(
        self,
        message_id: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """GET /me/messages/{id}."""
        return await self.request(
            "GET", f"/me/messages/{message_id}",
            token_provider=token_provider, token_refresher=token_refresher,
            context=f"get_message({message_id})",
        )

    async def send_mail(
        self,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
        message_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /me/sendMail — returns {} on 202 Accepted."""
        return await self.request(
            "POST", "/me/sendMail",
            token_provider=token_provider, token_refresher=token_refresher,
            json=message_payload, context="send_mail",
        )

    async def create_draft(
        self,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
        message_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /me/messages — creates a draft."""
        return await self.request(
            "POST", "/me/messages",
            token_provider=token_provider, token_refresher=token_refresher,
            json=message_payload, context="create_draft",
        )

    async def reply_message(
        self,
        message_id: str,
        comment: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """POST /me/messages/{id}/reply."""
        return await self.request(
            "POST", f"/me/messages/{message_id}/reply",
            token_provider=token_provider, token_refresher=token_refresher,
            json={"comment": comment}, context=f"reply_message({message_id})",
        )

    async def forward_message(
        self,
        message_id: str,
        to_recipients: List[Dict[str, Any]],
        comment: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """POST /me/messages/{id}/forward."""
        body = {"comment": comment, "toRecipients": to_recipients}
        return await self.request(
            "POST", f"/me/messages/{message_id}/forward",
            token_provider=token_provider, token_refresher=token_refresher,
            json=body, context=f"forward_message({message_id})",
        )

    async def move_message(
        self,
        message_id: str,
        destination_folder_id: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """POST /me/messages/{id}/move."""
        return await self.request(
            "POST", f"/me/messages/{message_id}/move",
            token_provider=token_provider, token_refresher=token_refresher,
            json={"destinationId": destination_folder_id},
            context=f"move_message({message_id})",
        )

    async def delete_message(
        self,
        message_id: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """DELETE /me/messages/{id} — returns {} on 204."""
        return await self.request(
            "DELETE", f"/me/messages/{message_id}",
            token_provider=token_provider, token_refresher=token_refresher,
            context=f"delete_message({message_id})",
        )

    async def list_mail_folders(
        self,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """GET /me/mailFolders."""
        return await self.request(
            "GET", "/me/mailFolders",
            token_provider=token_provider, token_refresher=token_refresher,
            context="list_mail_folders",
        )

    async def create_mail_folder(
        self,
        display_name: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """POST /me/mailFolders."""
        return await self.request(
            "POST", "/me/mailFolders",
            token_provider=token_provider, token_refresher=token_refresher,
            json={"displayName": display_name}, context="create_mail_folder",
        )

    async def patch_message(
        self,
        message_id: str,
        patch: Dict[str, Any],
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> Dict[str, Any]:
        """PATCH /me/messages/{id} — partial update (used for isRead toggle)."""
        return await self.request(
            "PATCH", f"/me/messages/{message_id}",
            token_provider=token_provider, token_refresher=token_refresher,
            json=patch, context=f"patch_message({message_id})",
        )

    async def search_messages(
        self,
        query: str,
        *,
        token_provider: TokenProvider,
        token_refresher: Optional[TokenRefresher] = None,
        top: int = 25,
    ) -> Dict[str, Any]:
        """GET /me/messages?$search=\"…\"."""
        params = {"$search": f'"{query}"' if not query.startswith('"') else query,
                  "$top": top}
        return await self.request(
            "GET", "/me/messages",
            token_provider=token_provider, token_refresher=token_refresher,
            params=params, context="search_messages",
        )
