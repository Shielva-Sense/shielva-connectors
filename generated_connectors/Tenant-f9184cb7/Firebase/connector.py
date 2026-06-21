"""Firebase connector — orchestration only.

All HTTP calls       → client/http_client.py::FirebaseHTTPClient
All normalization    → helpers/normalizer.py
All utilities        → helpers/utils.py
All wire models      → models.py

Auth: Google service-account JSON. The connector signs an RS256 JWT with
the service-account private key, exchanges it at
https://oauth2.googleapis.com/token for an OAuth2 access token, and sends:

    Authorization: Bearer <access_token>
    Content-Type:   application/json

on every Firestore / RTDB / FCM / Identity Toolkit / Storage call.
"""
from datetime import datetime, timezone
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

from client.http_client import FirebaseHTTPClient
from exceptions import (
    FirebaseAuthError,
    FirebaseError,
    FirebaseNetworkError,
    FirebaseNotFoundError,
)
from helpers.normalizer import (
    normalize_auth_user,
    normalize_firestore_document,
)
from helpers.utils import parse_service_account_json, with_retry
from models import CreateUserRequest, FCMMessage, UpdateUserRequest

logger = structlog.get_logger(__name__)


class FirebaseConnector(BaseConnector):
    """Shielva connector for Google Firebase.

    Wraps Firestore (documents), Realtime Database (paths), Cloud Messaging
    (FCM v1), Identity Toolkit (Auth admin), and Cloud Storage for Firebase
    behind a single service-account credential.
    """

    CONNECTOR_TYPE = "firebase"
    CONNECTOR_NAME = "Firebase"
    AUTH_TYPE = "service_account"

    REQUIRED_CONFIG_KEYS: List[str] = ["service_account_json"]

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
    ):
        super().__init__(tenant_id, connector_id, config)

        # Parse the service-account JSON lazily so __init__ never raises;
        # install() converts an unparseable SA into a clean MISSING_CREDENTIALS.
        raw_sa = self.config.get("service_account_json")
        self._service_account: Dict[str, Any] = {}
        self._sa_parse_error: Optional[str] = None
        if raw_sa:
            try:
                self._service_account = parse_service_account_json(raw_sa)
            except FirebaseAuthError as exc:
                self._sa_parse_error = str(exc)
                logger.warning(
                    "firebase.init.invalid_service_account",
                    connector_id=self.connector_id,
                    error=str(exc),
                )

        # project_id derives from the SA JSON — never a separate field.
        self.project_id: str = self._service_account.get("project_id", "") or ""
        self.client_email: str = self._service_account.get("client_email", "") or ""
        self.database_url: str = self.config.get("database_url", "") or ""
        self.storage_bucket: str = self.config.get("storage_bucket", "") or ""

        self.http_client = FirebaseHTTPClient(
            service_account=self._service_account,
            database_url=self.database_url,
            storage_bucket=self.storage_bucket,
        )

    # ── BaseConnector lifecycle ─────────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate the service-account JSON and mint an initial access token.

        Per CONNECTOR_SYSTEM_PROMPT install() returns a clean ConnectorStatus
        on success — it does not raise. We mint a token here (a single
        OAuth2 round-trip) to prove the credential is live, so a bad
        private key surfaces immediately rather than during the first
        Firestore call.
        """
        if self._sa_parse_error:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"service_account_json invalid: {self._sa_parse_error}",
            )
        if not self._service_account:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="service_account_json is required",
            )

        try:
            access_token = await self.http_client.get_access_token()
        except FirebaseAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"service-account JWT exchange failed: {exc}",
            )
        except FirebaseNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"network error during JWT exchange: {exc}",
            )

        expires_at = self.http_client.cached_token_expires_at or datetime.now(
            timezone.utc
        )
        token_info = TokenInfo(
            access_token=access_token,
            refresh_token=None,
            expires_at=expires_at,
            token_type="Bearer",
            scopes=[],
        )
        await self.set_token(token_info)

        await self.save_config(
            {
                "project_id": self.project_id,
                "client_email": self.client_email,
                "database_url": self.database_url,
                "storage_bucket": self.storage_bucket or self.http_client.storage_bucket,
            }
        )

        logger.info(
            "firebase.install.ok",
            connector_id=self.connector_id,
            project_id=self.project_id,
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Firebase connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """Service-account auth — no interactive OAuth code exchange.

        Returns a TokenInfo wrapping the cached access token so the platform
        surface can verify the credential is live.
        """
        access_token = await self.http_client.get_access_token()
        return TokenInfo(
            access_token=access_token,
            refresh_token=None,
            expires_at=self.http_client.cached_token_expires_at,
            token_type="Bearer",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Firebase reachability and credential validity.

        We mint/refresh the access token then probe a sentinel Firestore
        collection. A 404 on the probe is treated as success — only auth or
        network failures degrade health.
        """
        try:
            await self.http_client.get_access_token()
            try:
                await self.http_client.firestore_list_documents(
                    "__shielva_health__", page_size=1
                )
            except FirebaseNotFoundError:
                pass
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Firebase reachable",
            )
        except FirebaseAuthError as exc:
            health, auth = self._classify(exc.status_code or 401)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=health,
                auth_status=auth,
                message=str(exc),
            )
        except FirebaseNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except FirebaseError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    def _classify(self, status_code: int) -> tuple:
        mapping = self._STATUS_MAP.get(status_code)
        if not mapping:
            return ConnectorHealth.DEGRADED, AuthStatus.CONNECTED
        health_name, auth_name = mapping
        return ConnectorHealth[health_name], AuthStatus[auth_name]

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Sync Firestore collections + Identity Toolkit users into the KB.

        `config.sync_collections` is read as a list of Firestore collection
        names — for each, list_documents is paged and every doc is
        normalized + ingested. When the key is absent, the connector emits
        a COMPLETED-empty result so the platform's scheduler can call
        `sync()` safely without surprising errors.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        errors: List[str] = []

        sync_collections: List[str] = list(
            self.config.get("sync_collections", []) or []
        )
        sync_auth_users: bool = bool(self.config.get("sync_auth_users", False))

        if not sync_collections and not sync_auth_users:
            return SyncResult(
                status=SyncStatus.COMPLETED,
                connector_id=self.connector_id,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message=(
                    "No sync_collections / sync_auth_users configured — call "
                    "list_documents or list_users directly for the surfaces you want indexed."
                ),
            )

        try:
            for collection in sync_collections:
                page_token: Optional[str] = None
                while True:
                    resp = await with_retry(
                        lambda pt=page_token, c=collection: self.http_client.firestore_list_documents(
                            c, page_size=100, page_token=pt
                        ),
                    )
                    docs = resp.get("documents", []) or []
                    for raw in docs:
                        documents_found += 1
                        try:
                            doc = normalize_firestore_document(
                                raw,
                                tenant_id=self.tenant_id,
                                connector_id=self.connector_id,
                                collection=collection,
                            )
                            await self.ingest_document(
                                doc, kb_id=kb_id or "", webhook_url=webhook_url
                            )
                            documents_synced += 1
                        except Exception as exc:  # noqa: BLE001
                            documents_failed += 1
                            errors.append(str(exc))
                            logger.error(
                                "firebase.sync.document_failed",
                                error=str(exc),
                                collection=collection,
                            )
                    page_token = resp.get("nextPageToken")
                    if not page_token:
                        break

            if sync_auth_users:
                next_page_token: Optional[str] = None
                while True:
                    resp = await with_retry(
                        lambda pt=next_page_token: self.http_client.auth_list_users(
                            max_results=1000, next_page_token=pt
                        ),
                    )
                    users = resp.get("users", []) or []
                    for raw in users:
                        documents_found += 1
                        try:
                            doc = normalize_auth_user(
                                raw,
                                tenant_id=self.tenant_id,
                                connector_id=self.connector_id,
                            )
                            await self.ingest_document(
                                doc, kb_id=kb_id or "", webhook_url=webhook_url
                            )
                            documents_synced += 1
                        except Exception as exc:  # noqa: BLE001
                            documents_failed += 1
                            errors.append(str(exc))
                            logger.error(
                                "firebase.sync.user_failed", error=str(exc)
                            )
                    next_page_token = resp.get("nextPageToken")
                    if not next_page_token:
                        break

            status = (
                SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL
            )
            return SyncResult(
                status=status,
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                errors=errors,
                message=f"Synced {documents_synced}/{documents_found} Firebase documents",
            )
        except FirebaseError as exc:
            logger.error(
                "firebase.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                errors=errors + [str(exc)],
                message=str(exc),
            )

    # ── Firestore documents ────────────────────────────────────────────────

    async def list_documents(
        self,
        collection: str,
        *,
        page_size: int = 100,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/projects/{id}/databases/(default)/documents/{collection}."""
        return await with_retry(
            lambda: self.http_client.firestore_list_documents(
                collection, page_size=page_size, page_token=page_token
            ),
        )

    async def get_document(
        self, collection: str, document_id: str
    ) -> Dict[str, Any]:
        """GET /documents/{collection}/{document_id}."""
        return await with_retry(
            lambda: self.http_client.firestore_get_document(collection, document_id),
        )

    async def create_document(
        self,
        collection: str,
        fields: Dict[str, Any],
        document_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /documents/{collection} — optional `documentId` query param."""
        return await self.http_client.firestore_create_document(
            collection, fields, document_id=document_id
        )

    async def update_document(
        self,
        collection: str,
        document_id: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /documents/{collection}/{document_id}."""
        return await self.http_client.firestore_update_document(
            collection, document_id, fields
        )

    async def delete_document(
        self, collection: str, document_id: str
    ) -> Dict[str, Any]:
        """DELETE /documents/{collection}/{document_id}."""
        return await self.http_client.firestore_delete_document(
            collection, document_id
        )

    # ── Identity Toolkit (Firebase Auth admin) ─────────────────────────────

    async def list_users(
        self,
        *,
        page_size: int = 1000,
        next_page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST {identity_base}/accounts:batchGet — paginated."""
        return await with_retry(
            lambda: self.http_client.auth_list_users(
                max_results=page_size, next_page_token=next_page_token
            ),
        )

    async def get_user(self, uid: str) -> Dict[str, Any]:
        """POST {identity_base}/accounts:lookup with {localId: [uid]}."""
        return await with_retry(
            lambda: self.http_client.auth_lookup_user(uid),
        )

    async def create_user(
        self,
        email: str,
        password: Optional[str] = None,
        display_name: Optional[str] = None,
        phone_number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST https://identitytoolkit.googleapis.com/v1/accounts."""
        body = CreateUserRequest(
            email=email,
            password=password,
            display_name=display_name,
            phone_number=phone_number,
        ).model_dump(by_alias=True, exclude_none=True)
        return await self.http_client.auth_create_user(body)

    async def update_user(
        self,
        uid: str,
        *,
        email: Optional[str] = None,
        password: Optional[str] = None,
        display_name: Optional[str] = None,
        phone_number: Optional[str] = None,
        disabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """POST {identity_base}/accounts:update — partial update by localId."""
        body = UpdateUserRequest(
            local_id=uid,
            email=email,
            password=password,
            display_name=display_name,
            phone_number=phone_number,
            disabled=disabled,
        ).model_dump(by_alias=True, exclude_none=True)
        return await self.http_client.auth_update_user(body)

    async def delete_user(self, uid: str) -> Dict[str, Any]:
        """POST {identity_base}/accounts:delete with {localId: uid}."""
        return await self.http_client.auth_delete_user(uid)

    # ── Cloud Messaging (FCM v1) ───────────────────────────────────────────

    async def send_fcm_notification(
        self,
        token: Optional[str] = None,
        topic: Optional[str] = None,
        notification: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, str]] = None,
        android: Optional[Dict[str, Any]] = None,
        apns: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /v1/projects/{id}/messages:send.

        Exactly one of `token` or `topic` must be provided.
        """
        if not token and not topic:
            raise ValueError("send_fcm_notification requires either token or topic")
        if token and topic:
            raise ValueError("send_fcm_notification accepts token OR topic, not both")
        payload = FCMMessage(
            token=token,
            topic=topic,
            notification=notification,
            data=data,
            android=android,
            apns=apns,
        ).to_payload()
        return await with_retry(lambda: self.http_client.fcm_send(payload))

    # ── Realtime Database ──────────────────────────────────────────────────

    async def get_realtime_db(self, path: str) -> Any:
        """GET {database_url}/{path}.json."""
        return await with_retry(lambda: self.http_client.rtdb_get(path))

    async def set_realtime_db(self, path: str, data: Any) -> Any:
        """PUT {database_url}/{path}.json — full overwrite at path."""
        return await self.http_client.rtdb_set(path, data)

    # ── Cloud Storage for Firebase ─────────────────────────────────────────

    async def list_storage_objects(
        self,
        *,
        bucket: Optional[str] = None,
        prefix: Optional[str] = None,
        page_size: int = 100,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET https://firebasestorage.googleapis.com/v0/b/{bucket}/o."""
        return await with_retry(
            lambda: self.http_client.storage_list_objects(
                bucket=bucket,
                prefix=prefix,
                page_size=page_size,
                page_token=page_token,
            ),
        )

    async def upload_storage_object(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        bucket: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST https://firebasestorage.googleapis.com/v0/b/{bucket}/o?name={name}."""
        return await self.http_client.storage_upload_object(
            name, data, content_type=content_type, bucket=bucket
        )
