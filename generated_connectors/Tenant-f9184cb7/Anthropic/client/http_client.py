"""All Anthropic API HTTP calls — zero business logic, zero normalization.

httpx async client. The Anthropic REST API expects:

    x-api-key:         <api_key>            (raw key — NO 'Bearer' prefix)
    anthropic-version: 2023-06-01            (date-pinned API version, mandatory)
    content-type:      application/json
    anthropic-beta:    files-api-2025-04-14  (only on /files calls)

Retry on 429 / 5xx / 529 with exponential backoff. ``Retry-After`` header
is honoured when present (Anthropic returns it on 429).
"""
import asyncio
import random
import time
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    AnthropicAuthError,
    AnthropicBadRequestError,
    AnthropicError,
    AnthropicNetworkError,
    AnthropicNotFoundError,
    AnthropicRateLimitError,
    AnthropicServerError,
)

logger = structlog.get_logger(__name__)

_ANTHROPIC_BASE = "https://api.anthropic.com/v1"
_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_TIMEOUT = 60.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds
_BACKOFF_MAX = 30.0
_FILES_BETA_HEADER = "files-api-2025-04-14"


class AnthropicHTTPClient:
    """Thin async HTTP client for the Anthropic REST API.

    All methods are awaitable and return raw response dicts. Auth, headers,
    rate-limit pacing, and retry are owned here — the connector layer only
    orchestrates business calls.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = _ANTHROPIC_BASE,
        anthropic_version: str = _DEFAULT_ANTHROPIC_VERSION,
        rate_limit_per_min: int = 50,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._api_key = api_key or ""
        self._base_url = (base_url or _ANTHROPIC_BASE).rstrip("/")
        self._anthropic_version = anthropic_version or _DEFAULT_ANTHROPIC_VERSION
        self._rate_limit_per_min = max(1, int(rate_limit_per_min or 50))
        self._timeout = timeout
        self._max_retries = max_retries

        # Token-bucket pacing — track recent request timestamps and sleep
        # when the rolling 60s window is saturated.
        self._recent_ts: List[float] = []
        self._lock = asyncio.Lock()

    # ── Header construction ────────────────────────────────────────────────

    def _headers(self, *, beta: Optional[str] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "content-type": "application/json",
            "accept": "application/json",
        }
        if beta:
            headers["anthropic-beta"] = beta
        return headers

    # ── Rate-limit pacing ─────────────────────────────────────────────────

    async def _acquire_slot(self) -> None:
        async with self._lock:
            now = time.monotonic()
            window_start = now - 60.0
            self._recent_ts = [ts for ts in self._recent_ts if ts > window_start]
            if len(self._recent_ts) >= self._rate_limit_per_min:
                oldest = self._recent_ts[0]
                sleep_for = 60.0 - (now - oldest)
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
            self._recent_ts.append(time.monotonic())

    # ── Error mapping ─────────────────────────────────────────────────────

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}

        error_obj = body.get("error", {}) if isinstance(body, dict) else {}
        if isinstance(error_obj, dict):
            message = error_obj.get("message") or str(body)
        else:
            message = str(error_obj) or str(body)
        if not isinstance(message, str):
            message = str(message)

        ctx = f" [{context}]" if context else ""
        safe_body = body if isinstance(body, dict) else {"raw": body}

        if status == 401:
            raise AnthropicAuthError(
                f"401 Unauthorized{ctx}: {message}",
                status_code=401,
                response_body=safe_body,
            )
        if status == 403:
            raise AnthropicAuthError(
                f"403 Forbidden{ctx}: {message}",
                status_code=403,
                response_body=safe_body,
            )
        if status == 404:
            raise AnthropicNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=safe_body,
            )
        if status == 400 or status == 413:
            raise AnthropicBadRequestError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=safe_body,
            )
        if status == 429:
            retry_after_s = self._parse_retry_after(response) or 5.0
            err = AnthropicRateLimitError(
                f"429 Rate Limited{ctx}: {message}",
                retry_after_s=retry_after_s,
            )
            err.response_body = safe_body
            raise err
        if status >= 500:
            raise AnthropicServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=safe_body,
            )
        raise AnthropicError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=safe_body,
        )

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> Optional[float]:
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            return min(float(value), _BACKOFF_MAX)
        except (TypeError, ValueError):
            return None

    # ── Core request loop ─────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        beta: Optional[str] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers(beta=beta)

        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            await self._acquire_slot()
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
                    if attempt < self._max_retries - 1:
                        delay = (
                            self._parse_retry_after(response)
                            or _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.25)
                        )
                        delay = min(delay, _BACKOFF_MAX)
                        logger.warning(
                            "anthropic.http.retry",
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
                except Exception:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.25)
                    delay = min(delay, _BACKOFF_MAX)
                    logger.warning(
                        "anthropic.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise AnthropicNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise AnthropicNetworkError(str(last_exc)) from last_exc
        raise AnthropicNetworkError(
            f"Exhausted retries{': ' + context if context else ''}",
        )

    # ── Messages API ───────────────────────────────────────────────────────

    async def create_message(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        max_tokens: int = 1024,
        system: Optional[str] = None,
        temperature: float = 1.0,
        stream: bool = False,
    ) -> Dict[str, Any]:
        """POST /messages — create a Messages completion."""
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if system is not None:
            body["system"] = system
        return await self._request(
            "POST",
            "/messages",
            json_body=body,
            context="create_message",
        )

    async def count_tokens(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /messages/count_tokens — input-token cost estimate."""
        body: Dict[str, Any] = {"model": model, "messages": messages}
        if system is not None:
            body["system"] = system
        return await self._request(
            "POST",
            "/messages/count_tokens",
            json_body=body,
            context="count_tokens",
        )

    # ── Models API ─────────────────────────────────────────────────────────

    async def list_models(
        self,
        limit: int = 20,
        before_id: Optional[str] = None,
        after_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /models — list models available to this api key."""
        params: Dict[str, Any] = {"limit": limit}
        if before_id:
            params["before_id"] = before_id
        if after_id:
            params["after_id"] = after_id
        return await self._request(
            "GET",
            "/models",
            params=params,
            context="list_models",
        )

    async def get_model(self, model_id: str) -> Dict[str, Any]:
        """GET /models/{id} — fetch a single model."""
        return await self._request(
            "GET",
            f"/models/{model_id}",
            context=f"get_model({model_id})",
        )

    # ── Message Batches API ───────────────────────────────────────────────

    async def create_batch(self, requests: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /messages/batches — submit a Message Batch."""
        return await self._request(
            "POST",
            "/messages/batches",
            json_body={"requests": requests},
            context="create_batch",
        )

    async def get_batch(self, batch_id: str) -> Dict[str, Any]:
        """GET /messages/batches/{id} — batch status."""
        return await self._request(
            "GET",
            f"/messages/batches/{batch_id}",
            context=f"get_batch({batch_id})",
        )

    async def list_batches(
        self,
        limit: int = 20,
        before_id: Optional[str] = None,
        after_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /messages/batches — list all batches."""
        params: Dict[str, Any] = {"limit": limit}
        if before_id:
            params["before_id"] = before_id
        if after_id:
            params["after_id"] = after_id
        return await self._request(
            "GET",
            "/messages/batches",
            params=params,
            context="list_batches",
        )

    async def cancel_batch(self, batch_id: str) -> Dict[str, Any]:
        """POST /messages/batches/{id}/cancel — cancel an in-flight batch."""
        return await self._request(
            "POST",
            f"/messages/batches/{batch_id}/cancel",
            context=f"cancel_batch({batch_id})",
        )

    async def get_batch_results(self, batch_id: str) -> Dict[str, Any]:
        """GET /messages/batches/{id}/results — fetch results of a completed batch."""
        return await self._request(
            "GET",
            f"/messages/batches/{batch_id}/results",
            context=f"get_batch_results({batch_id})",
        )

    # ── Files API (beta) ───────────────────────────────────────────────────

    async def list_files(
        self,
        limit: int = 20,
        before_id: Optional[str] = None,
        after_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /files — list uploaded files (beta)."""
        params: Dict[str, Any] = {"limit": limit}
        if before_id:
            params["before_id"] = before_id
        if after_id:
            params["after_id"] = after_id
        return await self._request(
            "GET",
            "/files",
            params=params,
            beta=_FILES_BETA_HEADER,
            context="list_files",
        )

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        """GET /files/{id} — file metadata (beta)."""
        return await self._request(
            "GET",
            f"/files/{file_id}",
            beta=_FILES_BETA_HEADER,
            context=f"get_file({file_id})",
        )

    async def delete_file(self, file_id: str) -> Dict[str, Any]:
        """DELETE /files/{id} — delete a file (beta)."""
        return await self._request(
            "DELETE",
            f"/files/{file_id}",
            beta=_FILES_BETA_HEADER,
            context=f"delete_file({file_id})",
        )
