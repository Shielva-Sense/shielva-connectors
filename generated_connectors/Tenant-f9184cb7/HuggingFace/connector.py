"""HuggingFace connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: API token (HuggingFace user access token). Sent as
``Authorization: Bearer <api_key>`` on every request to every surface
(Hub, Inference, Inference Endpoints).
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

from client.http_client import HuggingFaceHTTPClient
from exceptions import (
    HuggingFaceAuthError,
    HuggingFaceError,
    HuggingFaceNetworkError,
    HuggingFaceNotFound,
    HuggingFaceServerError,
)
from helpers.normalizer import normalize_model
from helpers.utils import sanitize_model_id, with_retry

logger = structlog.get_logger(__name__)

_HUB_BASE = "https://huggingface.co/api"
_INFERENCE_BASE = "https://api-inference.huggingface.co"
_ENDPOINTS_BASE = "https://api.endpoints.huggingface.cloud/v2"


class HuggingFaceConnector(BaseConnector):
    """Shielva connector for the HuggingFace Hub + Inference + Inference Endpoints REST APIs."""

    CONNECTOR_TYPE = "huggingface"
    CONNECTOR_NAME = "HuggingFace"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_key",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
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
        # Canonical key is ``api_key``; ``api_token`` is accepted for
        # back-compat with the previous metadata shape.
        self.api_key: str = (
            self.config.get("api_key")
            or self.config.get("api_token")
            or ""
        )
        self.base_url: str = self.config.get("base_url", "") or _HUB_BASE
        self.inference_url: str = (
            self.config.get("inference_url")
            or self.config.get("inference_base_url")
            or _INFERENCE_BASE
        )
        self.endpoints_url: str = (
            self.config.get("endpoints_url") or _ENDPOINTS_BASE
        )
        self.default_model: str = self.config.get("default_model", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = HuggingFaceHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
            inference_url=self.inference_url,
            endpoints_url=self.endpoints_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        HuggingFace api_key install only requires ``api_key``. All base URLs
        and ``default_model`` are optional.
        """
        if not self.api_key:
            logger.warning(
                "huggingface.install.missing_token",
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
                "api_key": self.api_key,
                "base_url": self.base_url,
                "inference_url": self.inference_url,
                "endpoints_url": self.endpoints_url,
                "default_model": self.default_model,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("huggingface.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="HuggingFace connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="Bearer",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify HuggingFace API connectivity by calling GET /whoami-v2."""
        try:
            await with_retry(
                lambda: self.http_client.whoami(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="HuggingFace API reachable",
            )
        except HuggingFaceAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"HuggingFace auth failed: {exc}",
            )
        except HuggingFaceNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"HuggingFace network error: {exc}",
            )
        except HuggingFaceError as exc:
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
        """Sync the authenticated user's HuggingFace models into the Shielva KB.

        Lists models authored by the authenticated user (or by the first org
        the user belongs to), normalises each, and ingests them.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            whoami = await with_retry(
                lambda: self.http_client.whoami(), max_retries=2,
            )
            author = whoami.get("name") or ""
            if not author and whoami.get("orgs"):
                orgs = whoami.get("orgs") or []
                if orgs and isinstance(orgs[0], dict):
                    author = orgs[0].get("name", "")

            models = await with_retry(
                lambda: self.http_client.list_models(author=author or None, limit=100),
                max_retries=3,
            )
            for raw in models or []:
                documents_found += 1
                try:
                    doc = normalize_model(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "huggingface.sync.model_failed",
                        model_id=raw.get("id") if isinstance(raw, dict) else None,
                        error=str(exc),
                    )
                    documents_failed += 1

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} HuggingFace models",
            )
        except Exception as exc:
            logger.error(
                "huggingface.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    # ─── Hub: identity ────────────────────────────────────────────────────
    async def whoami(self) -> Dict[str, Any]:
        """GET /whoami-v2 — returns the authenticated user's identity."""
        return await with_retry(
            lambda: self.http_client.whoami(),
            max_retries=3,
        )

    # ─── Hub: models ──────────────────────────────────────────────────────
    async def list_models(
        self,
        search: Optional[str] = None,
        author: Optional[str] = None,
        filter: Optional[str] = None,
        limit: int = 20,
        sort: str = "downloads",
    ) -> List[Dict[str, Any]]:
        """GET /models — list Hub models matching the given filters."""
        return await with_retry(
            lambda: self.http_client.list_models(
                search=search,
                author=author,
                filter=filter,
                limit=limit,
                sort=sort,
            ),
            max_retries=3,
        )

    async def get_model(self, model_id: str) -> Dict[str, Any]:
        """GET /models/{id} — fetch a single model's full metadata."""
        return await with_retry(
            lambda: self.http_client.get_model(sanitize_model_id(model_id)),
            max_retries=3,
        )

    # ─── Hub: datasets ────────────────────────────────────────────────────
    async def list_datasets(
        self,
        search: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """GET /datasets — list Hub datasets matching the given search."""
        return await with_retry(
            lambda: self.http_client.list_datasets(search=search, limit=limit),
            max_retries=3,
        )

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """GET /datasets/{id} — fetch a single dataset's metadata."""
        return await with_retry(
            lambda: self.http_client.get_dataset(sanitize_model_id(dataset_id)),
            max_retries=3,
        )

    # ─── Hub: spaces ──────────────────────────────────────────────────────
    async def list_spaces(
        self,
        search: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """GET /spaces — list HuggingFace Spaces matching the given search."""
        return await with_retry(
            lambda: self.http_client.list_spaces(search=search, limit=limit),
            max_retries=3,
        )

    async def list_organization_repos(self, organization: str) -> Dict[str, Any]:
        """GET /organizations/{name} — list public repos for an organization."""
        return await with_retry(
            lambda: self.http_client.list_organization_repos(organization),
            max_retries=3,
        )

    # ─── Inference API ────────────────────────────────────────────────────
    async def run_inference(
        self,
        model: str,
        inputs: Any,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """POST {inference}/models/{model} — generic JSON inference entry point.

        Use this when the task is not one of the named helpers below
        (text_generation, feature_extraction, etc.). The body shape is
        ``{"inputs": ..., "parameters": ...}``.
        """
        payload: Dict[str, Any] = {"inputs": inputs}
        if parameters:
            payload["parameters"] = parameters
        return await self.http_client.inference_json(
            sanitize_model_id(model), payload, context="run_inference",
        )

    async def text_generation(
        self,
        model: str,
        inputs: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """POST {inference}/models/{model} — run a text-generation completion."""
        payload: Dict[str, Any] = {"inputs": inputs}
        if parameters:
            payload["parameters"] = parameters
        return await self.http_client.inference_json(
            sanitize_model_id(model), payload, context="text_generation",
        )

    async def feature_extraction(
        self,
        model: str,
        inputs: str,
    ) -> Any:
        """POST {inference}/models/{model} — return the embedding vector(s) for *inputs*."""
        return await self.http_client.inference_json(
            sanitize_model_id(model),
            {"inputs": inputs},
            context="feature_extraction",
        )

    async def text_classification(
        self,
        model: str,
        inputs: str,
    ) -> Any:
        """POST {inference}/models/{model} — return classification scores for *inputs*."""
        return await self.http_client.inference_json(
            sanitize_model_id(model),
            {"inputs": inputs},
            context="text_classification",
        )

    async def summarization(
        self,
        model: str,
        inputs: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """POST {inference}/models/{model} — summarize *inputs*."""
        payload: Dict[str, Any] = {"inputs": inputs}
        if parameters:
            payload["parameters"] = parameters
        return await self.http_client.inference_json(
            sanitize_model_id(model), payload, context="summarization",
        )

    async def translation(
        self,
        model: str,
        inputs: str,
    ) -> Any:
        """POST {inference}/models/{model} — translate *inputs*."""
        return await self.http_client.inference_json(
            sanitize_model_id(model),
            {"inputs": inputs},
            context="translation",
        )

    async def image_classification(
        self,
        model: str,
        image_bytes: bytes,
    ) -> Any:
        """POST {inference}/models/{model} — classify a raw image (binary body)."""
        if not isinstance(image_bytes, (bytes, bytearray)):
            raise ValueError("image_bytes must be bytes")
        return await self.http_client.inference_binary(
            sanitize_model_id(model),
            bytes(image_bytes),
            context="image_classification",
        )

    # ─── Inference Endpoints (managed deployments) ────────────────────────
    async def list_endpoints(self) -> Dict[str, Any]:
        """GET {endpoints}/endpoint — list managed Inference Endpoints."""
        return await with_retry(
            lambda: self.http_client.list_endpoints(),
            max_retries=3,
        )

    async def get_endpoint(self, endpoint_name: str) -> Dict[str, Any]:
        """GET {endpoints}/endpoint/{name}."""
        return await with_retry(
            lambda: self.http_client.get_endpoint(endpoint_name),
            max_retries=3,
        )

    async def create_endpoint(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST {endpoints}/endpoint — create a managed deployment."""
        return await self.http_client.create_endpoint(payload)

    async def delete_endpoint(self, endpoint_name: str) -> Dict[str, Any]:
        """DELETE {endpoints}/endpoint/{name}."""
        return await self.http_client.delete_endpoint(endpoint_name)

    async def run_inference_endpoint(
        self,
        endpoint_url: str,
        payload: Dict[str, Any],
    ) -> Any:
        """POST {endpoint_url} — invoke a managed Inference Endpoint."""
        return await self.http_client.run_inference_endpoint(endpoint_url, payload)
