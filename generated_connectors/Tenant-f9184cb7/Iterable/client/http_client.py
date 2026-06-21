"""All Iterable API HTTP calls — zero business logic, zero normalization.

Single owner of:
  - `httpx.AsyncClient` lifecycle (per-call client, no global pool)
  - `Api-Key` auth header injection
  - 429 / 5xx retry-with-backoff (Retry-After aware)
  - HTTP-status → typed connector-exception mapping
  - Plain-text response handling (used by `/lists/getUsers` export)

All methods return the raw decoded JSON body (or plain text when the
endpoint streams `text/plain`). Normalization belongs in
`helpers/normalizer.py`, not here.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, Optional, Union

import httpx
import structlog

from exceptions import (
    IterableAuthError,
    IterableBadRequestError,
    IterableConflictError,
    IterableError,
    IterableNetworkError,
    IterableNotFoundError,
    IterableRateLimitError,
    IterableServerError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.iterable.com/api"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds
_RETRY_AFTER_CAP = 30  # seconds


class IterableHTTPClient:
    """Thin async HTTP client for the Iterable REST API.

    Args:
        api_key: Iterable server-side API key. Sent as the `Api-Key` header.
        base_url: API root, defaults to `https://api.iterable.com/api`.
                  Override with `https://api.eu.iterable.com/api` for EU.
        timeout:  Per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key or ""
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout

    # ── Internal plumbing ────────────────────────────────────────────────

    def _headers(self, accept: str = "application/json") -> Dict[str, str]:
        return {
            "Api-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": accept,
        }

    def _raise_for_status(self, resp: httpx.Response, context: str = "") -> None:
        """Map HTTP error codes to typed connector exceptions."""
        status = resp.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = resp.json()
        except Exception:
            body = {"raw": resp.text}

        message = ""
        if isinstance(body, dict):
            message = (
                body.get("msg")
                or body.get("message")
                or body.get("error")
                or body.get("clientErrorCode")
                or ""
            )
            if not isinstance(message, str):
                message = str(message)
        if not message:
            message = resp.text or f"HTTP {status}"

        suffix = f" [{context}]" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status in (401, 403):
            raise IterableAuthError(
                f"{status} Unauthorized{suffix}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 400:
            raise IterableBadRequestError(
                f"400 Bad Request{suffix}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 404:
            raise IterableNotFoundError(
                f"404 Not Found{suffix}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise IterableConflictError(
                f"409 Conflict{suffix}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                retry_after_s = (
                    float(retry_after) if retry_after is not None else 5.0
                )
            except (TypeError, ValueError):
                retry_after_s = 5.0
            raise IterableRateLimitError(
                f"429 Too Many Requests{suffix}: {message}",
                retry_after_s=retry_after_s,
            )
        if 500 <= status < 600:
            raise IterableServerError(
                f"HTTP {status}{suffix}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise IterableError(
            f"HTTP {status}{suffix}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
        accept: str = "application/json",
        parse: str = "json",
    ) -> Union[Dict[str, Any], str]:
        """Single retry-aware request entry point.

        Retries on 429 and 5xx using exponential backoff with jitter, up to
        `_MAX_RETRIES` attempts. Non-retryable 4xx errors raise immediately.
        `Retry-After` headers are honoured (capped at `_RETRY_AFTER_CAP`).
        """
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers(accept=accept)
        last_exc: Optional[BaseException] = None

        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt + 1 >= _MAX_RETRIES:
                    logger.warning(
                        "iterable.http.timeout",
                        context=context,
                        attempts=_MAX_RETRIES,
                    )
                    raise IterableNetworkError(
                        f"Timeout after {_MAX_RETRIES} attempts [{context}]: {exc}"
                    ) from exc
                await asyncio.sleep(
                    _BACKOFF_BASE * (2 ** attempt) + random.random() * 0.1
                )
                continue
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt + 1 >= _MAX_RETRIES:
                    logger.warning(
                        "iterable.http.network_error",
                        context=context,
                        attempts=_MAX_RETRIES,
                        error=str(exc),
                    )
                    raise IterableNetworkError(
                        f"Network error after {_MAX_RETRIES} attempts "
                        f"[{context}]: {exc}"
                    ) from exc
                await asyncio.sleep(
                    _BACKOFF_BASE * (2 ** attempt) + random.random() * 0.1
                )
                continue

            # Retry on 429 + 5xx
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt + 1 < _MAX_RETRIES:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        delay = min(int(retry_after), _RETRY_AFTER_CAP)
                    else:
                        delay = (
                            _BACKOFF_BASE * (2 ** attempt)
                            + random.random() * 0.1
                        )
                    logger.warning(
                        "iterable.http.retry",
                        status=resp.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                # Last attempt: raise the typed exception
                self._raise_for_status(resp, context)

            # Non-retryable: either success or terminal 4xx
            self._raise_for_status(resp, context)
            if resp.status_code == 204 or not resp.content:
                return {} if parse == "json" else ""
            if parse == "text":
                return resp.text or ""
            try:
                return resp.json()
            except Exception:
                # Some Iterable endpoints stream text/plain even when the
                # request asked for JSON — fall back to raw text wrapped.
                return {"raw": resp.text}

        # Should be unreachable — _MAX_RETRIES is always > 0
        raise IterableNetworkError(
            f"Exhausted retries [{context}]: {last_exc}"
        )

    # ── Public verbs ────────────────────────────────────────────────────

    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
        parse: str = "json",
        accept: str = "application/json",
    ) -> Union[Dict[str, Any], str]:
        return await self._request(
            "GET", path, params=params, context=context, parse=parse, accept=accept
        )

    async def post(
        self,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
        parse: str = "json",
    ) -> Union[Dict[str, Any], str]:
        return await self._request(
            "POST",
            path,
            params=params,
            json_body=json_body,
            context=context,
            parse=parse,
        )

    async def put(
        self,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request(
            "PUT",
            path,
            params=params,
            json_body=json_body,
            context=context,
        )  # type: ignore[return-value]

    async def delete(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request(
            "DELETE", path, params=params, context=context
        )  # type: ignore[return-value]
