"""All Make API HTTP calls — zero business logic, zero normalization.

Make's API base URL is region-scoped: https://{zone}.make.com/api/v2 where
{zone} is one of eu1, eu2, us1, us2, … . The connector resolves the base URL
from install-time config and passes it to this client.

Authentication is a long-lived API token issued from the Make user profile.
The wire format is `Authorization: Token {api_token}` (Make-specific — NOT
`Bearer`).
"""
import asyncio
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    MakeAuthError,
    MakeError,
    MakeNetworkError,
    MakeNotFound,
    MakeRateLimitError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_RETRIES = 3
_BACKOFF_BASE_S = 0.5
_BACKOFF_MAX_S = 8.0


class MakeHTTPClient:
    """Thin async HTTP client for the Make REST API (v2).

    All public methods return parsed JSON dicts. Retry on 429/5xx with
    exponential backoff + jitter; surface 401/403 as MakeAuthError, 404 as
    MakeNotFound, transport errors as MakeNetworkError.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_RETRIES,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._timeout = timeout
        self._max_retries = max_retries

    # ── Public API ─────────────────────────────────────────────────────────

    def set_token(self, api_token: str) -> None:
        """Rotate the API token used for subsequent requests."""
        self._api_token = api_token

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self._request("POST", path, json=json, params=params)

    async def patch(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self._request("PATCH", path, json=json, params=params)

    async def delete(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._request("DELETE", path, params=params)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Token {self._api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = self._url(path)
        headers = self._headers()
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method, url, headers=headers, params=params, json=json
                    )

                if resp.status_code < 400:
                    if resp.status_code == 204 or not resp.content:
                        return {}
                    try:
                        return resp.json()
                    except Exception:
                        return {"raw": resp.text}

                # Retry on 429 + 5xx
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = self._error_from_response(resp)
                    if attempt >= self._max_retries:
                        raise last_exc
                    delay = self._compute_delay(attempt, resp.headers.get("Retry-After"))
                    logger.warning(
                        "make.http.retry",
                        method=method,
                        url=url,
                        status=resp.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                # Non-retryable 4xx
                raise self._error_from_response(resp)

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = MakeNetworkError(f"{method} {url}: {exc}")
                if attempt >= self._max_retries:
                    raise last_exc
                delay = self._compute_delay(attempt, None)
                logger.warning(
                    "make.http.transport_retry",
                    method=method,
                    url=url,
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
                continue

        # Should not reach here; safety net.
        if last_exc:
            raise last_exc
        raise MakeError(f"{method} {url}: exhausted retries with no response")

    @staticmethod
    def _compute_delay(attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                pass
        delay = _BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0.0, 0.25)
        return min(delay, _BACKOFF_MAX_S)

    @staticmethod
    def _error_from_response(resp: httpx.Response) -> MakeError:
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}

        if isinstance(body, dict):
            message = (
                body.get("message")
                or body.get("detail")
                or (body.get("error") if isinstance(body.get("error"), str) else None)
                or str(body)
            )
        else:
            message = str(body)

        status = resp.status_code
        if status in (401, 403):
            return MakeAuthError(
                f"{status} {message}", status_code=status, response_body=body if isinstance(body, dict) else {}
            )
        if status == 404:
            return MakeNotFound(
                f"404 {message}", status_code=status, response_body=body if isinstance(body, dict) else {}
            )
        if status == 429:
            return MakeRateLimitError(
                f"429 {message}", status_code=status, response_body=body if isinstance(body, dict) else {}
            )
        if status >= 500:
            return MakeNetworkError(
                f"{status} {message}", status_code=status, response_body=body if isinstance(body, dict) else {}
            )
        return MakeError(
            f"HTTP {status}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {},
        )
