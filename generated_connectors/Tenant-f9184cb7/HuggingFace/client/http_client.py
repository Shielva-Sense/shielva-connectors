"""All HuggingFace API HTTP calls — zero business logic, zero normalization.

The client straddles three different base URLs:

* **Hub API** (``https://huggingface.co/api``) — metadata about models /
  datasets / spaces / organizations / whoami.
* **Inference API** (``https://api-inference.huggingface.co``) — serverless
  model invocation (text-generation, embeddings, classification, summarization,
  translation, image classification, etc.).
* **Inference Endpoints API** (``https://api.endpoints.huggingface.cloud/v2``)
  — managed dedicated model deployments (list / get / create / delete).

All three share the same ``Authorization: Bearer <api_key>`` header.

Retry / cold-start handling
---------------------------
The Inference API returns ``HTTP 503`` with a body like
``{"error": "Model X is currently loading", "estimated_time": 20.0}`` while a
model warms up on the GPU. The client honours that hint and retries
automatically after sleeping ``estimated_time`` seconds. ``429`` (rate limit)
and ``5xx`` are retried with exponential backoff up to ``max_retries``.
"""
import asyncio
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    HuggingFaceAuthError,
    HuggingFaceModelLoadingError,
    HuggingFaceNetworkError,
    HuggingFaceNotFound,
    HuggingFaceRateLimitError,
    HuggingFaceServerError,
)

logger = structlog.get_logger(__name__)

_HUB_BASE = "https://huggingface.co/api"
_INFERENCE_BASE = "https://api-inference.huggingface.co"
_ENDPOINTS_BASE = "https://api.endpoints.huggingface.cloud/v2"

# OCP: tune retry behavior here, nowhere else
_RETRY_BASE_DELAY_S: float = 1.0
_RETRY_BACKOFF: float = 2.0
_RETRY_MAX_DELAY_S: float = 32.0
_DEFAULT_TIMEOUT_S: float = 60.0


class HuggingFaceHTTPClient:
    """Thin async HTTP client for the HuggingFace Hub + Inference + Endpoints REST APIs."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = _HUB_BASE,
        inference_url: str = _INFERENCE_BASE,
        endpoints_url: str = _ENDPOINTS_BASE,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = 3,
    ):
        self._api_key = api_key or ""
        self._hub_base = (base_url or _HUB_BASE).rstrip("/")
        self._inference_base = (inference_url or _INFERENCE_BASE).rstrip("/")
        self._endpoints_base = (endpoints_url or _ENDPOINTS_BASE).rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries

    # ── Internal helpers ──────────────────────────────────────────────────
    def _headers(self, content_type: Optional[str] = "application/json") -> Dict[str, str]:
        h: Dict[str, str] = {}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        if content_type:
            h["Content-Type"] = content_type
        h["Accept"] = "application/json"
        return h

    def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        """Map HTTP error codes to connector exceptions."""
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {}

        if isinstance(body, dict):
            message = body.get("error") or body.get("message") or str(body)
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status == 401:
            raise HuggingFaceAuthError(
                f"401 Unauthorized{ctx}: {message}",
                status_code=401,
                response_body=body_dict,
            )
        if status == 403:
            raise HuggingFaceAuthError(
                f"403 Forbidden{ctx}: {message}",
                status_code=403,
                response_body=body_dict,
            )
        if status == 404:
            raise HuggingFaceNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 429:
            raise HuggingFaceRateLimitError(
                f"429 Rate limit exceeded{ctx}: {message}",
            )
        if status == 503 and isinstance(body, dict) and "loading" in message.lower():
            estimated = float(body.get("estimated_time") or 20.0)
            raise HuggingFaceModelLoadingError(
                f"Model loading{ctx}: {message}",
                estimated_time=estimated,
            )
        raise HuggingFaceServerError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        binary_body: Optional[bytes] = None,
        context: str = "",
    ) -> Any:
        """Perform an HTTP call with cold-load + rate-limit + 5xx retry.

        Returns parsed JSON when the response is JSON; otherwise returns ``bytes``.
        """
        attempt = 0
        last_exc: Optional[Exception] = None

        while attempt <= self._max_retries:
            try:
                if binary_body is not None:
                    headers = self._headers(content_type="application/octet-stream")
                else:
                    headers = self._headers()

                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    resp = await client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body if binary_body is None else None,
                        content=binary_body,
                        headers=headers,
                    )
                self._raise_for_status(resp, context=context)
                if resp.status_code == 204 or not resp.content:
                    return {}
                content_type = resp.headers.get("content-type", "")
                if "application/json" in content_type:
                    return resp.json()
                try:
                    return resp.json()
                except Exception:
                    return resp.content

            except HuggingFaceModelLoadingError as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    break
                delay = min(exc.estimated_time, _RETRY_MAX_DELAY_S)
                logger.warning(
                    "huggingface.model_loading.retry",
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                )
                await asyncio.sleep(delay)
            except HuggingFaceRateLimitError as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    break
                delay = min(
                    _RETRY_BASE_DELAY_S * (_RETRY_BACKOFF ** attempt)
                    + random.uniform(0, 0.5),
                    _RETRY_MAX_DELAY_S,
                )
                logger.warning(
                    "huggingface.rate_limit.retry",
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                )
                await asyncio.sleep(delay)
            except HuggingFaceServerError as exc:
                # Retry server-side 5xx only; 4xx surface immediately.
                if 500 <= exc.status_code < 600 and attempt < self._max_retries:
                    last_exc = exc
                    delay = min(
                        _RETRY_BASE_DELAY_S * (_RETRY_BACKOFF ** attempt)
                        + random.uniform(0, 0.5),
                        _RETRY_MAX_DELAY_S,
                    )
                    logger.warning(
                        "huggingface.server_error.retry",
                        attempt=attempt + 1,
                        delay=delay,
                        status=exc.status_code,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
            except httpx.HTTPError as exc:
                last_exc = HuggingFaceNetworkError(str(exc))
                if attempt == self._max_retries:
                    break
                delay = min(
                    _RETRY_BASE_DELAY_S * (_RETRY_BACKOFF ** attempt)
                    + random.uniform(0, 0.5),
                    _RETRY_MAX_DELAY_S,
                )
                logger.warning(
                    "huggingface.network_error.retry",
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(exc),
                    context=context,
                )
                await asyncio.sleep(delay)

            attempt += 1

        assert last_exc is not None  # unreachable: loop always sets last_exc on break
        raise last_exc

    # ── Hub API ───────────────────────────────────────────────────────────
    async def whoami(self) -> Dict[str, Any]:
        """GET /whoami-v2 — returns the authenticated user's identity."""
        url = f"{self._hub_base}/whoami-v2"
        return await self._request("GET", url, context="whoami")

    async def list_models(
        self,
        search: Optional[str] = None,
        author: Optional[str] = None,
        filter: Optional[str] = None,
        limit: int = 20,
        sort: str = "downloads",
    ) -> List[Dict[str, Any]]:
        """GET /models — list models matching the given filters."""
        url = f"{self._hub_base}/models"
        params: Dict[str, Any] = {"limit": limit, "sort": sort}
        if search:
            params["search"] = search
        if author:
            params["author"] = author
        if filter:
            params["filter"] = filter
        return await self._request("GET", url, params=params, context="list_models")

    async def get_model(self, model_id: str) -> Dict[str, Any]:
        """GET /models/{id} — fetch a single model's metadata."""
        url = f"{self._hub_base}/models/{model_id}"
        return await self._request("GET", url, context=f"get_model({model_id})")

    async def list_datasets(
        self,
        search: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """GET /datasets — list datasets matching the given search."""
        url = f"{self._hub_base}/datasets"
        params: Dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        return await self._request("GET", url, params=params, context="list_datasets")

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """GET /datasets/{id}."""
        url = f"{self._hub_base}/datasets/{dataset_id}"
        return await self._request("GET", url, context=f"get_dataset({dataset_id})")

    async def list_spaces(
        self,
        search: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """GET /spaces — list Spaces matching the given search."""
        url = f"{self._hub_base}/spaces"
        params: Dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        return await self._request("GET", url, params=params, context="list_spaces")

    async def list_organization_repos(self, organization: str) -> Dict[str, Any]:
        """GET /organizations/{name} — list public repos for an organization."""
        url = f"{self._hub_base}/organizations/{organization}"
        return await self._request(
            "GET", url, context=f"list_organization_repos({organization})",
        )

    # ── Inference API ─────────────────────────────────────────────────────
    async def inference_json(
        self,
        model: str,
        payload: Dict[str, Any],
        context: str = "inference",
    ) -> Any:
        """POST {inference_base}/models/{model} with JSON body — for text-only inference."""
        url = f"{self._inference_base}/models/{model}"
        return await self._request(
            "POST", url, json_body=payload, context=f"{context}({model})",
        )

    async def inference_binary(
        self,
        model: str,
        binary_body: bytes,
        context: str = "inference_binary",
    ) -> Any:
        """POST {inference_base}/models/{model} with octet-stream body — image / audio inputs."""
        url = f"{self._inference_base}/models/{model}"
        return await self._request(
            "POST",
            url,
            binary_body=binary_body,
            context=f"{context}({model})",
        )

    # ── Inference Endpoints API (managed deployments) ─────────────────────
    async def list_endpoints(self) -> Dict[str, Any]:
        """GET {endpoints}/endpoint — list managed Inference Endpoints."""
        url = f"{self._endpoints_base}/endpoint"
        return await self._request("GET", url, context="list_endpoints")

    async def get_endpoint(self, endpoint_name: str) -> Dict[str, Any]:
        """GET {endpoints}/endpoint/{name}."""
        url = f"{self._endpoints_base}/endpoint/{endpoint_name}"
        return await self._request(
            "GET", url, context=f"get_endpoint({endpoint_name})",
        )

    async def create_endpoint(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST {endpoints}/endpoint — create a managed deployment."""
        url = f"{self._endpoints_base}/endpoint"
        return await self._request(
            "POST", url, json_body=payload, context="create_endpoint",
        )

    async def delete_endpoint(self, endpoint_name: str) -> Dict[str, Any]:
        """DELETE {endpoints}/endpoint/{name}."""
        url = f"{self._endpoints_base}/endpoint/{endpoint_name}"
        return await self._request(
            "DELETE", url, context=f"delete_endpoint({endpoint_name})",
        )

    async def run_inference_endpoint(
        self,
        endpoint_url: str,
        payload: Dict[str, Any],
    ) -> Any:
        """POST {endpoint_url} — invoke a managed Inference Endpoint."""
        return await self._request(
            "POST", endpoint_url, json_body=payload, context="run_inference_endpoint",
        )
