"""All JazzHR API HTTP calls — zero business logic, zero normalization.

JazzHR auth quirk: the API key is passed as a query parameter `?apikey=...`,
NOT as an Authorization header. Every request — GET or POST — must include it.

POST endpoints use `application/x-www-form-urlencoded` payloads (NOT JSON).
"""
import asyncio
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    JazzHRAuthError,
    JazzHRBadRequestError,
    JazzHRError,
    JazzHRNetworkError,
    JazzHRNotFound,
    JazzHRRateLimitError,
    JazzHRServerError,
)

logger = structlog.get_logger(__name__)

_JAZZHR_BASE = "https://api.resumatorapi.com/v1"

# Retry tuning — change here, nowhere else.
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0
DEFAULT_MAX_RETRIES: int = 3


class JazzHRHTTPClient:
    """Thin async HTTP client for the JazzHR (Resumator) REST API.

    Automatically injects `?apikey={api_key}` on every request. POST bodies
    are form-encoded. Retries on 429 and 5xx with jittered exponential backoff.
    All HTTP errors raise typed exceptions from `exceptions.py`.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = _JAZZHR_BASE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = 30.0,
    ) -> None:
        self._api_key: str = api_key or ""
        self._base_url: str = (base_url or _JAZZHR_BASE).rstrip("/")
        self._max_retries: int = max_retries
        self._timeout: float = timeout

    # ── Internal: URL + param assembly ─────────────────────────────────────

    def _full_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _inject_apikey(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge `apikey` into the user-provided params, dropping None values."""
        merged: Dict[str, Any] = {"apikey": self._api_key}
        if params:
            for k, v in params.items():
                if v is None:
                    continue
                merged[k] = v
        return merged

    # ── Internal: request execution with retries ───────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        form: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        url = self._full_url(path)
        query = self._inject_apikey(params)

        last_exc: Exception = JazzHRNetworkError("request never executed")
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    if method == "GET":
                        response = await client.get(url, params=query)
                    elif method == "POST":
                        clean_form = {
                            k: v for k, v in (form or {}).items() if v is not None
                        }
                        response = await client.post(
                            url, params=query, data=clean_form
                        )
                    else:
                        raise JazzHRError(f"Unsupported method: {method}")

                status = response.status_code

                # 429 / 5xx → retry
                if status == 429 or 500 <= status < 600:
                    last_exc = self._build_error(status, response, context)
                    if attempt == self._max_retries:
                        raise last_exc
                    await self._sleep_backoff(attempt, response)
                    continue

                if status >= 400:
                    raise self._build_error(status, response, context)

                # JazzHR returns JSON on success. Some endpoints return [] / {} / "ok".
                try:
                    return response.json()
                except ValueError:
                    text = response.text or ""
                    return {"raw_text": text}

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = JazzHRNetworkError(
                    f"Network error{': ' + context if context else ''}: {exc}"
                )
                if attempt == self._max_retries:
                    raise last_exc
                await self._sleep_backoff(attempt, None)

        raise last_exc

    # ── Error building ─────────────────────────────────────────────────────

    def _build_error(
        self, status: int, response: httpx.Response, context: str
    ) -> JazzHRError:
        try:
            body = response.json()
        except ValueError:
            body = {"raw_text": response.text}
        message = self._extract_message(body) or response.text or f"HTTP {status}"
        ctx_suffix = f": {context}" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status == 400:
            return JazzHRBadRequestError(
                f"400 Bad Request{ctx_suffix}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status in (401, 403):
            return JazzHRAuthError(
                f"{status} Unauthorized{ctx_suffix}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            return JazzHRNotFound(
                f"404 Not Found{ctx_suffix}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = self._extract_retry_after(response)
            err = JazzHRRateLimitError(
                f"429 Rate Limited{ctx_suffix}: {message}",
                retry_after_s=retry_after,
            )
            err.response_body = body_dict
            return err
        if 500 <= status < 600:
            return JazzHRServerError(
                f"HTTP {status}{ctx_suffix}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        return JazzHRError(
            f"HTTP {status}{ctx_suffix}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    @staticmethod
    def _extract_message(body: Any) -> str:
        if isinstance(body, dict):
            for key in ("error", "message", "detail"):
                v = body.get(key)
                if isinstance(v, str) and v:
                    return v
                if isinstance(v, dict):
                    inner = v.get("message")
                    if isinstance(inner, str) and inner:
                        return inner
        if isinstance(body, list) and body:
            return str(body[0])
        return ""

    @staticmethod
    def _extract_retry_after(response: httpx.Response) -> float:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return 5.0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 5.0

    async def _sleep_backoff(
        self, attempt: int, response: Optional[httpx.Response]
    ) -> None:
        # Honour Retry-After on the first retry if present.
        delay: float = (
            RETRY_DELAY_S * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5)
        )
        if response is not None and attempt == 0:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    pass
        delay = min(delay, MAX_RETRY_DELAY_S)
        logger.warning("jazzhr.retry", attempt=attempt + 1, delay=delay)
        await asyncio.sleep(delay)

    # ── Public API ─────────────────────────────────────────────────────────

    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        return await self._request(
            "GET", path, params=params, context=context or path
        )

    async def post(
        self,
        path: str,
        form: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        return await self._request(
            "POST", path, form=form, context=context or path
        )
