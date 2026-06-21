"""All Lightspeed Retail (R-Series) HTTP calls — zero business logic, zero normalization.

The client is async (httpx), Bearer-authenticated, and honors the Lightspeed
leaky-bucket rate-limit header ``X-LS-API-Bucket-Level`` (format ``"current/max"``).
On 401, it invokes an optional ``refresh_cb`` to obtain a fresh access token and
retries the request once. On 429 / 5xx, it retries with exponential backoff +
respect for ``Retry-After``.

URL shape::

    https://api.lightspeedapp.com/API/V3/Account/{account_id}/<Resource>.json

The base URL is composed by ``LightspeedConnector._compose_base_url`` — this
class is otherwise account-agnostic.
"""
import asyncio
import random
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx
import structlog

from exceptions import (
    LightspeedAuthError,
    LightspeedBadRequestError,
    LightspeedConflictError,
    LightspeedError,
    LightspeedNetworkError,
    LightspeedNotFound,
    LightspeedRateLimitError,
    LightspeedServerError,
)

logger = structlog.get_logger(__name__)

# Default Lightspeed Retail public API root (without account segment).
_LIGHTSPEED_PUBLIC_ROOT = "https://api.lightspeedapp.com/API/V3"

# Retry knobs — change here, nowhere else.
_RETRY_DELAY_S: float = 1.0
_BACKOFF_FACTOR: float = 2.0
_MAX_RETRY_DELAY_S: float = 32.0
_MAX_RETRIES: int = 4

# Refresh callback signature: returns the new access token string.
RefreshCallback = Callable[[], Awaitable[str]]


class LightspeedHTTPClient:
    """Thin async HTTP client for the Lightspeed Retail REST API.

    All methods are awaitable and return raw response dicts. Auth + retry +
    refresh-on-401 are owned here — the connector layer only orchestrates
    business calls.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        max_retries: int = _MAX_RETRIES,
    ):
        # ``base_url`` already includes the ``/API/V3/Account/{account_id}`` suffix
        # because the connector composed it from config.
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries

    # ── auth header builder ────────────────────────────────────────────────

    @staticmethod
    def _auth_headers(access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

    # ── response handling ──────────────────────────────────────────────────

    @staticmethod
    def _parse_bucket_header(response: httpx.Response) -> Optional[float]:
        """Return the fraction of the leaky bucket currently consumed (0..1).

        Lightspeed returns ``X-LS-API-Bucket-Level: "current/max"`` (e.g. ``"30/60"``).
        A fraction near 1.0 means we are about to be 429'd.
        """
        raw = response.headers.get("X-LS-API-Bucket-Level")
        if not raw or "/" not in raw:
            return None
        try:
            current_s, max_s = raw.split("/", 1)
            current = float(current_s)
            cap = float(max_s)
            if cap <= 0:
                return None
            return min(max(current / cap, 0.0), 1.0)
        except (ValueError, ZeroDivisionError):
            return None

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        """Map HTTP error codes to connector exceptions."""
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {}
        ctx = f": {context}" if context else ""
        message = ""
        if isinstance(body, dict):
            message = (
                body.get("message")
                or body.get("error")
                or body.get("httpMessage")
                or ""
            )
        message = message or response.text[:200] or f"HTTP {status}"

        if status == 400:
            raise LightspeedBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 401 or status == 403:
            raise LightspeedAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 404:
            raise LightspeedNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 409:
            raise LightspeedConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 429:
            retry_after = self._retry_after_seconds(response) or 1.0
            raise LightspeedRateLimitError(
                f"429 Rate Limited{ctx}: {message}",
                retry_after_s=retry_after,
            )
        if status >= 500:
            raise LightspeedServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {},
            )
        raise LightspeedError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {},
        )

    async def _throttle_if_full(self, response: httpx.Response) -> None:
        """If the leaky bucket is ≥90% full, sleep proactively before the next call."""
        fraction = self._parse_bucket_header(response)
        if fraction is None:
            return
        if fraction >= 0.9:
            logger.warning(
                "lightspeed.bucket_high",
                fraction=fraction,
                bucket=response.headers.get("X-LS-API-Bucket-Level"),
            )
            await asyncio.sleep(1.0)

    # ── core request ───────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
        refresh_cb: Optional[RefreshCallback] = None,
        _already_refreshed: bool = False,
    ) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}/{path.lstrip('/')}"
        headers = self._auth_headers(access_token)
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        attempt = 0
        last_exc: Exception = LightspeedError("no attempts made")
        while attempt <= self._max_retries:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
            except httpx.HTTPError as exc:
                last_exc = LightspeedNetworkError(
                    f"transport error{': ' + context if context else ''}: {exc}"
                )
                if attempt == self._max_retries:
                    raise last_exc
                await asyncio.sleep(self._backoff(attempt))
                attempt += 1
                continue

            # Successful HTTP exchange — inspect status.
            if response.status_code == 401 and refresh_cb is not None and not _already_refreshed:
                logger.info("lightspeed.refresh_on_401", context=context)
                new_token = await refresh_cb()
                return await self._request(
                    method,
                    path,
                    new_token,
                    params=params,
                    json_body=json_body,
                    context=context,
                    refresh_cb=refresh_cb,
                    _already_refreshed=True,
                )

            if response.status_code == 429 or response.status_code >= 500:
                retry_after = self._retry_after_seconds(response) or self._backoff(attempt)
                logger.warning(
                    "lightspeed.transient_status",
                    status=response.status_code,
                    retry_after=retry_after,
                    context=context,
                )
                last_exc = LightspeedNetworkError(
                    f"HTTP {response.status_code}{': ' + context if context else ''}"
                )
                if attempt == self._max_retries:
                    self._raise_for_status(response, context)
                await asyncio.sleep(retry_after)
                attempt += 1
                continue

            # Final non-retried branch: success or non-retryable error
            self._raise_for_status(response, context)
            await self._throttle_if_full(response)
            try:
                return response.json()
            except Exception:
                return {}

        # Fell off the loop without returning → re-raise the last transient error
        raise last_exc

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(
            _RETRY_DELAY_S * (_BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
            _MAX_RETRY_DELAY_S,
        )

    # ── public endpoints ───────────────────────────────────────────────────

    async def get(
        self,
        path: str,
        access_token: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        refresh_cb: Optional[RefreshCallback] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request(
            "GET", path, access_token,
            params=params, refresh_cb=refresh_cb, context=context or f"GET {path}",
        )

    async def post(
        self,
        path: str,
        access_token: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        refresh_cb: Optional[RefreshCallback] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request(
            "POST", path, access_token,
            json_body=json_body, refresh_cb=refresh_cb, context=context or f"POST {path}",
        )

    async def put(
        self,
        path: str,
        access_token: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        refresh_cb: Optional[RefreshCallback] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request(
            "PUT", path, access_token,
            json_body=json_body, refresh_cb=refresh_cb, context=context or f"PUT {path}",
        )

    async def post_form_data(
        self,
        url: str,
        payload: Dict[str, str],
        context: str = "post_form_data",
    ) -> Dict[str, Any]:
        """POST x-www-form-urlencoded — used for OAuth code exchange + refresh.

        Returns the parsed JSON response. Always targets an absolute URL.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, data=payload)
        except httpx.HTTPError as exc:
            raise LightspeedNetworkError(f"transport error during {context}: {exc}")
        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:
                body = {}
            message = (
                (body.get("error_description") or body.get("error") or response.text[:200])
                if isinstance(body, dict)
                else response.text[:200]
            )
            if response.status_code == 401:
                raise LightspeedAuthError(
                    f"401 Unauthorized: {context}: {message}",
                    status_code=401,
                    response_body=body if isinstance(body, dict) else {},
                )
            raise LightspeedAuthError(
                f"HTTP {response.status_code}: {context}: {message}",
                status_code=response.status_code,
                response_body=body if isinstance(body, dict) else {},
            )
        try:
            return response.json()
        except Exception:
            return {}
