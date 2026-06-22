"""All Adobe Sign API HTTP calls — zero business logic, zero normalization.

httpx async client. The Adobe Sign REST API v6 expects:
  Authorization: Bearer <access_token>
  Accept:        application/json
  Content-Type:  application/json   (when sending a body)

The connector lives on a shard-specific origin discovered via ``GET /baseUris``.
``set_base_url()`` lets the connector swap the active origin after authorize().

Retry on 429/5xx with exponential backoff.
"""
import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    AdobeSignAuthError,
    AdobeSignBadRequestError,
    AdobeSignConflictError,
    AdobeSignError,
    AdobeSignNotFoundError,
    AdobeSignRateLimitError,
    AdobeSignServerError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_API_BASE = "https://api.na1.adobesign.com/api/rest/v6"
_DEFAULT_OAUTH_HOST = "https://secure.na1.adobesign.com"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class AdobeSignHTTPClient:
    """Thin async HTTP client for the Adobe Sign REST API v6.

    All methods are awaitable and return raw response dicts (or bytes for
    binary endpoints such as ``combinedDocument``). Auth + retry are owned
    here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        access_token: str = "",
        base_url: str = _DEFAULT_API_BASE,
        oauth_host: str = _DEFAULT_OAUTH_HOST,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._access_token = access_token or ""
        self._base_url = (base_url or _DEFAULT_API_BASE).rstrip("/")
        self._oauth_host = (oauth_host or _DEFAULT_OAUTH_HOST).rstrip("/")
        self._timeout = timeout

    # ── URL management ─────────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return self._base_url

    def set_base_url(self, base_url: str) -> None:
        """Replace the active API base URL — used after shard discovery."""
        if base_url:
            self._base_url = base_url.rstrip("/")

    def set_access_token(self, access_token: str) -> None:
        self._access_token = access_token or ""

    def set_oauth_host(self, oauth_host: str) -> None:
        if oauth_host:
            self._oauth_host = oauth_host.rstrip("/")

    # ── Headers ────────────────────────────────────────────────────────────

    def _headers(self, *, json_body: bool = True) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    # ── Error classification ───────────────────────────────────────────────

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
                or body.get("code")
                or body.get("error_description")
                or body.get("error")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        body_dict = body if isinstance(body, dict) else {"raw": body}
        ctx = f": {context}" if context else ""

        if status in (401, 403):
            raise AdobeSignAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 400:
            raise AdobeSignBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 404:
            raise AdobeSignNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise AdobeSignConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after) if retry_after else 5.0
            except (TypeError, ValueError):
                retry_after_s = 5.0
            raise AdobeSignRateLimitError(
                f"429 Rate Limited{ctx}: {message}",
                retry_after_s=retry_after_s,
                response_body=body_dict,
            )
        if 500 <= status < 600:
            raise AdobeSignServerError(
                f"{status} Server Error{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise AdobeSignError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── Internal request ───────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
        return_bytes: bool = False,
    ) -> Any:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers(json_body=json_body is not None)

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "adobe_sign.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                await self._raise_for_status(response, context=context)
                if return_bytes:
                    return response.content or b""
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
                        "adobe_sign.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise AdobeSignServerError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise AdobeSignServerError(str(last_exc)) from last_exc
        raise AdobeSignServerError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── OAuth token endpoints ──────────────────────────────────────────────

    async def exchange_code_for_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        auth_code: str,
        redirect_uri: str,
    ) -> Dict[str, Any]:
        """``POST {oauth_host}/oauth/v2/token`` — exchange auth code for tokens."""
        url = f"{self._oauth_host}/oauth/v2/token"
        data = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": auth_code,
            "redirect_uri": redirect_uri,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data=data,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AdobeSignServerError(f"Token exchange transport error: {exc}") from exc

        await self._raise_for_status(response, context="exchange_code_for_token")
        return response.json() if response.content else {}

    async def refresh_access_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> Dict[str, Any]:
        """``POST {oauth_host}/oauth/v2/refresh`` — refresh an access token."""
        url = f"{self._oauth_host}/oauth/v2/refresh"
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data=data,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AdobeSignServerError(f"Token refresh transport error: {exc}") from exc

        await self._raise_for_status(response, context="refresh_access_token")
        return response.json() if response.content else {}

    # ── Shard discovery ────────────────────────────────────────────────────

    async def get_base_uris(self) -> Dict[str, Any]:
        """``GET /baseUris`` — discover the caller's shard-specific endpoints."""
        return await self._request("GET", "/baseUris", context="get_base_uris")

    # ── Users ──────────────────────────────────────────────────────────────

    async def get_me(self) -> Dict[str, Any]:
        """``GET /users/me`` — current user profile (used as health-check probe)."""
        return await self._request("GET", "/users/me", context="get_me")

    async def list_users(self, *, cursor: Optional[str] = None) -> Dict[str, Any]:
        """``GET /users``."""
        params: Dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", "/users", params=params or None, context="list_users",
        )

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """``GET /users/{userId}``."""
        return await self._request(
            "GET", f"/users/{user_id}", context=f"get_user({user_id})",
        )

    # ── Agreements ─────────────────────────────────────────────────────────

    async def create_agreement(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /agreements``."""
        return await self._request(
            "POST",
            "/agreements",
            json_body=payload,
            context="create_agreement",
        )

    async def get_agreement(self, agreement_id: str) -> Dict[str, Any]:
        """``GET /agreements/{agreementId}``."""
        return await self._request(
            "GET",
            f"/agreements/{agreement_id}",
            context=f"get_agreement({agreement_id})",
        )

    async def list_agreements(
        self,
        *,
        cursor: Optional[str] = None,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """``GET /agreements``."""
        params: Dict[str, Any] = {"pageSize": page_size}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", "/agreements", params=params, context="list_agreements",
        )

    async def send_reminder(
        self,
        agreement_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """``POST /agreements/{agreementId}/reminders``."""
        return await self._request(
            "POST",
            f"/agreements/{agreement_id}/reminders",
            json_body=payload,
            context=f"send_reminder({agreement_id})",
        )

    async def cancel_agreement(
        self,
        agreement_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """``PUT /agreements/{agreementId}/state`` — set state to ``CANCELLED``."""
        return await self._request(
            "PUT",
            f"/agreements/{agreement_id}/state",
            json_body=payload,
            context=f"cancel_agreement({agreement_id})",
        )

    async def download_agreement(self, agreement_id: str) -> bytes:
        """``GET /agreements/{agreementId}/combinedDocument`` — returns PDF bytes."""
        return await self._request(
            "GET",
            f"/agreements/{agreement_id}/combinedDocument",
            context=f"download_agreement({agreement_id})",
            return_bytes=True,
        )

    # ── Library documents ──────────────────────────────────────────────────

    async def list_library_documents(
        self,
        *,
        cursor: Optional[str] = None,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """``GET /libraryDocuments``."""
        params: Dict[str, Any] = {"pageSize": page_size}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET",
            "/libraryDocuments",
            params=params,
            context="list_library_documents",
        )

    async def get_library_document(self, library_document_id: str) -> Dict[str, Any]:
        """``GET /libraryDocuments/{libraryDocumentId}``."""
        return await self._request(
            "GET",
            f"/libraryDocuments/{library_document_id}",
            context=f"get_library_document({library_document_id})",
        )

    # ── Workflows ──────────────────────────────────────────────────────────

    async def list_workflows(self, *, cursor: Optional[str] = None) -> Dict[str, Any]:
        """``GET /workflows``."""
        params: Dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", "/workflows", params=params or None, context="list_workflows",
        )

    # ── Webhooks ───────────────────────────────────────────────────────────

    async def list_webhooks(self, *, cursor: Optional[str] = None) -> Dict[str, Any]:
        """``GET /webhooks``."""
        params: Dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", "/webhooks", params=params or None, context="list_webhooks",
        )

    async def create_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /webhooks``."""
        return await self._request(
            "POST",
            "/webhooks",
            json_body=payload,
            context="create_webhook",
        )
