"""All EngageBay API HTTP calls — zero business logic, zero normalization.

Uses httpx.AsyncClient (per spec) with a built-in 429 / 5xx exponential-backoff
retry loop. EngageBay auth = HTTP header `Authorization: {api_key}` — no Bearer
prefix (this is the documented EngageBay REST contract).
"""
import asyncio
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    EngageBayAuthError,
    EngageBayError,
    EngageBayNetworkError,
    EngageBayNotFound,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://app.engagebay.com/dev/api/panel"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 3
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class EngageBayHTTPClient:
    """Thin async HTTP client for the EngageBay REST API.

    All methods accept the api_key as the first argument and return raw response
    dicts (or empty dict for 204 No Content). Retry on 429 / 5xx is handled
    internally — callers do not need a `with_retry` wrapper.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries

    # ── Internal helpers ────────────────────────────────────────────────────

    def _auth_headers(self, api_key: str) -> Dict[str, str]:
        # EngageBay convention: bare key in Authorization, no `Bearer ` prefix.
        return {
            "Authorization": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    async def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}

        ctx = f": {context}" if context else ""
        message = ""
        if isinstance(body, dict):
            message = body.get("message") or body.get("error") or str(body)
        else:
            message = str(body)

        if status in (401, 403):
            raise EngageBayAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 404:
            raise EngageBayNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {},
            )
        raise EngageBayError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {},
        )

    @staticmethod
    def _retry_backoff(attempt: int, retry_after: Optional[str] = None) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        # Exponential with jitter, capped at 8s.
        return min(8.0, (2 ** attempt) * 0.5) + random.uniform(0, 0.25)

    async def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Issue a request with 429/5xx retry. Returns parsed JSON (or {} for 204)."""
        url = self._url(path)
        headers = self._auth_headers(api_key)
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                last_exc = exc
                if attempt + 1 >= self._max_retries:
                    raise EngageBayNetworkError(f"transport failure{': ' + context if context else ''}: {exc}") from exc
                await asyncio.sleep(self._retry_backoff(attempt))
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt + 1 < self._max_retries:
                delay = self._retry_backoff(attempt, response.headers.get("Retry-After"))
                logger.info(
                    "engagebay.http.retry",
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
                return response.json()
            except ValueError:
                return {"raw": response.text}

        # Exhausted retries on a retryable status — surface a final error.
        if last_exc is not None:
            raise EngageBayNetworkError(f"exhausted retries{': ' + context if context else ''}: {last_exc}") from last_exc
        raise EngageBayError(f"exhausted retries{': ' + context if context else ''}")

    # ── Public endpoint methods ─────────────────────────────────────────────

    async def get(
        self,
        api_key: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request("GET", path, api_key, params=params, context=context or f"GET {path}")

    async def post(
        self,
        api_key: str,
        path: str,
        json_body: Any = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request("POST", path, api_key, json_body=json_body, context=context or f"POST {path}")

    async def put(
        self,
        api_key: str,
        path: str,
        json_body: Any = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request("PUT", path, api_key, json_body=json_body, context=context or f"PUT {path}")

    async def delete(
        self,
        api_key: str,
        path: str,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request("DELETE", path, api_key, context=context or f"DELETE {path}")
