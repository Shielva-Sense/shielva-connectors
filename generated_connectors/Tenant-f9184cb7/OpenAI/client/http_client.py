"""OpenAI HTTP client — single owner of every outbound HTTP call.

`connector.py` MUST go through this client; never constructs httpx requests
directly. Retry policy: bounded retries on 5xx + Retry-After honour on 429.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

import httpx
import structlog

from exceptions import (
    OpenAIAuthError,
    OpenAIBadRequestError,
    OpenAIConflictError,
    OpenAIError,
    OpenAINetworkError,
    OpenAINotFoundError,
    OpenAIRateLimitError,
    OpenAIServerError,
)


logger = structlog.get_logger(__name__)


# Canonical OpenAI base URL (implementation_plan.md §3).
OPENAI_BASE_URL: str = "https://api.openai.com/v1"


def _extract_message(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if msg:
                return str(msg)
        for key in ("message", "detail", "description"):
            if isinstance(body.get(key), str):
                return body[key]
    if isinstance(body, str):
        return body
    return f"OpenAI HTTP error"


def _raise_for_status(resp: httpx.Response) -> None:
    code = resp.status_code
    if code < 400:
        return
    try:
        body = resp.json()
    except ValueError:
        body = {"text": resp.text}
    message = _extract_message(body)
    response_body = body if isinstance(body, dict) else {"raw": body}
    if code == 400:
        raise OpenAIBadRequestError(message, status_code=code, response_body=response_body)
    if code in (401, 403):
        exc = OpenAIAuthError(message, status_code=code, response_body=response_body)
        raise exc
    if code == 404:
        raise OpenAINotFoundError(message, status_code=code, response_body=response_body)
    if code == 409:
        raise OpenAIConflictError(message, status_code=code, response_body=response_body)
    if code == 429:
        try:
            retry_after_s = float(resp.headers.get("Retry-After") or 1.0)
        except ValueError:
            retry_after_s = 1.0
        raise OpenAIRateLimitError(
            message,
            status_code=code,
            response_body=response_body,
            retry_after_s=retry_after_s,
        )
    if 500 <= code < 600:
        raise OpenAIServerError(message, status_code=code, response_body=response_body)
    raise OpenAIError(f"HTTP {code}: {message}", status_code=code, response_body=response_body)


class OpenAIHTTPClient:
    """Async HTTP client scoped to a single OpenAI API key (+ optional org)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = OPENAI_BASE_URL,
        organization_id: str = "",
        timeout_s: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or OPENAI_BASE_URL).rstrip("/")
        self._organization_id = organization_id or ""
        self._timeout_s = float(timeout_s)
        self._max_retries = int(max_retries)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def organization_id(self) -> str:
        return self._organization_id

    def url(self, path: str) -> str:
        """Build a full URL from a path fragment (e.g. ``/models``)."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h: Dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if self._organization_id:
            h["OpenAI-Organization"] = self._organization_id
        if extra:
            h.update(extra)
        return h

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        content: Optional[bytes] = None,
        content_type: Optional[str] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """Send a request with bounded retries on 429 / 5xx / timeout."""
        merged = self._headers(headers)
        # JSON body sets Content-Type explicitly; multipart lets httpx pick.
        if files is None and content is None and json_body is not None:
            merged.setdefault("Content-Type", "application/json")
        if content_type:
            merged["Content-Type"] = content_type
        attempt = 0
        last_exc: Optional[Exception] = None
        full_url = self.url(url) if not url.startswith("http") else url
        while attempt <= self._max_retries:
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    resp = await client.request(
                        method,
                        full_url,
                        headers=merged,
                        json=json_body if (files is None and content is None) else None,
                        params=params,
                        content=content,
                        files=files,
                        data=data,
                    )
                _raise_for_status(resp)
                return resp
            except OpenAIRateLimitError as exc:
                logger.warning(
                    "openai.http.rate_limited",
                    method=method,
                    url=full_url,
                    retry_after_s=exc.retry_after_s,
                )
                last_exc = exc
                await asyncio.sleep(exc.retry_after_s)
            except OpenAIServerError as exc:
                logger.warning(
                    "openai.http.server_error",
                    method=method,
                    url=full_url,
                    attempt=attempt,
                )
                last_exc = exc
                await asyncio.sleep(min(2 ** attempt, 8))
            except (
                OpenAIAuthError,
                OpenAIBadRequestError,
                OpenAINotFoundError,
                OpenAIConflictError,
            ):
                # Non-retryable — propagate.
                raise
            except httpx.TimeoutException as exc:
                logger.warning("openai.http.timeout", method=method, url=full_url, attempt=attempt)
                last_exc = OpenAINetworkError(f"timeout: {exc}")
                await asyncio.sleep(min(2 ** attempt, 8))
            except httpx.RequestError as exc:
                # DNS / connection / TLS — non-retryable, surface as NetworkError.
                raise OpenAINetworkError(f"network error: {exc}") from exc
            attempt += 1
        assert last_exc is not None
        raise last_exc
