"""Cohere connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Bearer API key. Cohere has no OAuth dance; the key is sent in the
Authorization header on every request:

    Authorization: Bearer <api_key>
    Content-Type:  application/json
    X-Client-Name: shielva-connector
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import CohereHTTPClient
from exceptions import (
    CohereAuthError,
    CohereError,
    CohereNetworkError,
    CohereNotFound,
)
from helpers.utils import mask_api_key, with_retry

logger = structlog.get_logger(__name__)

_COHERE_BASE = "https://api.cohere.com"
_DEFAULT_CHAT_MODEL = "command-r-plus"
_DEFAULT_EMBED_MODEL = "embed-v4.0"


class CohereConnector(BaseConnector):
    """Shielva connector for the Cohere LLM API (chat, embed, rerank, classify, fine-tuning)."""

    CONNECTOR_TYPE = "cohere"
    CONNECTOR_NAME = "Cohere"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "INVALID_CREDENTIALS"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_key: str = self.config.get("api_key", "")
        self.base_url: str = self.config.get("base_url", "") or _COHERE_BASE
        self.default_chat_model: str = (
            self.config.get("default_chat_model", "") or _DEFAULT_CHAT_MODEL
        )
        self.default_embed_model: str = (
            self.config.get("default_embed_model", "") or _DEFAULT_EMBED_MODEL
        )
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        self.http_client = CohereHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Cohere API-key install requires only `api_key`. We verify the key by
        listing one model so a typo is caught immediately rather than the next
        time the gateway proxies a request.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning(
                "cohere.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        # Rebuild the client in case __init__ ran before save_config landed.
        self.http_client = CohereHTTPClient(
            api_key=api_key,
            base_url=self.base_url,
        )

        try:
            await self.http_client.list_models(page_size=1)
        except CohereAuthError:
            logger.warning(
                "cohere.install.invalid_key",
                connector_id=self.connector_id,
                api_key=mask_api_key(api_key),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="Cohere API rejected the provided api_key",
            )
        except CohereError as exc:
            logger.warning(
                "cohere.install.api_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=f"Cohere reachability check failed: {exc}",
            )

        await self.save_config(
            {
                "api_key": api_key,
                "base_url": self.base_url,
                "default_chat_model": self.default_chat_model,
                "default_embed_model": self.default_embed_model,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info(
            "cohere.install.ok",
            connector_id=self.connector_id,
            api_key=mask_api_key(api_key),
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Cohere connector installed and reachable",
        )

    async def authorize(
        self,
        auth_code: str = "",
        state: str = "",
    ) -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Cohere API connectivity by listing one model."""
        try:
            await with_retry(
                lambda: self.http_client.list_models(page_size=1),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Cohere API reachable",
            )
        except CohereAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Cohere auth failed: {exc}",
            )
        except CohereNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Cohere network error: {exc}",
            )
        except CohereError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Cohere is an inference API — no documents to sync.

        Returns `SUCCESS` immediately with 0 documents. The connector exposes
        `list_models()`, `list_datasets()`, `list_finetuned_models()` for the
        rare case a caller wants to land the model catalogue or fine-tune
        dataset list in the KB; those are explicit calls, not sync targets.
        """
        return SyncResult(
            status=SyncStatus.SUCCESS,
            documents_found=0,
            documents_synced=0,
            documents_failed=0,
            message="Cohere is an inference API — no documents to sync",
        )

    # ── Public API methods (per provider spec) ─────────────────────────────

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
        """POST /v2/chat — run a v2 chat completion."""
        return await with_retry(
            lambda: self.http_client.chat(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
                p=p,
                k=k,
                stop_sequences=stop_sequences,
            ),
            max_retries=3,
        )

    async def embed(
        self,
        model: str,
        texts: List[str],
        *,
        input_type: str = "search_document",
        embedding_types: Optional[List[str]] = None,
        truncate: str = "END",
    ) -> Dict[str, Any]:
        """POST /v2/embed — return vector embeddings for *texts*."""
        return await with_retry(
            lambda: self.http_client.embed(
                model=model,
                texts=texts,
                input_type=input_type,
                embedding_types=embedding_types,
                truncate=truncate,
            ),
            max_retries=3,
        )

    async def rerank(
        self,
        model: str,
        query: str,
        documents: List[Any],
        *,
        top_n: int = 10,
        return_documents: bool = False,
    ) -> Dict[str, Any]:
        """POST /v2/rerank — rerank *documents* by relevance to *query*."""
        return await with_retry(
            lambda: self.http_client.rerank(
                model=model,
                query=query,
                documents=documents,
                top_n=top_n,
                return_documents=return_documents,
            ),
            max_retries=3,
        )

    async def classify(
        self,
        model: str,
        inputs: List[str],
        examples: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """POST /v1/classify — classify *inputs* using few-shot *examples*."""
        return await self.http_client.classify(
            model=model,
            inputs=inputs,
            examples=examples,
        )

    async def tokenize(self, model: str, text: str) -> Dict[str, Any]:
        """POST /v1/tokenize — return Cohere tokens for *text*."""
        return await self.http_client.tokenize(model=model, text=text)

    async def detokenize(self, model: str, tokens: List[int]) -> Dict[str, Any]:
        """POST /v1/detokenize — convert *tokens* back to text."""
        return await self.http_client.detokenize(model=model, tokens=tokens)

    async def list_models(
        self,
        page_size: int = 20,
        page_token: Optional[str] = None,
        endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/models — list available Cohere models."""
        return await with_retry(
            lambda: self.http_client.list_models(
                page_size=page_size,
                page_token=page_token,
                endpoint=endpoint,
            ),
            max_retries=3,
        )

    async def get_model(self, model_id: str) -> Dict[str, Any]:
        """GET /v1/models/{model_id} — fetch a single model's metadata."""
        return await with_retry(
            lambda: self.http_client.get_model(model_id=model_id),
            max_retries=3,
        )

    async def list_datasets(
        self,
        dataset_type: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v1/datasets — list uploaded fine-tuning datasets."""
        return await with_retry(
            lambda: self.http_client.list_datasets(
                dataset_type=dataset_type,
                limit=limit,
            ),
            max_retries=3,
        )

    async def create_dataset(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v1/datasets — register a new fine-tune dataset envelope."""
        return await self.http_client.create_dataset(payload=payload)

    async def list_connectors(
        self,
        page_size: int = 20,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/connectors — list Cohere RAG connectors (not Shielva ones)."""
        return await with_retry(
            lambda: self.http_client.list_connectors(
                page_size=page_size,
                page_token=page_token,
            ),
            max_retries=3,
        )

    async def list_finetuned_models(self, page_size: int = 20) -> Dict[str, Any]:
        """GET /v1/finetuning/finetuned-models — list fine-tuned model jobs."""
        return await with_retry(
            lambda: self.http_client.list_finetuned_models(page_size=page_size),
            max_retries=3,
        )
