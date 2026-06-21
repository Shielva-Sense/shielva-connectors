"""Adobe Sign connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: OAuth 2.0 Authorization Code Grant. Adobe Sign issues per-shard access
tokens — after token exchange the connector calls ``GET /baseUris`` to learn
which shard (na1, eu1, jp1, etc.) the account lives on and pivots its API
base URL onto that origin.
"""
from datetime import datetime, timedelta, timezone
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

from client.http_client import AdobeSignHTTPClient
from exceptions import (
    AdobeSignAuthError,
    AdobeSignError,
    AdobeSignNotFoundError,
    AdobeSignServerError,
)
from helpers.normalizer import (
    normalize_agreement,
    normalize_agreements_page,
    normalize_library_document,
)
from helpers.utils import (
    api_base_url_from_access_point,
    build_oauth_authorize_url,
    with_retry,
)

logger = structlog.get_logger(__name__)

_DEFAULT_OAUTH_HOST = "https://secure.na1.adobesign.com"
_DEFAULT_API_BASE = "https://api.na1.adobesign.com/api/rest/v6"
_DEFAULT_SCOPES = (
    "user_read user_write "
    "agreement_read agreement_write agreement_send "
    "library_read library_write "
    "workflow_read"
)


class AdobeSignConnector(BaseConnector):
    """Shielva connector for the Adobe Sign REST API v6.

    Surfaces: agreements (create, list, get, remind, cancel, download),
    library documents, users, groups, workflows, webhooks.
    """

    CONNECTOR_TYPE = "adobe_sign"
    CONNECTOR_NAME = "Adobe Sign"
    AUTH_TYPE = "oauth2_code"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "client_id",
        "client_secret",
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
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.oauth_host: str = (
            self.config.get("oauth_host") or _DEFAULT_OAUTH_HOST
        )
        self.api_access_point: str = self.config.get("api_access_point", "")
        self.api_base_url: str = (
            api_base_url_from_access_point(self.api_access_point)
            or self.config.get("api_base_url")
            or _DEFAULT_API_BASE
        )
        self.scopes: str = self.config.get("scopes") or _DEFAULT_SCOPES
        self.timeout_s: float = float(self.config.get("timeout_s") or 30)
        self.access_token: str = self.config.get("access_token", "")

        self.http_client = AdobeSignHTTPClient(
            access_token=self.access_token,
            base_url=self.api_base_url,
            oauth_host=self.oauth_host,
            timeout=self.timeout_s,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        ``client_id`` + ``client_secret`` are mandatory. ``oauth_host`` defaults
        to the NA1 shard; ``api_access_point`` is discovered post-authorize().
        """
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        if not client_id or not client_secret:
            logger.warning(
                "adobe_sign.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )

        await self.save_config(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "oauth_host": self.oauth_host,
                "api_access_point": self.api_access_point,
                "api_base_url": self.api_base_url,
                "scopes": self.scopes,
                "timeout_s": self.timeout_s,
            }
        )
        logger.info("adobe_sign.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Adobe Sign connector installed — complete OAuth to activate",
        )

    def get_oauth_url(
        self,
        redirect_uri: str,
        state: str = None,
        use_pkce: bool = False,
    ) -> str:
        """Build the Adobe Sign OAuth authorize URL.

        Override the BaseConnector default because Adobe's authorize endpoint
        lives on a shard-specific host (``{oauth_host}/public/oauth/v2``).
        """
        return build_oauth_authorize_url(
            oauth_host=self.oauth_host,
            client_id=self.client_id,
            redirect_uri=redirect_uri,
            scopes=self.scopes,
            state=state or "",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """Exchange the OAuth code for tokens, then discover the home shard."""
        if not auth_code:
            raise AdobeSignAuthError("auth_code is required for OAuth code exchange")

        redirect_uri = self.config.get("redirect_uri", "") or state
        token_resp = await self.http_client.exchange_code_for_token(
            client_id=self.client_id,
            client_secret=self.client_secret,
            auth_code=auth_code,
            redirect_uri=redirect_uri,
        )

        access_token = token_resp.get("access_token", "")
        refresh_token = token_resp.get("refresh_token")
        expires_in = int(token_resp.get("expires_in") or 3600)
        scopes_str = token_resp.get("scope", "") or ""
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        self.access_token = access_token
        self.http_client.set_access_token(access_token)

        # ── Shard discovery: pivot api_base_url onto the home shard.
        try:
            base_uris = await self.http_client.get_base_uris()
            api_access_point = base_uris.get("apiAccessPoint", "")
            if api_access_point:
                self.api_access_point = api_access_point
                self.api_base_url = api_base_url_from_access_point(api_access_point)
                self.http_client.set_base_url(self.api_base_url)
                logger.info(
                    "adobe_sign.shard_discovered",
                    api_base_url=self.api_base_url,
                    connector_id=self.connector_id,
                )
        except AdobeSignError as exc:
            logger.warning(
                "adobe_sign.shard_discovery_failed",
                error=str(exc),
                connector_id=self.connector_id,
            )

        await self.save_config(
            {
                **self.config,
                "access_token": access_token,
                "api_access_point": self.api_access_point,
                "api_base_url": self.api_base_url,
            }
        )

        return TokenInfo(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            token_type=token_resp.get("token_type", "Bearer"),
            scopes=[s for s in scopes_str.split() if s],
            metadata={"api_access_point": self.api_access_point},
            raw=token_resp,
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Adobe Sign API connectivity with a ``GET /users/me`` probe."""
        try:
            await with_retry(
                lambda: self.http_client.get_me(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Adobe Sign API reachable",
            )
        except AdobeSignAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Adobe Sign auth failed: {exc}",
            )
        except AdobeSignServerError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Adobe Sign network error: {exc}",
            )
        except AdobeSignError as exc:
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
        """Sync Adobe Sign agreements + library documents into the Shielva KB."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            agreements_resp = await with_retry(
                lambda: self.http_client.list_agreements(page_size=100),
                max_retries=3,
            )
            agreements = (
                agreements_resp.get("userAgreementList")
                or agreements_resp.get("agreementList")
                or agreements_resp.get("agreements")
                or []
            )
            for raw in agreements:
                documents_found += 1
                try:
                    doc = normalize_agreement(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "adobe_sign.sync.agreement_failed",
                        error=str(exc),
                        connector_id=self.connector_id,
                    )
                    documents_failed += 1

            library_resp = await with_retry(
                lambda: self.http_client.list_library_documents(page_size=100),
                max_retries=3,
            )
            library = (
                library_resp.get("libraryDocumentList")
                or library_resp.get("libraryDocuments")
                or []
            )
            for raw in library:
                documents_found += 1
                try:
                    doc = normalize_library_document(
                        raw, self.connector_id, self.tenant_id,
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "adobe_sign.sync.library_failed",
                        error=str(exc),
                        connector_id=self.connector_id,
                    )
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Adobe Sign documents",
            )
        except Exception as exc:
            logger.error(
                "adobe_sign.sync.failed",
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

    async def get_base_uris(self) -> Dict[str, Any]:
        """``GET /baseUris`` — fetch the caller's shard-specific access points."""
        return await with_retry(
            lambda: self.http_client.get_base_uris(),
            max_retries=3,
        )

    # ── Agreements ─────────────────────────────────────────────────────────

    async def create_agreement(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /agreements`` — create a new agreement and (optionally) send it."""
        return await self.http_client.create_agreement(payload)

    async def get_agreement(self, agreement_id: str) -> Dict[str, Any]:
        """``GET /agreements/{agreementId}``."""
        return await with_retry(
            lambda: self.http_client.get_agreement(agreement_id),
            max_retries=3,
        )

    async def list_agreements(
        self,
        *,
        cursor: Optional[str] = None,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """``GET /agreements`` — cursor-paginated."""
        return await with_retry(
            lambda: self.http_client.list_agreements(
                cursor=cursor, page_size=page_size,
            ),
            max_retries=3,
        )

    async def send_reminder(
        self,
        agreement_id: str,
        participant_emails: Optional[List[str]] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST /agreements/{agreementId}/reminders``.

        Adobe expects ``recipientParticipantIds`` — when callers only have
        emails they must resolve participant IDs first via
        ``get_agreement(...)['participantSetsInfo']``.
        """
        payload: Dict[str, Any] = {
            "recipientParticipantIds": participant_emails or [],
            "status": "ACTIVE",
        }
        if note:
            payload["note"] = note
        return await self.http_client.send_reminder(agreement_id, payload)

    async def cancel_agreement(
        self,
        agreement_id: str,
        comment: Optional[str] = None,
        notify_signer: bool = True,
    ) -> Dict[str, Any]:
        """``PUT /agreements/{agreementId}/state`` — set state to ``CANCELLED``."""
        payload: Dict[str, Any] = {
            "state": "CANCELLED",
            "agreementCancellationInfo": {
                "comment": comment or "",
                "notifyOthers": notify_signer,
            },
        }
        return await self.http_client.cancel_agreement(agreement_id, payload)

    async def download_agreement(self, agreement_id: str) -> bytes:
        """``GET /agreements/{agreementId}/combinedDocument`` — returns PDF bytes."""
        return await with_retry(
            lambda: self.http_client.download_agreement(agreement_id),
            max_retries=3,
        )

    # ── Library documents ──────────────────────────────────────────────────

    async def list_library_documents(
        self,
        *,
        cursor: Optional[str] = None,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """``GET /libraryDocuments``."""
        return await with_retry(
            lambda: self.http_client.list_library_documents(
                cursor=cursor, page_size=page_size,
            ),
            max_retries=3,
        )

    async def get_library_document(self, library_document_id: str) -> Dict[str, Any]:
        """``GET /libraryDocuments/{libraryDocumentId}``."""
        return await with_retry(
            lambda: self.http_client.get_library_document(library_document_id),
            max_retries=3,
        )

    # ── Users ──────────────────────────────────────────────────────────────

    async def list_users(self, *, cursor: Optional[str] = None) -> Dict[str, Any]:
        """``GET /users``."""
        return await with_retry(
            lambda: self.http_client.list_users(cursor=cursor),
            max_retries=3,
        )

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """``GET /users/{userId}``."""
        return await with_retry(
            lambda: self.http_client.get_user(user_id),
            max_retries=3,
        )

    # ── Workflows ──────────────────────────────────────────────────────────

    async def list_workflows(self, *, cursor: Optional[str] = None) -> Dict[str, Any]:
        """``GET /workflows`` — list published custom workflows."""
        return await with_retry(
            lambda: self.http_client.list_workflows(cursor=cursor),
            max_retries=3,
        )

    # ── Webhooks ───────────────────────────────────────────────────────────

    async def list_webhooks(self, *, cursor: Optional[str] = None) -> Dict[str, Any]:
        """``GET /webhooks``."""
        return await with_retry(
            lambda: self.http_client.list_webhooks(cursor=cursor),
            max_retries=3,
        )

    async def create_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /webhooks``."""
        return await self.http_client.create_webhook(payload)
