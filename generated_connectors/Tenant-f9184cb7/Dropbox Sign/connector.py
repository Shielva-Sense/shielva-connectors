"""Dropbox Sign (formerly HelloSign) connector — orchestration only.

All HTTP calls       → client/http_client.py
All normalization    → helpers/normalizer.py
All utilities/retry  → helpers/utils.py
Auth model           → HTTP Basic with api_key as username, empty password
                       (`Authorization: Basic base64(api_key:)`).
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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

from client.http_client import DropboxSignHTTPClient
from exceptions import (
    DropboxSignAuthError,
    DropboxSignError,
    DropboxSignNetworkError,
    DropboxSignNotFoundError,
    DropboxSignRateLimitError,
)
from helpers.normalizer import normalize_signature_request
from helpers.utils import validate_signers, with_retry

logger = structlog.get_logger(__name__)

_DROPBOX_SIGN_BASE = "https://api.hellosign.com/v3"


class DropboxSignConnector(BaseConnector):
    """Shielva connector for the Dropbox Sign (HelloSign) REST API.

    Surfaces (per provider docs):
        - Signature Requests: list, get, send, send_with_template, cancel,
          remind, download (PDF or ZIP), create_embedded
        - Templates: list, get
        - Account: get
        - Team: list members
        - Unclaimed drafts: list
        - API Apps: create

    Auth — HTTP Basic, where the username is the api_key and the password is
    empty. Implemented in `client/http_client.py`.
    """

    CONNECTOR_TYPE = "dropbox_sign"
    CONNECTOR_NAME = "Dropbox Sign"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_key",
    ]

    OPTIONAL_CONFIG_KEYS: List[str] = [
        "client_id",
        "test_mode_default",
        "base_url",
        "rate_limit_per_min",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Tuple[str, str]] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_key: str = self.config.get("api_key", "")
        self.client_id: str = self.config.get("client_id", "")
        self.base_url: str = self.config.get("base_url", "") or _DROPBOX_SIGN_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)
        self.test_mode_default: bool = bool(self.config.get("test_mode_default", True))

        self.http_client = DropboxSignHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Dropbox Sign API-key install only requires `api_key`. We persist the
        non-secret config knobs and probe `/account` once to confirm the
        credential actually works — a green install means the key is live.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning(
                "dropbox_sign.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        await self.save_config({
            "client_id": self.client_id,
            "base_url": self.base_url,
            "rate_limit_per_min": self.rate_limit_per_min,
            "test_mode_default": self.test_mode_default,
        })

        # Probe `/account` once to confirm the API key is live. We do not
        # `with_retry` this — install is interactive; a fast failure is
        # better than a slow one.
        self.http_client.set_api_key(api_key)
        try:
            await self.http_client.get_account()
        except DropboxSignAuthError as exc:
            logger.warning(
                "dropbox_sign.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=f"API key rejected: {exc}",
            )
        except DropboxSignError as exc:
            logger.warning(
                "dropbox_sign.install.probe_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.AUTHENTICATED,
                message=f"Installed, but probe failed: {exc}",
            )

        logger.info("dropbox_sign.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Dropbox Sign connector installed and API key verified",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose `access_token` is the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Dropbox Sign API connectivity by hitting `/account`."""
        self.http_client.set_api_key(self.api_key)
        try:
            await with_retry(
                lambda: self.http_client.get_account(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Dropbox Sign API reachable",
            )
        except DropboxSignAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"API key rejected: {exc}",
            )
        except DropboxSignRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Dropbox Sign rate limited: {exc}",
            )
        except DropboxSignNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Network error: {exc}",
            )
        except DropboxSignError as exc:
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
        """Sync Dropbox Sign signature requests into the Shielva KB.

        Pages through `/signature_request/list`, normalizes each entry to a
        NormalizedDocument (tenant-scoped id), and ingests it. Returns a
        SyncResult with documents_found / synced / failed.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        self.http_client.set_api_key(self.api_key)
        try:
            page = 1
            while True:
                resp = await with_retry(
                    lambda p=page: self.http_client.list_signature_requests(
                        page=p, page_size=100,
                    ),
                    max_retries=3,
                )
                items = resp.get("signature_requests", []) if isinstance(resp, dict) else []
                for raw in items or []:
                    documents_found += 1
                    try:
                        doc = normalize_signature_request(
                            {"signature_request": raw},
                            tenant_id=self.tenant_id,
                            connector_id=self.connector_id,
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "dropbox_sign.sync.signature_request_failed",
                            error=str(exc),
                        )
                        documents_failed += 1

                list_info = resp.get("list_info", {}) if isinstance(resp, dict) else {}
                num_pages = int(list_info.get("num_pages", 1) or 1)
                if page >= num_pages or not items:
                    break
                page += 1

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=(
                    f"Synced {documents_synced}/{documents_found} "
                    f"Dropbox Sign signature requests"
                ),
            )
        except Exception as exc:
            logger.error(
                "dropbox_sign.sync.failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Account ────────────────────────────────────────────────────────────

    async def get_account(self) -> Dict[str, Any]:
        """GET /account — return the authenticated account payload."""
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.get_account(),
            max_retries=3,
        )

    # ── Signature requests ────────────────────────────────────────────────

    async def list_signature_requests(
        self,
        page: int = 1,
        page_size: int = 20,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /signature_request/list — paginated list of signature requests."""
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.list_signature_requests(
                page=page, page_size=page_size, query=query,
            ),
            max_retries=3,
        )

    async def get_signature_request(self, signature_request_id: str) -> Dict[str, Any]:
        """GET /signature_request/{id} — full signature request payload."""
        if not signature_request_id:
            raise ValueError("signature_request_id is required")
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.get_signature_request(signature_request_id),
            max_retries=3,
        )

    async def send_signature_request(
        self,
        title: str,
        subject: str,
        message: str,
        signers: List[Dict[str, Any]],
        file_urls: Optional[List[str]] = None,
        files: Optional[List[Tuple[str, bytes, str]]] = None,
        test_mode: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """POST /signature_request/send — kick off a new signature request.

        Args:
            title: Internal title for the signature request.
            subject: Email subject seen by signers.
            message: Body of the email signers receive.
            signers: list of `{"name": "...", "email_address": "..."}` dicts.
            file_urls: publicly downloadable URLs for the docs to be signed.
            files: list of `(filename, bytes, content_type)` tuples (multipart upload).
            test_mode: per-call override; falls back to the install-time default.

        At least one of `file_urls` or `files` is required.
        """
        validate_signers(signers)
        if not file_urls and not files:
            raise ValueError("send_signature_request requires file_urls or files")

        effective_test_mode = (
            self.test_mode_default if test_mode is None else bool(test_mode)
        )
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.send_signature_request(
                title=title,
                subject=subject,
                message=message,
                signers=signers,
                file_urls=file_urls,
                files=files,
                test_mode=effective_test_mode,
            ),
            max_retries=3,
        )

    async def cancel_signature_request(self, signature_request_id: str) -> Dict[str, Any]:
        """POST /signature_request/cancel/{id} — cancel an in-flight signature request."""
        if not signature_request_id:
            raise ValueError("signature_request_id is required")
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.cancel_signature_request(signature_request_id),
            max_retries=3,
        )

    async def remind_signature_request(
        self,
        signature_request_id: str,
        email_address: str,
    ) -> Dict[str, Any]:
        """POST /signature_request/remind/{id} — nudge a signer."""
        if not signature_request_id:
            raise ValueError("signature_request_id is required")
        if not email_address:
            raise ValueError("email_address is required")
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.remind_signature_request(
                signature_request_id, email_address,
            ),
            max_retries=3,
        )

    async def download_files(
        self,
        signature_request_id: str,
        file_type: str = "pdf",
    ) -> bytes:
        """GET /signature_request/files/{id}?file_type=… — returns raw bytes (PDF or ZIP).

        `file_type` must be 'pdf' or 'zip'.
        """
        if not signature_request_id:
            raise ValueError("signature_request_id is required")
        if file_type not in ("pdf", "zip"):
            raise ValueError("file_type must be 'pdf' or 'zip'")
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.download_signature_request(
                signature_request_id, file_type=file_type,
            ),
            max_retries=3,
        )

    # Back-compat alias for older callers.
    download_signature_request = download_files

    # ── Templates ─────────────────────────────────────────────────────────

    async def list_templates(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """GET /template/list — paginated list of templates owned/shared with this account."""
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.list_templates(page=page, page_size=page_size),
            max_retries=3,
        )

    async def get_template(self, template_id: str) -> Dict[str, Any]:
        """GET /template/{id} — full template payload."""
        if not template_id:
            raise ValueError("template_id is required")
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.get_template(template_id),
            max_retries=3,
        )

    async def send_with_template(
        self,
        template_id: str,
        title: str,
        subject: str,
        message: str,
        signers: List[Dict[str, Any]],
        custom_fields: Optional[List[Dict[str, Any]]] = None,
        test_mode: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """POST /signature_request/send_with_template — create a request from a template.

        `signers` is a list of `{"role": "Client", "name": "...", "email_address": "..."}`.
        """
        if not template_id:
            raise ValueError("template_id is required")
        validate_signers(signers)
        effective_test_mode = (
            self.test_mode_default if test_mode is None else bool(test_mode)
        )
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.send_with_template(
                template_id=template_id,
                title=title,
                subject=subject,
                message=message,
                signers=signers,
                custom_fields=custom_fields,
                test_mode=effective_test_mode,
            ),
            max_retries=3,
        )

    # ── Team ──────────────────────────────────────────────────────────────

    async def list_team_members(self, page: int = 1) -> Dict[str, Any]:
        """GET /team — list members of the authenticated user's team."""
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.list_team_members(page=page),
            max_retries=3,
        )

    # ── Unclaimed drafts ──────────────────────────────────────────────────

    async def list_unclaimed_drafts(self) -> Dict[str, Any]:
        """GET /unclaimed_draft/list — return drafts pending claim."""
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.list_unclaimed_drafts(),
            max_retries=3,
        )

    # ── Embedded signing ──────────────────────────────────────────────────

    async def create_embedded_signature_request(
        self,
        client_id: str,
        title: str,
        signers: List[Dict[str, Any]],
        file_urls: Optional[List[str]] = None,
        test_mode: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """POST /signature_request/create_embedded — produce an embedded sign URL.

        Requires the Dropbox Sign API app `client_id`; pass it explicitly so
        this method can be used regardless of whether one was set at install
        time.
        """
        if not client_id:
            raise ValueError("client_id is required for embedded signature requests")
        validate_signers(signers)
        effective_test_mode = (
            self.test_mode_default if test_mode is None else bool(test_mode)
        )
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.create_embedded_signature_request(
                client_id=client_id,
                title=title,
                signers=signers,
                file_urls=file_urls,
                test_mode=effective_test_mode,
            ),
            max_retries=3,
        )

    # ── API Apps ──────────────────────────────────────────────────────────

    async def create_api_app(
        self,
        name: str,
        domain: str,
        callback_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /api_app — register a new API App for embedded signing."""
        if not name:
            raise ValueError("name is required")
        if not domain:
            raise ValueError("domain is required")
        self.http_client.set_api_key(self.api_key)
        return await with_retry(
            lambda: self.http_client.create_api_app(
                name=name, domain=domain, callback_url=callback_url,
            ),
            max_retries=3,
        )
