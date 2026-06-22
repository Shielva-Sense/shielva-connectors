"""Async HTTP client for the SugarCRM REST API (v11).

This module owns **all** network I/O for the SugarCRM connector. It speaks raw
``httpx.AsyncClient`` and returns parsed JSON dicts. It has zero business logic
and zero normalization — both live in :mod:`connector`.

SugarCRM authentication:

* Token endpoint: ``{site_url}/rest/v11/oauth2/token``
* Supported grants: ``password`` (on-prem) and ``authorization_code`` (cloud).
* Token header on every authenticated call: ``OAuth-Token: <access_token>``.

A 401 surfaces a :class:`SugarCRMAuthError` so the connector wrapper can refresh
the token and retry once. 429s surface a :class:`SugarCRMRateLimitError` carrying
``Retry-After`` for the retry helper.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

import httpx

from exceptions import (
    SugarCRMAuthError,
    SugarCRMError,
    SugarCRMNetworkError,
    SugarCRMRateLimitError,
)

_DEFAULT_TIMEOUT_S = 30.0


class SugarCRMHTTPClient:
    """Thin async wrapper around :class:`httpx.AsyncClient` for SugarCRM v11.

    The base URL is tenant-specific (every customer hosts SugarCRM at a different
    site URL), so the connector constructs the client with the resolved
    ``{site_url}/rest/v11`` base.
    """

    def __init__(self, base_url: str, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_s

    # ── Helpers ────────────────────────────────────────────────────────────

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        # SugarCRM v11 uses OAuth-Token, NOT Authorization: Bearer.
        return {
            "OAuth-Token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"

    @staticmethod
    def _extract_error_message(body: Mapping[str, Any]) -> str:
        for key in ("error_message", "message", "error", "error_description"):
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

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        """Map an :class:`httpx.Response` to a typed SugarCRM exception."""
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {}
        body_dict: Dict[str, Any] = body if isinstance(body, dict) else {"raw": body}

        message = self._extract_error_message(body_dict) or response.text or context

        if status == 401:
            raise SugarCRMAuthError(
                f"401 Unauthorized ({context}): {message}",
                status_code=401,
                response_body=body_dict,
            )
        if status == 429:
            raise SugarCRMRateLimitError(
                f"429 Rate limit exceeded ({context}): {message}",
                retry_after=self._parse_retry_after(response.headers),
            )
        if status >= 500:
            raise SugarCRMNetworkError(
                f"HTTP {status} ({context}): {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise SugarCRMError(
            f"HTTP {status} ({context}): {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── Token exchange (no auth header — endpoint is unauthenticated) ──────

    async def post_oauth_token(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        context: str = "post_oauth_token",
    ) -> Dict[str, Any]:
        """POST ``application/json`` to the SugarCRM ``/oauth2/token`` endpoint.

        SugarCRM expects the grant payload as JSON, not form-encoded. Returns
        the parsed response body containing ``access_token`` / ``refresh_token``
        / ``expires_in`` / ``token_type``.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as session:
                resp = await session.post(
                    url,
                    json=dict(payload),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
                self._raise_for_status(resp, context)
                return resp.json() if resp.content else {}
        except (SugarCRMError, SugarCRMNetworkError):
            raise
        except httpx.TransportError as exc:
            raise SugarCRMNetworkError(f"Transport error during {context}: {exc}") from exc

    # ── Generic verbs ──────────────────────────────────────────────────────

    async def get(
        self,
        access_token: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        context: str = "get",
    ) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as session:
                resp = await session.get(
                    self._url(path),
                    headers=self._auth_headers(access_token),
                    params=dict(params) if params else None,
                )
                self._raise_for_status(resp, context)
                return resp.json() if resp.content else {}
        except (SugarCRMError, SugarCRMNetworkError):
            raise
        except httpx.TransportError as exc:
            raise SugarCRMNetworkError(f"Transport error during {context}: {exc}") from exc

    async def post(
        self,
        access_token: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        context: str = "post",
    ) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as session:
                resp = await session.post(
                    self._url(path),
                    headers=self._auth_headers(access_token),
                    json=dict(json_body) if json_body is not None else None,
                )
                self._raise_for_status(resp, context)
                return resp.json() if resp.content else {}
        except (SugarCRMError, SugarCRMNetworkError):
            raise
        except httpx.TransportError as exc:
            raise SugarCRMNetworkError(f"Transport error during {context}: {exc}") from exc

    async def put(
        self,
        access_token: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        context: str = "put",
    ) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as session:
                resp = await session.put(
                    self._url(path),
                    headers=self._auth_headers(access_token),
                    json=dict(json_body) if json_body is not None else None,
                )
                self._raise_for_status(resp, context)
                return resp.json() if resp.content else {}
        except (SugarCRMError, SugarCRMNetworkError):
            raise
        except httpx.TransportError as exc:
            raise SugarCRMNetworkError(f"Transport error during {context}: {exc}") from exc

    async def delete(
        self,
        access_token: str,
        path: str,
        *,
        context: str = "delete",
    ) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as session:
                resp = await session.delete(
                    self._url(path),
                    headers=self._auth_headers(access_token),
                )
                self._raise_for_status(resp, context)
                return resp.json() if resp.content else {}
        except (SugarCRMError, SugarCRMNetworkError):
            raise
        except httpx.TransportError as exc:
            raise SugarCRMNetworkError(f"Transport error during {context}: {exc}") from exc

    # ── Convenience: list endpoints with SugarCRM list params ──────────────

    @staticmethod
    def build_list_params(
        offset: int = 0,
        max_num: int = 50,
        filter_: Optional[List[Any]] = None,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Render SugarCRM ``GET /<Module>`` query params.

        SugarCRM accepts ``offset`` / ``max_num`` for pagination, ``fields`` as a
        comma-separated field list, and ``filter`` as a JSON-encoded list of
        per-field predicates.
        """
        params: Dict[str, Any] = {"offset": int(offset), "max_num": int(max_num)}
        if filter_:
            # SugarCRM supports filter[0][field][operator]=value syntax OR a
            # repeated ``filter`` querystring. Pass the JSON-encoded list as a
            # single ``filter`` value — accepted by v11.
            import json as _json

            params["filter"] = _json.dumps(filter_)
        if fields:
            params["fields"] = ",".join(fields)
        return params
