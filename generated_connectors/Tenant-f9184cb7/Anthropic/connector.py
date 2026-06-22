"""Anthropic (Claude API) connector — orchestration only.

All HTTP calls         → client/http_client.py
All normalization      → helpers/normalizer.py
All utilities          → helpers/utils.py
All error mapping      → exceptions.py

Auth: API key sent in the ``x-api-key`` header (NOT ``Authorization``), plus
the mandatory ``anthropic-version: 2023-06-01`` header. There is no OAuth
dance, no token refresh, no expiry.

Required headers on every request::

    x-api-key:          <api_key>
    anthropic-version:  2023-06-01
    content-type:       application/json
    anthropic-beta:     files-api-2025-04-14   (only on /files calls)
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

from client.http_client import AnthropicHTTPClient
from exceptions import (
    AnthropicAuthError,
    AnthropicError,
    AnthropicNetworkError,
)
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_ANTHROPIC_BASE = "https://api.anthropic.com/v1"
_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicConnector(BaseConnector):
    """Shielva connector for the Anthropic (Claude) Messages + Models + Batches + Files APIs."""

    CONNECTOR_TYPE = "anthropic"
    CONNECTOR_NAME = "Anthropic"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

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
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        self.api_key: str = self.config.get("api_key", "")
        self.base_url: str = self.config.get("base_url") or _ANTHROPIC_BASE
        self.anthropic_version: str = (
            self.config.get("anthropic_version") or _DEFAULT_ANTHROPIC_VERSION
        )
        # Install-fields may arrive as strings — coerce defensively.
        try:
            self.rate_limit_per_min: int = int(
                self.config.get("rate_limit_per_min") or 50
            )
        except (TypeError, ValueError):
            self.rate_limit_per_min = 50
        try:
            self.timeout_s: float = float(self.config.get("timeout_s") or 60)
        except (TypeError, ValueError):
            self.timeout_s = 60.0

        self.http_client = AnthropicHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
            anthropic_version=self.anthropic_version,
            rate_limit_per_min=self.rate_limit_per_min,
            timeout=self.timeout_s,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        For api-key connectors the install path is purely structural: we
        verify ``api_key`` is non-empty. A separate ``health_check()`` call
        performs the live round-trip to api.anthropic.com.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning(
                "anthropic.install.missing_credentials",
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
                "anthropic_version": self.anthropic_version,
                "rate_limit_per_min": self.rate_limit_per_min,
                "timeout_s": self.timeout_s,
            }
        )
        logger.info("anthropic.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Anthropic connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        ``TokenInfo`` whose ``access_token`` is the configured ``api_key``.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Anthropic API connectivity by listing 1 model.

        We deliberately use ``/models?limit=1`` instead of pinging
        ``/messages`` — the latter would burn input tokens against the
        tenant's billing account on every health poll.
        """
        try:
            await with_retry(
                lambda: self.http_client.list_models(limit=1),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Anthropic API reachable",
            )
        except AnthropicAuthError as exc:
            # 401 → OFFLINE+TOKEN_EXPIRED, 403 → UNHEALTHY+INVALID_CREDENTIALS
            if exc.status_code == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Anthropic forbidden: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Anthropic auth failed: {exc}",
            )
        except AnthropicNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Anthropic network error: {exc}",
            )
        except AnthropicError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """No-op sync.

        Anthropic is an inference API, not a document store, so there is
        nothing to crawl into a knowledge base. We return a COMPLETED
        zero-document SyncResult so callers that invoke ``sync()``
        polymorphically do not error out.
        """
        return SyncResult(
            status=SyncStatus.COMPLETED,
            connector_id=self.connector_id,
            documents_found=0,
            documents_synced=0,
            documents_failed=0,
            message=(
                "Anthropic is an inference API, not a document source — "
                "sync is a no-op. Use create_message / count_tokens / "
                "create_batch instead."
            ),
        )

    # ── BaseConnector webhook overrides (Anthropic has no webhooks) ───────

    async def handle_webhook(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Documented no-op — Anthropic has no provider-pushed webhooks today."""
        logger.info(
            "anthropic.webhook.ignored",
            connector_id=self.connector_id,
            reason="anthropic_has_no_webhooks",
        )
        return {"status": "ignored", "reason": "anthropic_has_no_webhooks"}

    async def process_callback(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Documented no-op — no OAuth or signed-callback flow."""
        return {"status": "ignored", "reason": "anthropic_has_no_callbacks"}

    async def handle_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Documented no-op event handler."""
        return {"status": "ignored", "reason": "anthropic_has_no_events"}

    async def batch_processor(self, items: list, **kwargs: Any) -> Dict[str, Any]:
        """Documented no-op batch processor (use ``create_batch`` instead)."""
        return {"processed": 0, "items": []}

    # ── Public API methods — Messages ──────────────────────────────────────

    async def create_message(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        max_tokens: int = 1024,
        system: Optional[str] = None,
        temperature: float = 1.0,
        stream: bool = False,
    ) -> Dict[str, Any]:
        """POST /messages — create a chat completion."""
        return await with_retry(
            lambda: self.http_client.create_message(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                system=system,
                temperature=temperature,
                stream=stream,
            ),
            max_retries=3,
        )

    async def count_tokens(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /messages/count_tokens — input-token estimate."""
        return await with_retry(
            lambda: self.http_client.count_tokens(
                model=model, messages=messages, system=system,
            ),
            max_retries=3,
        )

    # ── Public API methods — Models ────────────────────────────────────────

    async def list_models(
        self,
        limit: int = 20,
        before_id: Optional[str] = None,
        after_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /models — list models available to this api key."""
        return await with_retry(
            lambda: self.http_client.list_models(
                limit=limit, before_id=before_id, after_id=after_id,
            ),
            max_retries=3,
        )

    async def get_model(self, model_id: str) -> Dict[str, Any]:
        """GET /models/{id} — fetch a single model's metadata."""
        return await with_retry(
            lambda: self.http_client.get_model(model_id),
            max_retries=3,
        )

    # ── Public API methods — Message Batches ──────────────────────────────

    async def create_batch(self, requests: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /messages/batches — submit a Message Batch."""
        return await with_retry(
            lambda: self.http_client.create_batch(requests),
            max_retries=3,
        )

    async def get_batch(self, batch_id: str) -> Dict[str, Any]:
        """GET /messages/batches/{id} — batch status."""
        return await with_retry(
            lambda: self.http_client.get_batch(batch_id),
            max_retries=3,
        )

    async def list_batches(
        self,
        limit: int = 20,
        before_id: Optional[str] = None,
        after_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /messages/batches — list all batches."""
        return await with_retry(
            lambda: self.http_client.list_batches(
                limit=limit, before_id=before_id, after_id=after_id,
            ),
            max_retries=3,
        )

    async def cancel_batch(self, batch_id: str) -> Dict[str, Any]:
        """POST /messages/batches/{id}/cancel — cancel an in-flight batch."""
        return await self.http_client.cancel_batch(batch_id)

    async def get_batch_results(self, batch_id: str) -> Dict[str, Any]:
        """GET /messages/batches/{id}/results — fetch results of a completed batch."""
        return await with_retry(
            lambda: self.http_client.get_batch_results(batch_id),
            max_retries=3,
        )

    # ── Public API methods — Files (beta) ─────────────────────────────────

    async def list_files(
        self,
        limit: int = 20,
        before_id: Optional[str] = None,
        after_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /files — list uploaded files (beta)."""
        return await with_retry(
            lambda: self.http_client.list_files(
                limit=limit, before_id=before_id, after_id=after_id,
            ),
            max_retries=3,
        )

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        """GET /files/{id} — file metadata (beta)."""
        return await with_retry(
            lambda: self.http_client.get_file(file_id),
            max_retries=3,
        )

    async def delete_file(self, file_id: str) -> Dict[str, Any]:
        """DELETE /files/{id} — delete a file (beta)."""
        return await self.http_client.delete_file(file_id)
