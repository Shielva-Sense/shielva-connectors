"""Mistral AI connector — orchestration only.

All HTTP calls → client/http_client.py::MistralHTTPClient
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Bearer API key. The key is read from `self.config["api_key"]` and
passed through to every HTTP call. No OAuth, no refresh.
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

from client.http_client import MistralHTTPClient
from exceptions import (
    MistralAuthError,
    MistralError,
    MistralNetworkError,
    MistralNotFoundError,
)
from helpers.normalizer import (
    normalize_file,
    normalize_fine_tuning_job,
    normalize_model,
)
from helpers.utils import (
    build_chat_payload,
    build_embeddings_payload,
    build_fine_tuning_payload,
    with_retry,
)

logger = structlog.get_logger(__name__)

_MISTRAL_BASE = "https://api.mistral.ai/v1"
_DEFAULT_CHAT_MODEL = "mistral-large-latest"
_DEFAULT_EMBED_MODEL = "mistral-embed"


class MistralConnector(BaseConnector):
    """Shielva connector for the Mistral AI REST API."""

    CONNECTOR_TYPE = "mistral"
    CONNECTOR_NAME = "Mistral AI"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "TOKEN_EXPIRED"),
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
        self.base_url: str = self.config.get("base_url", "") or _MISTRAL_BASE
        self.default_chat_model: str = (
            self.config.get("default_chat_model") or _DEFAULT_CHAT_MODEL
        )
        self.default_embed_model: str = (
            self.config.get("default_embed_model") or _DEFAULT_EMBED_MODEL
        )
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = MistralHTTPClient(base_url=self.base_url)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _get_api_key(self) -> str:
        """Return the configured API key — raises MistralAuthError when missing."""
        key = self.config.get("api_key") or self.api_key
        if not key:
            raise MistralAuthError("Mistral API key is not configured")
        return key

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate `api_key`, persist config, probe `GET /models` once.

        Returns:
        - `HEALTHY + CONNECTED` when the API key is accepted by Mistral.
        - `OFFLINE + MISSING_CREDENTIALS` when `api_key` is missing.
        - `DEGRADED + MISSING_CREDENTIALS` when Mistral rejects the key (401/403).
        - `DEGRADED + CONNECTED` when the network probe fails for transient reasons —
          the connector is still installed and `health_check()` will re-probe.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning(
                "mistral.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
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

        try:
            await self.http_client.list_models(api_key)
        except MistralAuthError as exc:
            logger.warning(
                "mistral.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="API key rejected by Mistral",
            )
        except (MistralNetworkError, MistralError) as exc:
            logger.warning(
                "mistral.install.probe_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connector installed but probe failed: {exc}",
            )

        logger.info("mistral.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Connector installed and API key verified",
        )

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returns a synthetic TokenInfo whose `access_token` is the configured
        api_key, for ABI compatibility with BaseConnector.
        """
        api_key = self._get_api_key()
        return TokenInfo(
            access_token=api_key,
            refresh_token=None,
            expires_at=None,
            token_type="Bearer",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Mistral API connectivity by listing models."""
        try:
            api_key = self._get_api_key()
            await with_retry(
                lambda: self.http_client.list_models(api_key),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Mistral API reachable",
            )
        except MistralAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=str(exc),
            )
        except MistralNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except MistralError as exc:
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
        """Catalogue models + uploaded files into the Shielva KB.

        Mistral has no real-time syncable corpus; this hook is implemented as a
        stub so the BaseConnector contract stays whole and observability shows
        what the configured API key can see.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            api_key = self._get_api_key()

            # Models
            try:
                models_resp = await self.http_client.list_models(api_key)
                for raw in (models_resp.get("data") or []) if isinstance(models_resp, dict) else []:
                    documents_found += 1
                    try:
                        doc = normalize_model(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("mistral.sync.model_failed", error=str(exc))
                        documents_failed += 1
            except MistralError as exc:
                logger.warning("mistral.sync.models_skip", error=str(exc))

            # Files
            try:
                files_resp = await self.http_client.list_files(api_key)
                for raw in (files_resp.get("data") or []) if isinstance(files_resp, dict) else []:
                    documents_found += 1
                    try:
                        doc = normalize_file(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("mistral.sync.file_failed", error=str(exc))
                        documents_failed += 1
            except MistralError as exc:
                logger.warning("mistral.sync.files_skip", error=str(exc))

            return SyncResult(
                status=SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL,
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Mistral is stateless — catalogued {documents_synced}/{documents_found} item(s)",
            )
        except Exception as exc:
            logger.error(
                "mistral.sync.failed", error=str(exc), connector_id=self.connector_id
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Inference ──────────────────────────────────────────────────────────

    async def create_chat_completion(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        top_p: float = 1.0,
        stream: bool = False,
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """POST /chat/completions — returns the raw API response dict."""
        api_key = self._get_api_key()
        payload = build_chat_payload(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stream=stream,
            response_format=response_format,
            tools=tools,
        )
        return await with_retry(
            lambda: self.http_client.create_chat_completion(api_key, payload),
            max_retries=3,
        )

    async def create_embeddings(
        self,
        model: str,
        inputs: List[str],
        encoding_format: str = "float",
    ) -> Dict[str, Any]:
        """POST /embeddings — returns embedding vectors for each input."""
        api_key = self._get_api_key()
        payload = build_embeddings_payload(
            model=model, inputs=inputs, encoding_format=encoding_format
        )
        return await with_retry(
            lambda: self.http_client.create_embeddings(api_key, payload),
            max_retries=3,
        )

    # ── Models ─────────────────────────────────────────────────────────────

    async def list_models(self) -> Dict[str, Any]:
        """GET /models — return all models visible to this API key."""
        api_key = self._get_api_key()
        return await with_retry(
            lambda: self.http_client.list_models(api_key),
            max_retries=3,
        )

    async def get_model(self, model_id: str) -> Dict[str, Any]:
        """GET /models/{id} — fetch a single model record."""
        if not model_id:
            raise MistralError("model_id is required")
        api_key = self._get_api_key()
        return await with_retry(
            lambda: self.http_client.get_model(api_key, model_id),
            max_retries=3,
        )

    async def delete_model(self, model_id: str) -> Dict[str, Any]:
        """DELETE /models/{id} — delete a fine-tuned model."""
        if not model_id:
            raise MistralError("model_id is required")
        api_key = self._get_api_key()
        return await with_retry(
            lambda: self.http_client.delete_model(api_key, model_id),
            max_retries=2,
        )

    # ── Files ──────────────────────────────────────────────────────────────

    async def list_files(
        self,
        purpose: Optional[str] = None,
        page: int = 0,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """GET /files — list uploaded files."""
        api_key = self._get_api_key()
        return await with_retry(
            lambda: self.http_client.list_files(
                api_key, purpose=purpose, page=page, page_size=page_size
            ),
            max_retries=3,
        )

    async def upload_file(self, purpose: str, file_path: str) -> Dict[str, Any]:
        """POST /files — multipart upload of a training or batch input file."""
        if not purpose:
            raise MistralError("purpose is required (e.g. 'fine-tune')")
        if not file_path:
            raise MistralError("file_path is required")
        api_key = self._get_api_key()
        return await self.http_client.upload_file(api_key, purpose, file_path)

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        """GET /files/{id} — return file metadata."""
        if not file_id:
            raise MistralError("file_id is required")
        api_key = self._get_api_key()
        return await with_retry(
            lambda: self.http_client.get_file(api_key, file_id),
            max_retries=3,
        )

    async def delete_file(self, file_id: str) -> Dict[str, Any]:
        """DELETE /files/{id} — remove a previously uploaded file."""
        if not file_id:
            raise MistralError("file_id is required")
        api_key = self._get_api_key()
        return await with_retry(
            lambda: self.http_client.delete_file(api_key, file_id),
            max_retries=2,
        )

    # ── Fine-tuning ────────────────────────────────────────────────────────

    async def list_fine_tuning_jobs(
        self,
        page: int = 0,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """GET /fine_tuning/jobs — paginate over fine-tuning jobs."""
        api_key = self._get_api_key()
        return await with_retry(
            lambda: self.http_client.list_fine_tuning_jobs(
                api_key, page=page, page_size=page_size
            ),
            max_retries=3,
        )

    async def create_fine_tuning_job(
        self,
        model: str,
        training_files: List[str],
        hyperparameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /fine_tuning/jobs — kick off a fine-tuning run."""
        if not model:
            raise MistralError("model is required")
        if not training_files:
            raise MistralError("training_files must contain at least one file_id")
        api_key = self._get_api_key()
        payload = build_fine_tuning_payload(
            model=model,
            training_files=training_files,
            hyperparameters=hyperparameters,
        )
        return await with_retry(
            lambda: self.http_client.create_fine_tuning_job(api_key, payload),
            max_retries=2,
        )
