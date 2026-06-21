"""All OneSignal API HTTP calls — zero business logic, zero normalization.

httpx async client. The OneSignal REST v1 API expects:

    Authorization: Basic <key>          (raw key — NOT base64 — OneSignal quirk)
    Content-Type:   application/json
    Accept:         application/json

The ``Basic`` prefix is literal. Despite RFC 7617 calling for
``Basic base64(user:password)``, OneSignal accepts the raw REST API key (or
User Auth Key) after the prefix. Base64-encoding it returns 401.

Header selection rule:
    - Endpoints that act ON a single app (notifications/players/segments under
      that app) use the APP-level **REST API Key**.
    - Endpoints that operate across the OneSignal account (``GET /apps``,
      ``POST /apps``, ``PUT /apps/{id}``) require the USER-level
      **User Auth Key**.

Retry on 429/5xx with exponential backoff + jitter.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    OneSignalAuthError,
    OneSignalBadRequestError,
    OneSignalConflictError,
    OneSignalError,
    OneSignalNetworkError,
    OneSignalNotFoundError,
    OneSignalRateLimitError,
    OneSignalServerError,
)

logger = structlog.get_logger(__name__)

ONESIGNAL_BASE = "https://onesignal.com/api/v1"
_DEFAULT_TIMEOUT_S = 30.0
_MAX_RETRIES = 3
_BASE_DELAY_S = 0.5
_MAX_DELAY_S = 16.0


class OneSignalHTTPClient:
    """Thin async HTTP client for the OneSignal REST v1 API.

    All public methods accept the key they need explicitly so the connector
    layer owns auth resolution. Methods return raw response dicts.
    """

    BASE_URL = ONESIGNAL_BASE

    def __init__(
        self,
        base_url: str = ONESIGNAL_BASE,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _MAX_RETRIES,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries

    # ── header helpers ────────────────────────────────────────────────────

    @staticmethod
    def _auth_headers(key: str) -> Dict[str, str]:
        """Build the standard OneSignal auth headers.

        The literal string ``"Basic "`` is prepended — but the key itself is
        RAW, not base64-encoded. This is a OneSignal-specific quirk.
        """
        return {
            "Authorization": f"Basic {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ── error mapping ─────────────────────────────────────────────────────

    @staticmethod
    def _extract_message(body: Any) -> str:
        if not isinstance(body, dict):
            return str(body)
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        if isinstance(errors, dict):
            return str(errors)
        return body.get("message") or body.get("error") or str(body)

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        """Map HTTP error codes to connector exceptions."""
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}

        message = self._extract_message(body)
        suffix = f": {context}" if context else ""
        body_for_exc: Dict[str, Any] = body if isinstance(body, dict) else {"raw": body}

        if status == 400:
            raise OneSignalBadRequestError(
                f"400 Bad Request{suffix}: {message}",
                status_code=400,
                response_body=body_for_exc,
            )
        if status == 401:
            raise OneSignalAuthError(
                f"401 Unauthorized{suffix}: {message}",
                status_code=401,
                response_body=body_for_exc,
            )
        if status == 403:
            raise OneSignalAuthError(
                f"403 Forbidden{suffix}: {message}",
                status_code=403,
                response_body=body_for_exc,
            )
        if status == 404:
            raise OneSignalNotFoundError(
                f"404 Not Found{suffix}: {message}",
                status_code=404,
                response_body=body_for_exc,
            )
        if status == 409:
            raise OneSignalConflictError(
                f"409 Conflict{suffix}: {message}",
                status_code=409,
                response_body=body_for_exc,
            )
        if status == 429:
            retry_after = 5.0
            try:
                retry_after = float(response.headers.get("Retry-After", "5"))
            except (TypeError, ValueError):
                retry_after = 5.0
            exc = OneSignalRateLimitError(
                f"429 Rate limit{suffix}: {message}",
                retry_after_s=retry_after,
            )
            exc.response_body = body_for_exc
            raise exc
        if 500 <= status < 600:
            raise OneSignalServerError(
                f"HTTP {status}{suffix}: {message}",
                status_code=status,
                response_body=body_for_exc,
            )
        raise OneSignalError(
            f"HTTP {status}{suffix}: {message}",
            status_code=status,
            response_body=body_for_exc,
        )

    # ── core request with retry on 429/5xx ────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        key: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Send an authenticated request and return parsed JSON.

        Retries on 429 and 5xx with exponential backoff + jitter, up to
        ``self._max_retries`` attempts. Maps transport-level failures to
        ``OneSignalNetworkError``.
        """
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._auth_headers(key)

        attempt = 0
        last_exc: Optional[BaseException] = None
        while attempt <= self._max_retries:
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
                status = response.status_code
                if status == 429 or 500 <= status < 600:
                    if attempt == self._max_retries:
                        self._raise_for_status(response, context)
                    delay = min(
                        _BASE_DELAY_S * (2 ** attempt) + random.uniform(0, 0.3),
                        _MAX_DELAY_S,
                    )
                    logger.warning(
                        "onesignal.http.retry",
                        status=status,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                self._raise_for_status(response, context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    raise OneSignalNetworkError(
                        f"Transport error{': ' + context if context else ''}: {exc}",
                    ) from exc
                delay = min(
                    _BASE_DELAY_S * (2 ** attempt) + random.uniform(0, 0.3),
                    _MAX_DELAY_S,
                )
                logger.warning(
                    "onesignal.http.transport_retry",
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
                attempt += 1

        raise OneSignalError(
            f"request exhausted without resolution: {last_exc}",
            status_code=0,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Apps — require the USER auth key
    # ─────────────────────────────────────────────────────────────────────

    async def list_apps(self, user_auth_key: str) -> List[Dict[str, Any]]:
        """GET /apps."""
        data = await self._request(
            "GET", "/apps", user_auth_key, context="list_apps",
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("apps"), list):
            return data["apps"]
        return []

    async def get_app(self, key: str, app_id: str) -> Dict[str, Any]:
        """GET /apps/{id}.

        Accepts either ``rest_api_key`` (works for the app's own record) or
        ``user_auth_key`` (works for any app in the account). Connector layer
        decides which to pass.
        """
        return await self._request(
            "GET",
            f"/apps/{app_id}",
            key,
            context=f"get_app({app_id})",
        )

    async def create_app(
        self, user_auth_key: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /apps."""
        return await self._request(
            "POST", "/apps", user_auth_key, json_body=payload, context="create_app",
        )

    async def update_app(
        self, user_auth_key: str, app_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /apps/{id}."""
        return await self._request(
            "PUT",
            f"/apps/{app_id}",
            user_auth_key,
            json_body=payload,
            context=f"update_app({app_id})",
        )

    # ─────────────────────────────────────────────────────────────────────
    # Notifications — require the per-app REST API key
    # ─────────────────────────────────────────────────────────────────────

    async def send_notification(
        self, rest_api_key: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /notifications.

        Payload MUST include ``app_id`` matching the REST API key's app.
        """
        return await self._request(
            "POST",
            "/notifications",
            rest_api_key,
            json_body=payload,
            context="send_notification",
        )

    async def cancel_notification(
        self, rest_api_key: str, app_id: str, notification_id: str
    ) -> Dict[str, Any]:
        """DELETE /notifications/{id}?app_id=..."""
        return await self._request(
            "DELETE",
            f"/notifications/{notification_id}",
            rest_api_key,
            params={"app_id": app_id},
            context=f"cancel_notification({notification_id})",
        )

    async def get_notification(
        self, rest_api_key: str, app_id: str, notification_id: str
    ) -> Dict[str, Any]:
        """GET /notifications/{id}?app_id=..."""
        return await self._request(
            "GET",
            f"/notifications/{notification_id}",
            rest_api_key,
            params={"app_id": app_id},
            context=f"get_notification({notification_id})",
        )

    async def list_notifications(
        self,
        rest_api_key: str,
        app_id: str,
        limit: int = 50,
        offset: int = 0,
        kind: Optional[int] = None,
    ) -> Dict[str, Any]:
        """GET /notifications?app_id=..."""
        params: Dict[str, Any] = {
            "app_id": app_id,
            "limit": limit,
            "offset": offset,
        }
        if kind is not None:
            params["kind"] = kind
        return await self._request(
            "GET",
            "/notifications",
            rest_api_key,
            params=params,
            context="list_notifications",
        )

    async def notification_history(
        self,
        rest_api_key: str,
        notification_id: str,
        app_id: str,
        events: str = "sent",
    ) -> Dict[str, Any]:
        """POST /notifications/{id}/history."""
        return await self._request(
            "POST",
            f"/notifications/{notification_id}/history",
            rest_api_key,
            json_body={"events": events, "app_id": app_id},
            context=f"notification_history({notification_id})",
        )

    # ─────────────────────────────────────────────────────────────────────
    # Players (devices) — require the per-app REST API key
    # ─────────────────────────────────────────────────────────────────────

    async def list_devices(
        self,
        rest_api_key: str,
        app_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /players?app_id=..."""
        params = {"app_id": app_id, "limit": limit, "offset": offset}
        return await self._request(
            "GET",
            "/players",
            rest_api_key,
            params=params,
            context="list_devices",
        )

    async def get_device(
        self, rest_api_key: str, app_id: str, player_id: str
    ) -> Dict[str, Any]:
        """GET /players/{id}?app_id=..."""
        return await self._request(
            "GET",
            f"/players/{player_id}",
            rest_api_key,
            params={"app_id": app_id},
            context=f"get_device({player_id})",
        )

    async def create_device(
        self, rest_api_key: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /players."""
        return await self._request(
            "POST",
            "/players",
            rest_api_key,
            json_body=payload,
            context="create_device",
        )

    async def update_device(
        self, rest_api_key: str, player_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /players/{id}."""
        return await self._request(
            "PUT",
            f"/players/{player_id}",
            rest_api_key,
            json_body=payload,
            context=f"update_device({player_id})",
        )

    # ─────────────────────────────────────────────────────────────────────
    # Segments — require the per-app REST API key
    # ─────────────────────────────────────────────────────────────────────

    async def list_segments(
        self, rest_api_key: str, app_id: str
    ) -> Dict[str, Any]:
        """GET /apps/{id}/segments."""
        return await self._request(
            "GET",
            f"/apps/{app_id}/segments",
            rest_api_key,
            context=f"list_segments({app_id})",
        )

    async def create_segment(
        self, rest_api_key: str, app_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /apps/{id}/segments."""
        return await self._request(
            "POST",
            f"/apps/{app_id}/segments",
            rest_api_key,
            json_body=payload,
            context=f"create_segment({app_id})",
        )

    async def delete_segment(
        self, rest_api_key: str, app_id: str, segment_id: str
    ) -> Dict[str, Any]:
        """DELETE /apps/{id}/segments/{segment_id}."""
        return await self._request(
            "DELETE",
            f"/apps/{app_id}/segments/{segment_id}",
            rest_api_key,
            context=f"delete_segment({segment_id})",
        )
