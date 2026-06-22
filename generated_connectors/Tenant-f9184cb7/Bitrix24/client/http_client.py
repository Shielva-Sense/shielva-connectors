"""All Bitrix24 REST HTTP calls — zero business logic, zero normalization.

httpx async client. Bitrix24 REST shape:

    POST {base_url}{method}.json
      body: application/json

where `{base_url}` is either:

    - an inbound webhook URL:  https://{portal}.bitrix24.com/rest/{uid}/{code}/
    - an OAuth REST base:      https://{portal}.bitrix24.com/rest/?auth={access_token}

The connector boundary is `Bitrix24HTTPClient.call(method, payload)` — auth +
retry are owned here. Bitrix24's quirks:

  * HTTP 200 with `{"error": "QUERY_LIMIT_EXCEEDED"}` → treat as 429.
  * HTTP 200 with `{"error": "expired_token"|"invalid_token"|"NO_AUTH_FOUND"}` → 401.
  * Otherwise the response envelope is `{result, total?, next?, time}`.

Retry on 429/5xx with exponential backoff (3 attempts).
"""
import asyncio
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
import structlog

from exceptions import (
    Bitrix24AuthError,
    Bitrix24Error,
    Bitrix24NetworkError,
    Bitrix24NotFound,
    Bitrix24RateLimitError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds

# Embedded-error tokens that Bitrix24 returns in a HTTP 200 body.
_AUTH_ERROR_TOKENS = {
    "expired_token",
    "invalid_token",
    "invalid_grant",
    "NO_AUTH_FOUND",
    "ACCESS_DENIED",
    "INVALID_CREDENTIALS",
    "PORTAL_DELETED",
}
_RATE_LIMIT_TOKENS = {
    "QUERY_LIMIT_EXCEEDED",
    "OPERATION_TIME_LIMIT",
}
_NOT_FOUND_TOKENS = {
    "ERROR_NOT_FOUND",
    "NOT_FOUND",
}


class Bitrix24HTTPClient:
    """Thin async HTTP client for the Bitrix24 REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        webhook_url: str = "",
        access_token: str = "",
        base_url: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        # Strip trailing slash so we can append `{method}.json` cleanly.
        self._webhook_url = (webhook_url or "").rstrip("/")
        self._access_token = access_token or ""
        # Explicit base_url overrides whatever was derived from webhook_url.
        self._base_url = (base_url or "").rstrip("/")
        self._timeout = timeout

    # ── URL + headers ──────────────────────────────────────────────────────

    def _resolve_base(self) -> str:
        """Return the REST base URL for the current credential mode.

        - Webhook mode: `https://{portal}.bitrix24.com/rest/{uid}/{code}` (no slash).
        - OAuth mode:   `https://{portal}.bitrix24.com/rest`. The access_token is
          appended as a `?auth=` query param at request time.
        """
        if self._base_url:
            return self._base_url
        if self._webhook_url:
            return self._webhook_url
        return ""

    def _build_url(self, method: str) -> str:
        base = self._resolve_base()
        if not base:
            raise Bitrix24Error("Bitrix24 base URL is not configured")
        method = method.lstrip("/")
        if not method.endswith(".json"):
            method = f"{method}.json"
        return f"{base}/{method}"

    def _build_params(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(params or {})
        # OAuth mode — append the token. Webhook mode does not need it.
        if self._access_token and not self._webhook_url:
            merged.setdefault("auth", self._access_token)
        return merged

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @property
    def portal(self) -> str:
        """Best-effort portal subdomain — useful for logging."""
        base = self._resolve_base()
        if not base:
            return ""
        host = urlparse(base).hostname or ""
        return host.split(".")[0] if host else ""

    # ── Error classification ───────────────────────────────────────────────

    def _raise_for_embedded_error(
        self,
        body: Dict[str, Any],
        *,
        context: str,
    ) -> None:
        """Raise the right exception for a Bitrix24 200-OK error envelope."""
        err_raw = body.get("error")
        err = str(err_raw or "").strip()
        if not err:
            return
        desc = body.get("error_description") or err
        if not isinstance(desc, str):
            desc = str(desc)
        ctx = f": {context}" if context else ""
        if err in _AUTH_ERROR_TOKENS:
            raise Bitrix24AuthError(
                f"{err}{ctx}: {desc}",
                status_code=401,
                response_body=body,
            )
        if err in _RATE_LIMIT_TOKENS:
            raise Bitrix24RateLimitError(
                f"{err}{ctx}: {desc}",
            )
        if err in _NOT_FOUND_TOKENS:
            raise Bitrix24NotFound(
                f"{err}{ctx}: {desc}",
                status_code=404,
                response_body=body,
            )
        raise Bitrix24Error(
            f"{err}{ctx}: {desc}",
            status_code=0,
            response_body=body,
        )

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
            err_obj = body.get("error")
            if isinstance(err_obj, dict):
                message = err_obj.get("message") or str(err_obj)
            else:
                message = (
                    body.get("error_description")
                    or body.get("error")
                    or body.get("message")
                    or str(body)
                )
            if not isinstance(message, str):
                message = str(message)
            envelope: Dict[str, Any] = body
        else:
            message = str(body)
            envelope = {"raw": body}

        ctx = f": {context}" if context else ""
        if status in (401, 403):
            raise Bitrix24AuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=envelope,
            )
        if status == 404:
            raise Bitrix24NotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=envelope,
            )
        if status == 429:
            raise Bitrix24RateLimitError(
                f"429 Too Many Requests{ctx}: {message}",
            )
        raise Bitrix24Error(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=envelope,
        )

    # ── Request core ───────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
        url = path if path.startswith("http") else f"{self._resolve_base()}{path}"
        headers = self._headers()
        send_params = self._build_params(params)

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=send_params or None,
                        json=json_body,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "bitrix24.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                await self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    body = response.json()
                except Exception:
                    return {"raw": response.text}
                if isinstance(body, dict):
                    # Embedded-error envelope (HTTP 200 with `error`).
                    try:
                        self._raise_for_embedded_error(body, context=context)
                    except Bitrix24RateLimitError:
                        if attempt < _MAX_RETRIES - 1:
                            delay = _BACKOFF_BASE * (2 ** attempt)
                            logger.warning(
                                "bitrix24.http.embedded_rate_limit_retry",
                                attempt=attempt + 1,
                                delay=delay,
                                context=context,
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise
                return body
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "bitrix24.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise Bitrix24NetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise Bitrix24NetworkError(str(last_exc)) from last_exc
        raise Bitrix24NetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Public surface ─────────────────────────────────────────────────────

    async def call(
        self,
        method: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generic Bitrix24 REST call.

        `method` is the dotted REST name (e.g. `crm.lead.list`). The client
        appends `.json` and sends `payload` as the JSON body.
        """
        url = self._build_url(method)
        return await self._request(
            "POST",
            url,
            json_body=payload or {},
            context=method,
        )

    async def user_current(self) -> Dict[str, Any]:
        """`user.current` — identifies the caller and is the cheapest health probe."""
        return await self.call("user.current")
