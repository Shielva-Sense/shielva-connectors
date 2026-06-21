"""All Cohere API HTTP calls — zero business logic, zero normalization.

httpx async client. The Cohere REST API expects:
  Authorization: Bearer <api_key>
  Content-Type:  application/json
  X-Client-Name: shielva-connector

The v2 surface (chat / embed / rerank) lives under `/v2/*`. The classic v1
surface (classify / tokenize / detokenize / models / datasets / connectors /
finetuning) lives under `/v1/*`. We keep the base_url at `https://api.cohere.com`
and prefix per-endpoint, so a single client handles both API versions without
forcing the operator to pick one.

Retry on 429/5xx with exponential backoff.
"""
import asyncio
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    CohereAuthError,
    CohereBadRequestError,
    CohereError,
    CohereNetworkError,
    CohereNotFoundError,
    CohereRateLimitError,
    CohereServerError,
)

logger = structlog.get_logger(__name__)

_COHERE_BASE = "https://api.cohere.com"
_DEFAULT_TIMEOUT = 60.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds
_RETRY_MAX_DELAY_S = 16.0
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class CohereHTTPClient:
    """Thin async HTTP client for the Cohere REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = _COHERE_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ):
        self._api_key = api_key or ""
        # Strip a trailing `/v2` or `/v1` so callers can safely paste either
        # base URL — every endpoint method below specifies the version prefix.
        cleaned = (base_url or _COHERE_BASE).rstrip("/")
        for suffix in ("/v2", "/v1"):
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)]
                break
        self._base_url = cleaned
        self._timeout = timeout
        self._max_retries = max_retries

    # ── Auth + error mapping ────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Client-Name": "shielva-connector",
        }

    def _raise_for_status(
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
            message = (
                body.get("message")
                or body.get("error")
                or body.get("detail")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        body_dict = body if isinstance(body, dict) else {"raw": body}
        ctx = f": {context}" if context else ""

        if status in (401, 403):
            raise CohereAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise CohereNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status in (400, 422):
            raise CohereBadRequestError(
                f"{status} Bad Request{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 429:
            raise CohereRateLimitError(
                f"429 Rate limited{ctx}: {message}",
                status_code=429,
                response_body=body_dict,
            )
        if 500 <= status < 600:
            raise CohereServerError(
                f"{status} Server error{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise CohereError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── Core request driver with retry ──────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers()

        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
                if (
                    response.status_code in _RETRY_STATUSES
                    and attempt < self._max_retries
                ):
                    delay = self._delay(attempt)
                    logger.warning(
                        "cohere.http.retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    delay = self._delay(attempt)
                    logger.warning(
                        "cohere.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise CohereNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise CohereNetworkError(str(last_exc)) from last_exc
        raise CohereNetworkError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    @staticmethod
    def _delay(attempt: int) -> float:
        return min(
            _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.25),
            _RETRY_MAX_DELAY_S,
        )

    # ── v2 inference ───────────────────────────────────────────────────────

    async def chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        stream: bool = False,
        p: Optional[float] = None,
        k: Optional[int] = None,
        stop_sequences: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /v2/chat — v2 chat completion."""
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if p is not None:
            body["p"] = p
        if k is not None:
            body["k"] = k
        if stop_sequences:
            body["stop_sequences"] = stop_sequences
        return await self._request("POST", "/v2/chat", json_body=body, context="chat")

    async def embed(
        self,
        model: str,
        texts: List[str],
        *,
        input_type: str = "search_document",
        embedding_types: Optional[List[str]] = None,
        truncate: str = "END",
    ) -> Dict[str, Any]:
        """POST /v2/embed — v2 embeddings."""
        body: Dict[str, Any] = {
            "model": model,
            "texts": texts,
            "input_type": input_type,
            "embedding_types": embedding_types or ["float"],
            "truncate": truncate,
        }
        return await self._request("POST", "/v2/embed", json_body=body, context="embed")

    async def rerank(
        self,
        model: str,
        query: str,
        documents: List[Any],
        *,
        top_n: int = 10,
        return_documents: bool = False,
    ) -> Dict[str, Any]:
        """POST /v2/rerank — v2 reranker."""
        body: Dict[str, Any] = {
            "model": model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
            "return_documents": return_documents,
        }
        return await self._request("POST", "/v2/rerank", json_body=body, context="rerank")

    # ── v1 inference utilities ─────────────────────────────────────────────

    async def classify(
        self,
        model: str,
        inputs: List[str],
        examples: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """POST /v1/classify — few-shot text classification."""
        body: Dict[str, Any] = {
            "model": model,
            "inputs": inputs,
            "examples": examples,
        }
        return await self._request(
            "POST", "/v1/classify", json_body=body, context="classify"
        )

    async def tokenize(self, model: str, text: str) -> Dict[str, Any]:
        """POST /v1/tokenize — model-specific tokenisation."""
        body = {"model": model, "text": text}
        return await self._request(
            "POST", "/v1/tokenize", json_body=body, context="tokenize"
        )

    async def detokenize(self, model: str, tokens: List[int]) -> Dict[str, Any]:
        """POST /v1/detokenize — reverse tokenisation."""
        body = {"model": model, "tokens": tokens}
        return await self._request(
            "POST", "/v1/detokenize", json_body=body, context="detokenize"
        )

    # ── v1 management ──────────────────────────────────────────────────────

    async def list_models(
        self,
        page_size: int = 20,
        page_token: Optional[str] = None,
        endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/models — list available Cohere models."""
        params: Dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        if endpoint:
            params["endpoint"] = endpoint
        return await self._request(
            "GET", "/v1/models", params=params, context="list_models"
        )

    async def get_model(self, model_id: str) -> Dict[str, Any]:
        """GET /v1/models/{model_id}."""
        return await self._request(
            "GET",
            f"/v1/models/{model_id}",
            context=f"get_model({model_id})",
        )

    async def list_datasets(
        self,
        dataset_type: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v1/datasets — list fine-tune datasets."""
        params: Dict[str, Any] = {"limit": limit}
        if dataset_type:
            params["datasetType"] = dataset_type
        return await self._request(
            "GET", "/v1/datasets", params=params, context="list_datasets"
        )

    async def create_dataset(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /v1/datasets — register a new fine-tune dataset.

        Real Cohere dataset upload is multipart with a CSV/JSONL file. The
        connector accepts a pre-built JSON envelope so the gateway can shape
        the file upload separately and call this for metadata-only creates.
        """
        return await self._request(
            "POST",
            "/v1/datasets",
            json_body=payload,
            context="create_dataset",
        )

    async def list_connectors(
        self,
        page_size: int = 20,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/connectors — list Cohere RAG connectors (not Shielva connectors)."""
        params: Dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return await self._request(
            "GET", "/v1/connectors", params=params, context="list_connectors"
        )

    async def list_finetuned_models(
        self,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """GET /v1/finetuning/finetuned-models — list fine-tune jobs."""
        params = {"page_size": page_size}
        return await self._request(
            "GET",
            "/v1/finetuning/finetuned-models",
            params=params,
            context="list_finetuned_models",
        )
