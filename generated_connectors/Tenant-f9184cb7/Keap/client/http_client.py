"""Async HTTP client for the Keap (Infusionsoft) REST API (v1).

This module owns **all** network I/O for the Keap connector. It speaks raw
``httpx`` and returns parsed JSON dicts. It has zero business logic and zero
normalization — both of those live in :mod:`connector`.

The client supports an optional ``token_refresher`` callback so the connector
can transparently refresh an expired OAuth2 access token on a 401 response and
retry the in-flight request exactly once. Generic transient-error retries
(429 + 5xx) are the caller's responsibility via :func:`helpers.utils.with_retry`.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

import httpx

from exceptions import (
    KeapAuthError,
    KeapError,
    KeapNetworkError,
    KeapNotFound,
    KeapRateLimitError,
)

_KEAP_BASE = "https://api.infusionsoft.com/crm/rest/v1"
_DEFAULT_TIMEOUT_S = 30.0

TokenRefresher = Callable[[], Awaitable[str]]


class KeapHTTPClient:
    """Thin async wrapper around ``httpx.AsyncClient`` for the Keap REST API.

    Every public verb (``get``, ``post``, ``patch``, ``delete``) takes the path
    relative to the configured base URL and a Bearer access token. When the
    response is 401 and a ``token_refresher`` was provided, the client invokes
    the refresher and retries the request once with the new token. Persistent
    auth failures raise :class:`KeapAuthError`.
    """

    def __init__(
        self,
        base_url: str = _KEAP_BASE,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        token_refresher: Optional[TokenRefresher] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._token_refresher = token_refresher

    # ── Helpers ────────────────────────────────────────────────────────────

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        """Map an ``httpx.Response`` to a Keap connector exception."""
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {}
        body_dict: Dict[str, Any] = body if isinstance(body, dict) else {"raw": body}

        message = self._extract_error_message(body_dict) or response.text or context

        if status in (401, 403):
            raise KeapAuthError(
                f"HTTP {status} Unauthorized ({context}): {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise KeapNotFound(resource=context, resource_id="")
        if status == 429:
            retry_after = self._parse_retry_after(response.headers)
            raise KeapRateLimitError(
                f"429 Rate limit exceeded ({context})",
                retry_after=retry_after,
            )
        if status >= 500:
            raise KeapNetworkError(
                f"HTTP {status} server error ({context}): {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise KeapError(
            f"HTTP {status} ({context}): {message}",
            status_code=status,
            response_body=body_dict,
        )

    @staticmethod
    def _extract_error_message(body: Mapping[str, Any]) -> str:
        """Best-effort extraction of Keap's error message field."""
        for key in ("message", "error_description", "error", "detail"):
            val = body.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, dict) and isinstance(val.get("message"), str):
                return val["message"]
        return ""

    @staticmethod
    def _parse_retry_after(headers: Mapping[str, str]) -> Optional[float]:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if not raw:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def _send(
        self,
        method: str,
        access_token: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        context: str,
    ) -> Dict[str, Any]:
        """Issue an HTTP request, refreshing the token once on 401 if possible."""
        token = access_token
        attempted_refresh = False
        async with httpx.AsyncClient(timeout=self._timeout) as session:
            while True:
                try:
                    resp = await session.request(
                        method,
                        self._url(path),
                        headers=self._auth_headers(token),
                        params=dict(params) if params is not None else None,
                        json=dict(json_body) if json_body is not None else None,
                    )
                except httpx.TimeoutException as exc:
                    raise KeapNetworkError(f"Request timed out: {exc}") from exc
                except httpx.TransportError:
                    raise

                if resp.status_code == 401 and self._token_refresher and not attempted_refresh:
                    attempted_refresh = True
                    token = await self._token_refresher()
                    continue
                self._raise_for_status(resp, context)
                return resp.json() if resp.content else {}

    # ── Generic verbs ──────────────────────────────────────────────────────

    async def get(
        self,
        access_token: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        context: str = "get",
    ) -> Dict[str, Any]:
        return await self._send(
            "GET", access_token, path, params=params, context=context
        )

    async def post(
        self,
        access_token: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        context: str = "post",
    ) -> Dict[str, Any]:
        return await self._send(
            "POST", access_token, path, json_body=json_body, context=context
        )

    async def patch(
        self,
        access_token: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        context: str = "patch",
    ) -> Dict[str, Any]:
        return await self._send(
            "PATCH", access_token, path, json_body=json_body, context=context
        )

    async def delete(
        self,
        access_token: str,
        path: str,
        *,
        context: str = "delete",
    ) -> Dict[str, Any]:
        return await self._send("DELETE", access_token, path, context=context)

    async def post_form(
        self,
        url: str,
        payload: Mapping[str, str],
        *,
        context: str = "post_form",
    ) -> Dict[str, Any]:
        """POST ``application/x-www-form-urlencoded`` to an absolute URL.

        Used by the connector for the OAuth2 token exchange — the token URL is
        not the Keap API base.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as session:
                resp = await session.post(url, data=dict(payload))
        except httpx.TimeoutException as exc:
            raise KeapNetworkError(f"Token request timed out: {exc}") from exc
        self._raise_for_status(resp, context)
        return resp.json() if resp.content else {}

    def set_token_refresher(self, refresher: Optional[TokenRefresher]) -> None:
        """Install (or replace) the token-refresh callback."""
        self._token_refresher = refresher
