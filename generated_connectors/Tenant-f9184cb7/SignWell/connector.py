"""SignWell connector — orchestration only.

All HTTP calls    → client/http_client.py
All retries       → helpers/utils.py
All normalization → helpers/normalizer.py

Auth: header-based API key. The key is sent in `X-Api-Key` (NOT
`Authorization: Bearer …`). Required headers:

    X-Api-Key:    <api_key>
    Accept:       application/json
    Content-Type: application/json   (when a body is present)
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

from client.http_client import SignWellHTTPClient
from exceptions import (
    SignWellAuthError,
    SignWellError,
    SignWellNetworkError,
    SignWellNotFoundError,
    SignWellRateLimitError,
    SignWellServerError,
)
from helpers.normalizer import normalize_document
from helpers.utils import validate_recipients, with_retry

logger = structlog.get_logger(__name__)

_SIGNWELL_BASE = "https://www.signwell.com/api/v1"


class SignWellConnector(BaseConnector):
    """Shielva connector for the SignWell e-signature REST API."""

    CONNECTOR_TYPE = "signwell"
    CONNECTOR_NAME = "SignWell"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]
    OPTIONAL_CONFIG_KEYS: List[str] = [
        "test_mode_default",
        "base_url",
        "rate_limit_per_min",
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
        self.api_key: str = self.config.get("api_key", "")
        self.base_url: str = self.config.get("base_url", "") or _SIGNWELL_BASE
        self.test_mode_default: bool = bool(self.config.get("test_mode_default", True))
        self.rate_limit_per_min: int = int(
            self.config.get("rate_limit_per_min", 100) or 100
        )

        self.http_client = SignWellHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config — an API key is required.

        SignWell uses a header-based api_key. install() does NOT call the API
        (the health_check probe does that). It validates the key is present,
        persists non-secret config, and materialises a TokenInfo for surface
        compatibility with the OAuth path.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning(
                "signwell.install.missing_api_key",
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
                "test_mode_default": self.test_mode_default,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        await self.set_token(
            TokenInfo(
                access_token=api_key,
                refresh_token=None,
                expires_at=None,
                token_type="ApiKey",
                scopes=[],
            )
        )
        logger.info("signwell.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="SignWell connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """API-key connectors do not use OAuth — return the stored key as a TokenInfo.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_key.
        """
        api_key = self.config.get("api_key", "") or auth_code
        if not api_key:
            raise SignWellAuthError("No api_key configured for SignWell connector")
        token_info = TokenInfo(
            access_token=api_key,
            refresh_token=None,
            expires_at=None,
            token_type="ApiKey",
            scopes=[],
        )
        await self.set_token(token_info)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify the API key by calling GET /me.

        Failure classification mirrors `_STATUS_MAP`:
          401 → ConnectorStatus(OFFLINE,   TOKEN_EXPIRED)
          403 → ConnectorStatus(UNHEALTHY, INVALID_CREDENTIALS)
          429 → ConnectorStatus(DEGRADED,  CONNECTED)
          5xx / transport → ConnectorStatus(DEGRADED, CONNECTED)
        """
        try:
            await with_retry(lambda: self.http_client.get_me(), max_retries=2)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="SignWell API reachable",
            )
        except SignWellAuthError as exc:
            if exc.status_code == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"API key forbidden: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"API key rejected: {exc}",
            )
        except SignWellRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Rate limited: {exc}",
            )
        except (SignWellServerError, SignWellNetworkError) as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"SignWell transient error: {exc}",
            )
        except SignWellError as exc:
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
        """Sync SignWell documents into the Shielva knowledge base.

        Pages through `/documents`, normalises each via `normalize_document`
        (tenant-scoped id), and ingests. Returns `SyncResult` with per-document
        counts so the gateway can surface PARTIAL on per-item failures without
        failing the whole sync.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        page = 1

        try:
            while True:
                resp = await with_retry(
                    lambda p=page: self.http_client.list_documents(page=p),
                    max_retries=3,
                )
                docs = resp.get("documents") or resp.get("data") or []
                if not docs:
                    break

                for raw in docs:
                    documents_found += 1
                    try:
                        full_raw = await with_retry(
                            lambda did=raw.get("id"): self.http_client.get_document(did),
                            max_retries=2,
                        )
                        norm = normalize_document(
                            full_raw, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            norm, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "signwell.sync.document_failed",
                            document_id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1

                # SignWell paginates with `next_page` / `has_more`; fall back to
                # page count when the API returns neither.
                if not resp.get("next_page") and not resp.get("has_more"):
                    break
                page += 1

            status = (
                SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} documents",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "signwell.sync.failed",
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

    async def get_me(self) -> Dict[str, Any]:
        """GET /me — authenticated account payload."""
        return await with_retry(
            lambda: self.http_client.get_me(),
            max_retries=3,
        )

    async def list_documents(
        self,
        page: int = 1,
        status: Optional[str] = None,
        archived: bool = False,
        q: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /documents — paginated list."""
        return await with_retry(
            lambda: self.http_client.list_documents(
                page=page, status=status, archived=archived, q=q
            ),
            max_retries=3,
        )

    async def get_document(self, document_id: str) -> Dict[str, Any]:
        """GET /documents/{id}."""
        return await with_retry(
            lambda: self.http_client.get_document(document_id),
            max_retries=3,
        )

    async def create_document(
        self,
        name: str,
        recipients: list,
        files: Optional[list] = None,
        file_urls: Optional[list] = None,
        message: Optional[str] = None,
        subject: Optional[str] = None,
        test_mode: Optional[bool] = None,
        draft: bool = False,
        embedded_signing: bool = False,
        expires_in: Optional[int] = None,
        reminders: bool = True,
    ) -> Dict[str, Any]:
        """POST /documents — create a new envelope.

        Either *files* (raw file objects: `{name, file_base64}` or
        `{name, url}`) OR *file_urls* must be provided. *recipients* is a list
        of `{name, email[, id, subject, message]}`. *test_mode* defaults to the
        install-time `test_mode_default`.
        """
        validate_recipients(recipients)
        if not files and not file_urls:
            raise ValueError("at least one of files or file_urls is required")

        effective_test_mode = (
            self.test_mode_default if test_mode is None else bool(test_mode)
        )

        body: Dict[str, Any] = {
            "name": name,
            "recipients": recipients,
            "test_mode": effective_test_mode,
            "draft": draft,
            "embedded_signing": embedded_signing,
            "reminders": reminders,
        }
        if files:
            body["files"] = files
        if file_urls:
            body["file_urls"] = file_urls
        if message is not None:
            body["message"] = message
        if subject is not None:
            body["subject"] = subject
        if expires_in is not None:
            body["expires_in"] = expires_in

        return await with_retry(
            lambda: self.http_client.create_document(body),
            max_retries=2,
        )

    async def send_document(self, document_id: str) -> Dict[str, Any]:
        """POST /documents/{id}/send."""
        return await with_retry(
            lambda: self.http_client.send_document(document_id),
            max_retries=2,
        )

    async def cancel_document(self, document_id: str) -> Dict[str, Any]:
        """POST /documents/{id}/cancel."""
        return await with_retry(
            lambda: self.http_client.cancel_document(document_id),
            max_retries=2,
        )

    async def archive_document(self, document_id: str) -> Dict[str, Any]:
        """POST /documents/{id}/archive."""
        return await with_retry(
            lambda: self.http_client.archive_document(document_id),
            max_retries=2,
        )

    async def delete_document(self, document_id: str) -> Dict[str, Any]:
        """DELETE /documents/{id}."""
        return await with_retry(
            lambda: self.http_client.delete_document(document_id),
            max_retries=2,
        )

    async def download_completed_document(
        self,
        document_id: str,
        type: str = "completed",
    ) -> bytes:
        """GET /documents/{id}/completed_pdf — raw PDF bytes."""
        return await with_retry(
            lambda: self.http_client.download_completed_document(
                document_id, type_=type
            ),
            max_retries=3,
        )

    # Back-compat alias for older callers that used the shorter name.
    async def download_document(
        self,
        document_id: str,
        type: str = "completed",
    ) -> bytes:
        """Alias for `download_completed_document` (back-compat)."""
        return await self.download_completed_document(document_id, type=type)

    async def list_templates(
        self,
        page: int = 1,
        q: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /templates."""
        return await with_retry(
            lambda: self.http_client.list_templates(page=page, q=q),
            max_retries=3,
        )

    async def get_template(self, template_id: str) -> Dict[str, Any]:
        """GET /templates/{id}."""
        return await with_retry(
            lambda: self.http_client.get_template(template_id),
            max_retries=3,
        )

    async def create_document_from_template(
        self,
        template_id: str,
        name: str,
        recipients: list,
        template_fields: Optional[list] = None,
        test_mode: Optional[bool] = None,
        draft: bool = False,
        embedded_signing: bool = False,
    ) -> Dict[str, Any]:
        """POST /document_templates/documents."""
        validate_recipients(recipients)
        effective_test_mode = (
            self.test_mode_default if test_mode is None else bool(test_mode)
        )
        body: Dict[str, Any] = {
            "template_id": template_id,
            "name": name,
            "recipients": recipients,
            "test_mode": effective_test_mode,
            "draft": draft,
            "embedded_signing": embedded_signing,
        }
        if template_fields:
            body["template_fields"] = template_fields
        return await with_retry(
            lambda: self.http_client.create_document_from_template(body),
            max_retries=2,
        )

    async def list_recipients(self, document_id: str) -> Dict[str, Any]:
        """GET /documents/{id}/recipients."""
        return await with_retry(
            lambda: self.http_client.list_recipients(document_id),
            max_retries=3,
        )

    async def send_reminder(
        self,
        document_id: str,
        recipient_id: str,
    ) -> Dict[str, Any]:
        """POST /documents/{did}/recipients/{rid}/reminder."""
        return await with_retry(
            lambda: self.http_client.send_reminder(document_id, recipient_id),
            max_retries=2,
        )

    async def list_webhooks(self) -> Dict[str, Any]:
        """GET /api_application/webhooks."""
        return await with_retry(
            lambda: self.http_client.list_webhooks(),
            max_retries=3,
        )

    async def create_webhook(
        self,
        url: str,
        events: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /api_application/webhooks."""
        if not url:
            raise ValueError("url is required for create_webhook")
        return await with_retry(
            lambda: self.http_client.create_webhook(url=url, events=events),
            max_retries=2,
        )

    async def delete_webhook(self, webhook_id: str) -> Dict[str, Any]:
        """DELETE /api_application/webhooks/{id}."""
        return await with_retry(
            lambda: self.http_client.delete_webhook(webhook_id),
            max_retries=2,
        )
