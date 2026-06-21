"""OneSignal connector — orchestration only.

All HTTP calls   → client/http_client.py
Normalization    → helpers/normalizer.py
Utilities        → helpers/utils.py
Errors           → exceptions.py

Authentication model
--------------------
OneSignal exposes two distinct API keys:

* **REST API Key** — per-app. Used to send notifications, manage players,
  segments, templates, and inspect notification status for THAT app.
* **User Auth Key** — account-wide. Required for ``/apps`` management
  endpoints (``list_apps``, ``create_app``, ``update_app``).

Both keys are sent as ``Authorization: Basic <key>`` — the literal string
``Basic `` followed by the RAW key (NOT base64-encoded). This is a
OneSignal-specific quirk; see ``client/http_client.py::_auth_headers``.

``install()`` requires ``app_id`` and ``rest_api_key``. ``user_auth_key`` is
optional — it only unlocks ``/apps`` endpoints. Methods that need it raise a
clear ``OneSignalAuthError`` when it is not configured.
"""
from __future__ import annotations

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

from client.http_client import ONESIGNAL_BASE, OneSignalHTTPClient
from exceptions import (
    OneSignalAuthError,
    OneSignalError,
    OneSignalNetworkError,
    OneSignalNotFoundError,
)
from helpers.normalizer import (
    normalize_app,
    normalize_notification,
    normalize_player,
)
from helpers.utils import build_notification_payload, prune_none, with_retry

logger = structlog.get_logger(__name__)


class OneSignalConnector(BaseConnector):
    """Shielva connector for OneSignal push / email / SMS notifications."""

    CONNECTOR_TYPE: str = "onesignal"
    CONNECTOR_NAME: str = "OneSignal"
    AUTH_TYPE: str = "api_key"

    # Required config keys for install() — must all be present and non-empty.
    REQUIRED_CONFIG_KEYS: List[str] = ["app_id", "rest_api_key"]

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
        # ALWAYS read credentials from self.config — NEVER from os.environ.
        self.app_id: str = self.config.get("app_id", "") or ""
        self.rest_api_key: str = self.config.get("rest_api_key", "") or ""
        self.user_auth_key: str = self.config.get("user_auth_key", "") or ""
        self.base_url: str = self.config.get("base_url", ONESIGNAL_BASE) or ONESIGNAL_BASE
        try:
            self.timeout_s: float = float(self.config.get("timeout_s", 30.0))
        except (TypeError, ValueError):
            self.timeout_s = 30.0

        self.http_client: OneSignalHTTPClient = OneSignalHTTPClient(
            base_url=self.base_url,
            timeout_s=self.timeout_s,
        )

    # ── Internal helpers ───────────────────────────────────────────────────

    def _require_rest_key(self) -> str:
        if not self.rest_api_key:
            raise OneSignalAuthError(
                "rest_api_key is not configured — install the connector first",
            )
        return self.rest_api_key

    def _require_user_key(self) -> str:
        if not self.user_auth_key:
            raise OneSignalAuthError(
                "user_auth_key is required for /apps endpoints — set it in connector config",
            )
        return self.user_auth_key

    def _resolve_app_id(self, app_id: Optional[str]) -> str:
        candidate = app_id or self.app_id
        if not candidate:
            raise OneSignalError(
                "app_id is required and no default app_id is configured",
            )
        return candidate

    # ── BaseConnector lifecycle ─────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate required config keys.

        Per CONNECTOR_SYSTEM_PROMPT: install() MUST NOT call health_check or
        any API endpoint. The gateway calls health_check separately.
        """
        missing = [k for k in self.REQUIRED_CONFIG_KEYS if not self.config.get(k)]
        if missing:
            logger.warning(
                "onesignal.install.missing_credentials",
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
                missing=missing,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required config: {', '.join(missing)}",
            )

        await self.save_config(
            {
                "app_id": self.app_id,
                "rest_api_key": self.rest_api_key,
                "user_auth_key": self.user_auth_key,
                "base_url": self.base_url,
                "timeout_s": self.timeout_s,
            },
        )
        logger.info(
            "onesignal.install.ok",
            tenant_id=self.tenant_id,
            connector_id=self.connector_id,
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="OneSignal connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured rest_api_key.
        """
        return TokenInfo(
            access_token=self.rest_api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify OneSignal API connectivity.

        Probes ``GET /apps/{app_id}`` with the REST API key — minimal payload,
        always available once the connector is installed.
        """
        try:
            app_id = self._resolve_app_id(None)
            rest_key = self._require_rest_key()
            await with_retry(
                lambda: self.http_client.get_app(rest_key, app_id),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="OneSignal API reachable",
            )
        except OneSignalAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE if exc.status_code == 401 else ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.TOKEN_EXPIRED if exc.status_code == 401 else AuthStatus.INVALID_CREDENTIALS,
                message=f"OneSignal auth failed: {exc}",
            )
        except OneSignalNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"OneSignal network error: {exc}",
            )
        except OneSignalError as exc:
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
        """Page recent notifications + apps → NormalizedDocument → ingest.

        OneSignal is primarily a push-out channel, but the configured app's
        notification history is useful corpus for delivery analysis. We list
        recent notifications and (when available) the app metadata, normalize
        them, and call ``ingest_document``.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            app_id = self._resolve_app_id(None)
            rest_key = self._require_rest_key()

            # App record
            try:
                app_raw = await with_retry(
                    lambda: self.http_client.get_app(rest_key, app_id),
                    max_retries=3,
                )
                if app_raw:
                    documents_found += 1
                    try:
                        doc = normalize_app(app_raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("onesignal.sync.app_failed", error=str(exc))
                        documents_failed += 1
            except OneSignalError as exc:
                logger.warning("onesignal.sync.app_fetch_failed", error=str(exc))

            # Recent notifications
            notif_resp = await with_retry(
                lambda: self.http_client.list_notifications(
                    rest_key, app_id=app_id, limit=50, offset=0,
                ),
                max_retries=3,
            )
            for raw in (notif_resp.get("notifications") or []):
                documents_found += 1
                try:
                    doc = normalize_notification(
                        raw, self.connector_id, self.tenant_id,
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("onesignal.sync.notification_failed", error=str(exc))
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} OneSignal documents",
            )
        except Exception as exc:
            logger.error(
                "onesignal.sync.failed",
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

    # ── Apps (account-scope — require user_auth_key) ──────────────────────

    async def list_apps(self) -> List[Dict[str, Any]]:
        """GET /apps — list every app in the OneSignal account."""
        key = self._require_user_key()
        return await with_retry(
            lambda: self.http_client.list_apps(key),
            max_retries=3,
        )

    async def get_app(self, app_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /apps/{id}.

        Uses ``user_auth_key`` when set (canonical per OneSignal docs), falling
        back to ``rest_api_key`` for the connector's own app — which is what
        ``health_check()`` relies on.
        """
        resolved = self._resolve_app_id(app_id)
        key = self.user_auth_key or self._require_rest_key()
        return await with_retry(
            lambda: self.http_client.get_app(key, resolved),
            max_retries=3,
        )

    async def create_app(
        self,
        name: str,
        gcm_key: Optional[str] = None,
        apns_env: Optional[str] = None,
        apns_p12: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /apps."""
        key = self._require_user_key()
        payload = prune_none(
            {
                "name": name,
                "gcm_key": gcm_key,
                "apns_env": apns_env,
                "apns_p12": apns_p12,
            },
        )
        return await self.http_client.create_app(key, payload)

    async def update_app(self, app_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /apps/{id}."""
        key = self._require_user_key()
        return await self.http_client.update_app(key, app_id, fields)

    # ── Notifications ──────────────────────────────────────────────────────

    async def send_notification(
        self,
        contents: Dict[str, Any],
        *,
        app_id: Optional[str] = None,
        headings: Optional[Dict[str, Any]] = None,
        included_segments: Optional[List[str]] = None,
        excluded_segments: Optional[List[str]] = None,
        include_player_ids: Optional[List[str]] = None,
        include_external_user_ids: Optional[List[str]] = None,
        data: Optional[Dict[str, Any]] = None,
        url: Optional[str] = None,
        big_picture: Optional[str] = None,
        send_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /notifications."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        payload = build_notification_payload(
            app_id=resolved_app_id,
            contents=contents,
            headings=headings,
            included_segments=included_segments,
            excluded_segments=excluded_segments,
            include_player_ids=include_player_ids,
            include_external_user_ids=include_external_user_ids,
            data=data,
            url=url,
            big_picture=big_picture,
            send_after=send_after,
        )
        return await self.http_client.send_notification(rest_key, payload)

    async def cancel_notification(
        self, notification_id: str, app_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """DELETE /notifications/{id}?app_id=..."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        return await self.http_client.cancel_notification(
            rest_key, resolved_app_id, notification_id,
        )

    async def get_notification(
        self, notification_id: str, app_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /notifications/{id}?app_id=..."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        return await with_retry(
            lambda: self.http_client.get_notification(
                rest_key, resolved_app_id, notification_id,
            ),
            max_retries=3,
        )

    async def list_notifications(
        self,
        app_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        kind: Optional[int] = None,
    ) -> Dict[str, Any]:
        """GET /notifications?app_id=..."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        return await with_retry(
            lambda: self.http_client.list_notifications(
                rest_key,
                app_id=resolved_app_id,
                limit=limit,
                offset=offset,
                kind=kind,
            ),
            max_retries=3,
        )

    async def notification_history(
        self,
        notification_id: str,
        events: str = "sent",
        app_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /notifications/{id}/history."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        return await self.http_client.notification_history(
            rest_key,
            notification_id=notification_id,
            app_id=resolved_app_id,
            events=events,
        )

    # ── Devices (Players) ──────────────────────────────────────────────────

    async def list_devices(
        self,
        app_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /players?app_id=..."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        return await with_retry(
            lambda: self.http_client.list_devices(
                rest_key, app_id=resolved_app_id, limit=limit, offset=offset,
            ),
            max_retries=3,
        )

    async def get_device(
        self, player_id: str, app_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /players/{id}?app_id=..."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        return await with_retry(
            lambda: self.http_client.get_device(
                rest_key, resolved_app_id, player_id,
            ),
            max_retries=3,
        )

    async def create_device(
        self,
        device_type: int,
        *,
        app_id: Optional[str] = None,
        identifier: Optional[str] = None,
        language: Optional[str] = None,
        tags: Optional[Dict[str, Any]] = None,
        external_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /players."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        payload = prune_none(
            {
                "app_id": resolved_app_id,
                "device_type": device_type,
                "identifier": identifier,
                "language": language,
                "tags": tags,
                "external_user_id": external_user_id,
            },
        )
        return await self.http_client.create_device(rest_key, payload)

    async def update_device(
        self,
        player_id: str,
        fields: Dict[str, Any],
        app_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PUT /players/{id}.

        ``app_id`` is always injected into the body — OneSignal requires it
        for player updates.
        """
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        payload = dict(fields)
        payload.setdefault("app_id", resolved_app_id)
        return await self.http_client.update_device(rest_key, player_id, payload)

    # ── Segments ───────────────────────────────────────────────────────────

    async def list_segments(self, app_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /apps/{id}/segments."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        return await with_retry(
            lambda: self.http_client.list_segments(rest_key, resolved_app_id),
            max_retries=3,
        )

    async def create_segment(
        self,
        name: str,
        filters: List[Dict[str, Any]],
        app_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /apps/{id}/segments."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        payload = {"name": name, "filters": filters}
        return await self.http_client.create_segment(
            rest_key, resolved_app_id, payload,
        )

    async def delete_segment(
        self, segment_id: str, app_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """DELETE /apps/{id}/segments/{segment_id}."""
        rest_key = self._require_rest_key()
        resolved_app_id = self._resolve_app_id(app_id)
        return await self.http_client.delete_segment(
            rest_key, resolved_app_id, segment_id,
        )
