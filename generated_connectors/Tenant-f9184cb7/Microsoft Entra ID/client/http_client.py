"""All Microsoft Graph HTTP calls — zero business logic, zero normalization.

httpx async client. Owns:
- client-credentials token exchange against the tenant token endpoint
- in-memory access-token cache (refresh ~60s before expiry)
- automatic refresh on 401 (single retry)
- retry on 429/5xx honoring Retry-After
- raises typed `EntraIdError` subclasses for every non-2xx
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    EntraIdAuthError,
    EntraIdBadRequestError,
    EntraIdConflictError,
    EntraIdError,
    EntraIdNetworkError,
    EntraIdNotFound,
    EntraIdRateLimitError,
    EntraIdServerError,
)

logger = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_LOGIN_BASE = "https://login.microsoftonline.com"
_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# OCP: retry constants — change here, nowhere else
_RETRY_DELAY_S: float = 1.0
_BACKOFF_FACTOR: float = 2.0
_MAX_RETRY_DELAY_S: float = 32.0
_MAX_RETRIES: int = 3
_TOKEN_REFRESH_LEAD_S: int = 60
_DEFAULT_TIMEOUT_S: float = 30.0


class EntraIdHTTPClient:
    """Thin async HTTP client for Microsoft Graph + the AAD token endpoint.

    Construction does **not** perform I/O. The first call that needs a bearer
    token will mint one against ``token_url`` using the client_credentials grant.
    """

    def __init__(
        self,
        azure_tenant_id: str,
        client_id: str,
        client_secret: str,
        base_url: str = _GRAPH_BASE,
        scope: str = _DEFAULT_SCOPE,
        token_url: Optional[str] = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._azure_tenant_id = azure_tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._scope = scope or _DEFAULT_SCOPE
        self._token_url = token_url or (
            f"{_LOGIN_BASE}/{azure_tenant_id}/oauth2/v2.0/token"
        )
        self._timeout = httpx.Timeout(timeout_s)

        self._access_token: Optional[str] = None
        self._token_expiry_epoch: float = 0.0
        self._token_lock = asyncio.Lock()

    # ── Public token API ────────────────────────────────────────────────────

    @property
    def token_url(self) -> str:
        return self._token_url

    @property
    def base_url(self) -> str:
        return self._base_url

    async def authenticate(self, force: bool = False) -> Dict[str, Any]:
        """Mint (or re-mint) a client-credentials access token.

        Returns the raw token envelope dict so the caller can inspect ``scope``
        and ``expires_in``. When ``force=False`` returns the cached envelope if
        it is still valid.
        """
        if not force:
            if self._access_token and time.time() < self._token_expiry_epoch:
                return {
                    "access_token": self._access_token,
                    "expires_in": int(max(0, self._token_expiry_epoch - time.time())),
                    "token_type": "Bearer",
                    "scope": self._scope,
                    "cached": True,
                }

        async with self._token_lock:
            # double-checked: another coroutine may have refreshed while we waited
            if (
                not force
                and self._access_token
                and time.time() < self._token_expiry_epoch
            ):
                return {
                    "access_token": self._access_token,
                    "expires_in": int(max(0, self._token_expiry_epoch - time.time())),
                    "token_type": "Bearer",
                    "scope": self._scope,
                    "cached": True,
                }

            payload = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": self._scope,
            }
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as session:
                    resp = await session.post(self._token_url, data=payload)
            except httpx.HTTPError as exc:
                raise EntraIdNetworkError(
                    f"token endpoint unreachable: {exc}"
                ) from exc

            if resp.status_code >= 400:
                body = _safe_json(resp)
                desc = (
                    body.get("error_description")
                    or body.get("error")
                    or resp.text
                    or "token exchange failed"
                )
                raise EntraIdAuthError(
                    f"{resp.status_code} token exchange failed: {desc}",
                    status_code=resp.status_code,
                    response_body=body,
                )

            data = resp.json()
            access_token = data.get("access_token")
            if not access_token:
                raise EntraIdAuthError(
                    "token endpoint returned no access_token",
                    status_code=resp.status_code,
                    response_body=data,
                )
            expires_in = int(data.get("expires_in", 3600))
            self._access_token = access_token
            self._token_expiry_epoch = time.time() + max(
                60, expires_in - _TOKEN_REFRESH_LEAD_S
            )
            logger.info(
                "entra_id.token.minted",
                expires_in=expires_in,
                scope=self._scope,
            )
            return data

    async def _bearer(self) -> str:
        if not self._access_token or time.time() >= self._token_expiry_epoch:
            await self.authenticate()
        return self._access_token or ""

    def _auth_headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ── Request core ────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        context: str = "",
        expect_json: bool = True,
    ) -> Any:
        """Make an authenticated Microsoft Graph request.

        Handles:
          - lazy token minting
          - refresh-on-401 (single retry)
          - retry-on-429/5xx (up to _MAX_RETRIES) honoring Retry-After
          - 404 → EntraIdNotFound
          - non-JSON bodies (204 No Content)
        """
        url = path if path.startswith("http") else f"{self._base_url}/{path.lstrip('/')}"
        attempt = 0
        refreshed_once = False
        last_exc: Optional[Exception] = None

        while attempt <= _MAX_RETRIES:
            token = await self._bearer()
            headers = self._auth_headers(token)
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as session:
                    resp = await session.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
            except httpx.HTTPError as exc:
                last_exc = EntraIdNetworkError(
                    f"network error{(' (' + context + ')') if context else ''}: {exc}"
                )
                if attempt == _MAX_RETRIES:
                    raise last_exc from exc
                await asyncio.sleep(_compute_backoff(attempt))
                attempt += 1
                continue

            status = resp.status_code

            if status == 401 and not refreshed_once:
                # token may have been revoked or hit clock-skew — re-mint once
                refreshed_once = True
                self._access_token = None
                self._token_expiry_epoch = 0.0
                logger.warning(
                    "entra_id.http.refresh_on_401",
                    context=context,
                )
                continue

            if status == 429 or 500 <= status < 600:
                if attempt == _MAX_RETRIES:
                    body = _safe_json(resp)
                    if status == 429:
                        raise EntraIdRateLimitError(
                            f"429 throttled{(' (' + context + ')') if context else ''}: "
                            f"{_extract_error_message(body) or resp.text}",
                            retry_after_s=_parse_retry_after(
                                resp.headers.get("Retry-After")
                            )
                            or 5.0,
                        )
                    raise EntraIdServerError(
                        f"HTTP {status}{(' (' + context + ')') if context else ''}: "
                        f"{_extract_error_message(body) or resp.text}",
                        status_code=status,
                        response_body=body,
                    )
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                delay = (
                    retry_after if retry_after is not None else _compute_backoff(attempt)
                )
                logger.warning(
                    "entra_id.http.retry",
                    status=status,
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if status == 401:
                body = _safe_json(resp)
                raise EntraIdAuthError(
                    f"401 Unauthorized{(' (' + context + ')') if context else ''}: "
                    f"{_extract_error_message(body) or 'invalid token'}",
                    status_code=401,
                    response_body=body,
                )

            if status == 403:
                body = _safe_json(resp)
                raise EntraIdAuthError(
                    f"403 Forbidden{(' (' + context + ')') if context else ''}: "
                    f"{_extract_error_message(body) or 'app lacks required Graph permission'}",
                    status_code=403,
                    response_body=body,
                )

            if status == 404:
                body = _safe_json(resp)
                raise EntraIdNotFound(
                    f"404 Not Found{(' (' + context + ')') if context else ''}: "
                    f"{_extract_error_message(body) or 'resource missing'}",
                    status_code=404,
                    response_body=body,
                )

            if status == 400:
                body = _safe_json(resp)
                raise EntraIdBadRequestError(
                    f"400 Bad Request{(' (' + context + ')') if context else ''}: "
                    f"{_extract_error_message(body) or resp.text}",
                    status_code=400,
                    response_body=body,
                )

            if status == 409:
                body = _safe_json(resp)
                raise EntraIdConflictError(
                    f"409 Conflict{(' (' + context + ')') if context else ''}: "
                    f"{_extract_error_message(body) or resp.text}",
                    status_code=409,
                    response_body=body,
                )

            if status >= 400:
                body = _safe_json(resp)
                raise EntraIdError(
                    f"HTTP {status}{(' (' + context + ')') if context else ''}: "
                    f"{_extract_error_message(body) or resp.text}",
                    status_code=status,
                    response_body=body,
                )

            if status == 204 or not expect_json:
                return None
            if not resp.content:
                return None
            try:
                return resp.json()
            except Exception:
                return None

        # exhausted retries
        if last_exc:
            raise last_exc
        raise EntraIdError("request exhausted retries with no response", status_code=0)

    # ── Convenience wrappers ────────────────────────────────────────────────

    async def get(
        self, path: str, *, params: Optional[Dict[str, Any]] = None, context: str = ""
    ) -> Any:
        return await self._request("GET", path, params=params, context=context)

    async def post(
        self,
        path: str,
        *,
        json_body: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
        expect_json: bool = True,
    ) -> Any:
        return await self._request(
            "POST",
            path,
            params=params,
            json_body=json_body,
            context=context,
            expect_json=expect_json,
        )

    async def patch(
        self,
        path: str,
        *,
        json_body: Optional[Any] = None,
        context: str = "",
    ) -> Any:
        return await self._request(
            "PATCH", path, json_body=json_body, context=context, expect_json=False
        )

    async def delete(self, path: str, *, context: str = "") -> Any:
        return await self._request("DELETE", path, context=context, expect_json=False)


# Back-compat alias for older imports.
EntraIDHTTPClient = EntraIdHTTPClient


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_json(resp: httpx.Response) -> Dict[str, Any]:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return body
        return {"raw": body}
    except Exception:
        return {}


def _extract_error_message(body: Dict[str, Any]) -> str:
    if not isinstance(body, dict):
        return ""
    error_obj = body.get("error")
    if isinstance(error_obj, dict):
        return str(error_obj.get("message", "")) or str(error_obj)
    if isinstance(error_obj, str):
        return error_obj
    # Token endpoint shape
    desc = body.get("error_description")
    if desc:
        return str(desc)
    return ""


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compute_backoff(attempt: int) -> float:
    return min(
        _RETRY_DELAY_S * (_BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
        _MAX_RETRY_DELAY_S,
    )
