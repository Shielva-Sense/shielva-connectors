"""All Mistral AI HTTP calls — httpx async, Bearer auth, retry on 429/5xx.

Zero business logic, zero normalization. The class owns:
- header construction (Bearer for JSON, multipart for /files)
- exponential backoff + jitter (honouring Retry-After) on 429 and 5xx
- typed-exception mapping on error codes

The connector layer only orchestrates business calls.
"""
import asyncio
import random
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    MistralAuthError,
    MistralBadRequestError,
    MistralError,
    MistralNetworkError,
    MistralNotFoundError,
    MistralRateLimitError,
    MistralServerError,
)

logger = structlog.get_logger(__name__)

_MISTRAL_BASE = "https://api.mistral.ai/v1"
_DEFAULT_TIMEOUT = 60.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class MistralHTTPClient:
    """Thin async HTTP client for the Mistral REST API.

    All methods accept an *api_key* string and return raw JSON dicts. The
    client never decides business logic — it only speaks HTTP.
    """

    def __init__(
        self,
        base_url: str = _MISTRAL_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ── Headers ────────────────────────────────────────────────────────────

    def _auth_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _multipart_headers(self, api_key: str) -> Dict[str, str]:
        # httpx sets Content-Type automatically when files= is used.
        return {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    # ── Error mapping ──────────────────────────────────────────────────────

    def _raise_for_status(
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

        message = ""
        if isinstance(body, dict):
            err = body.get("error") or body.get("message") or body
            if isinstance(err, dict):
                message = err.get("message", "") or err.get("type", "") or str(err)
            else:
                message = str(err)
        ctx = f": {context}" if context else ""

        if status == 400:
            raise MistralBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body,
            )
        if status in (401, 403):
            raise MistralAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        if status == 404:
            raise MistralNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 5.0
            except (TypeError, ValueError):
                wait = 5.0
            raise MistralRateLimitError(
                f"429 Rate limit exceeded{ctx}: {message}",
                status_code=429,
                response_body=body,
                retry_after_s=wait,
            )
        if 500 <= status < 600:
            raise MistralServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        raise MistralError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body,
        )

    # ── Retry loop ─────────────────────────────────────────────────────────

    async def _sleep_backoff(
        self,
        attempt: int,
        response: Optional[httpx.Response] = None,
    ) -> None:
        """Sleep with exponential backoff + jitter, honouring Retry-After."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    await asyncio.sleep(float(retry_after))
                    return
                except (TypeError, ValueError):
                    pass
        delay = _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.25)
        await asyncio.sleep(delay)

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> httpx.Response:
        """Issue an HTTP request, retrying 429 + 5xx up to _MAX_RETRIES times."""
        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method,
                        url,
                        headers=headers,
                        json=json_payload,
                        params=params,
                        files=files,
                        data=data,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt + 1 >= _MAX_RETRIES:
                    raise MistralNetworkError(
                        f"Network failure after {_MAX_RETRIES} attempts ({context}): {exc}"
                    ) from exc
                logger.warning(
                    "mistral.http.transport_retry",
                    attempt=attempt + 1,
                    context=context,
                    error=str(exc),
                )
                await self._sleep_backoff(attempt)
                continue

            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt + 1 < _MAX_RETRIES:
                    logger.warning(
                        "mistral.http.retry",
                        status=resp.status_code,
                        attempt=attempt + 1,
                        context=context,
                    )
                    await self._sleep_backoff(attempt, response=resp)
                    continue
            return resp

        if last_exc:
            raise MistralNetworkError(str(last_exc))
        raise MistralError("Unknown HTTP failure")

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        api_key: str,
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        resp = await self._request_with_retry(
            method,
            url,
            headers=self._auth_headers(api_key),
            json_payload=json_payload,
            params=params,
            context=context,
        )
        self._raise_for_status(resp, context=context)
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    # ── Models ─────────────────────────────────────────────────────────────

    async def list_models(self, api_key: str) -> Dict[str, Any]:
        """GET /models — list available models."""
        return await self._json_request(
            "GET", "/models", api_key=api_key, context="list_models"
        )

    async def get_model(self, api_key: str, model_id: str) -> Dict[str, Any]:
        """GET /models/{id}."""
        return await self._json_request(
            "GET",
            f"/models/{model_id}",
            api_key=api_key,
            context=f"get_model({model_id})",
        )

    async def delete_model(self, api_key: str, model_id: str) -> Dict[str, Any]:
        """DELETE /models/{id} — delete a fine-tuned model."""
        return await self._json_request(
            "DELETE",
            f"/models/{model_id}",
            api_key=api_key,
            context=f"delete_model({model_id})",
        )

    # ── Chat / Embeddings ──────────────────────────────────────────────────

    async def create_chat_completion(
        self, api_key: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /chat/completions."""
        return await self._json_request(
            "POST",
            "/chat/completions",
            api_key=api_key,
            json_payload=payload,
            context="create_chat_completion",
        )

    async def create_embeddings(
        self, api_key: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /embeddings."""
        return await self._json_request(
            "POST",
            "/embeddings",
            api_key=api_key,
            json_payload=payload,
            context="create_embeddings",
        )

    # ── Files ──────────────────────────────────────────────────────────────

    async def list_files(
        self,
        api_key: str,
        purpose: Optional[str] = None,
        page: int = 0,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """GET /files."""
        params: Dict[str, Any] = {"page": page, "page_size": page_size}
        if purpose:
            params["purpose"] = purpose
        return await self._json_request(
            "GET", "/files", api_key=api_key, params=params, context="list_files"
        )

    async def upload_file(
        self,
        api_key: str,
        purpose: str,
        file_path: str,
    ) -> Dict[str, Any]:
        """POST /files — multipart upload."""
        url = f"{self._base_url}/files"
        path = Path(file_path)
        if not path.exists():
            raise MistralError(f"File not found: {file_path}")
        with path.open("rb") as fh:
            file_bytes = fh.read()
        files = {"file": (path.name, file_bytes, "application/octet-stream")}
        data = {"purpose": purpose}
        resp = await self._request_with_retry(
            "POST",
            url,
            headers=self._multipart_headers(api_key),
            files=files,
            data=data,
            context="upload_file",
        )
        self._raise_for_status(resp, context="upload_file")
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    async def get_file(self, api_key: str, file_id: str) -> Dict[str, Any]:
        """GET /files/{id}."""
        return await self._json_request(
            "GET",
            f"/files/{file_id}",
            api_key=api_key,
            context=f"get_file({file_id})",
        )

    async def delete_file(self, api_key: str, file_id: str) -> Dict[str, Any]:
        """DELETE /files/{id}."""
        return await self._json_request(
            "DELETE",
            f"/files/{file_id}",
            api_key=api_key,
            context=f"delete_file({file_id})",
        )

    # ── Fine-tuning ────────────────────────────────────────────────────────

    async def create_fine_tuning_job(
        self, api_key: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /fine_tuning/jobs."""
        return await self._json_request(
            "POST",
            "/fine_tuning/jobs",
            api_key=api_key,
            json_payload=payload,
            context="create_fine_tuning_job",
        )

    async def list_fine_tuning_jobs(
        self,
        api_key: str,
        page: int = 0,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """GET /fine_tuning/jobs."""
        params = {"page": page, "page_size": page_size}
        return await self._json_request(
            "GET",
            "/fine_tuning/jobs",
            api_key=api_key,
            params=params,
            context="list_fine_tuning_jobs",
        )
