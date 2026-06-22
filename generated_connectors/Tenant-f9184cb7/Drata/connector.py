"""Drata connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Bearer API key. Required header:
    Authorization: Bearer <api_key>
    Content-Type:  application/json
    Accept:        application/json
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

from client.http_client import DrataHTTPClient
from exceptions import (
    DrataAuthError,
    DrataError,
    DrataNetworkError,
    DrataNotFound,
)
from helpers.normalizer import (
    normalize_control,
    normalize_evidence,
    normalize_personnel,
    normalize_risk,
    normalize_vendor,
)
from helpers.utils import coerce_items, with_retry

logger = structlog.get_logger(__name__)

_DRATA_BASE = "https://public-api.drata.com"


class DrataConnector(BaseConnector):
    """Shielva connector for the Drata public REST API (compliance automation)."""

    CONNECTOR_TYPE = "drata"
    CONNECTOR_NAME = "Drata"
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
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_key: str = self.config.get("api_key", "")
        self.base_url: str = self.config.get("base_url", "") or _DRATA_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = DrataHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Drata API-key install only requires `api_key`. The `base_url` is
        optional and falls back to the public-api default.
        """
        api_key = self.config.get("api_key")

        if not api_key:
            logger.warning(
                "drata.install.missing_credentials",
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
                "base_url": self.config.get("base_url", _DRATA_BASE),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 60),
            }
        )
        logger.info("drata.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Drata connector installed",
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
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Drata API connectivity by listing one personnel record."""
        try:
            await with_retry(
                lambda: self.http_client.list_personnel(limit=1, offset=0),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Drata API reachable",
            )
        except DrataAuthError as exc:
            status = getattr(exc, "status_code", 401)
            if status == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Drata auth forbidden: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Drata auth failed: {exc}",
            )
        except DrataNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Drata network error: {exc}",
            )
        except DrataError as exc:
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
        """Sync Drata personnel + controls + evidence + risks + vendors into the Shielva KB."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        normalizers = [
            (self.http_client.list_personnel, normalize_personnel, "personnel"),
            (self.http_client.list_controls, normalize_control, "control"),
            (self.http_client.list_evidence, normalize_evidence, "evidence"),
            (self.http_client.list_risks, normalize_risk, "risk"),
            (self.http_client.list_vendors, normalize_vendor, "vendor"),
        ]

        try:
            for lister, normalizer, kind in normalizers:
                resp = await with_retry(
                    lambda l=lister: l(limit=100, offset=0),
                    max_retries=3,
                )
                for raw in coerce_items(resp):
                    documents_found += 1
                    try:
                        doc = normalizer(raw, self.tenant_id, self.connector_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "drata.sync.item_failed",
                            kind=kind,
                            error=str(exc),
                        )
                        documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Drata documents",
            )
        except Exception as exc:
            logger.error("drata.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def list_personnel(
        self,
        limit: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /personnel — list personnel."""
        return await with_retry(
            lambda: self.http_client.list_personnel(
                limit=limit, offset=offset, status=status,
            ),
            max_retries=3,
        )

    async def get_personnel(self, personnel_id: str) -> Dict[str, Any]:
        """GET /personnel/{id}."""
        return await with_retry(
            lambda: self.http_client.get_personnel(personnel_id),
            max_retries=3,
        )

    async def list_controls(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /controls — list controls."""
        return await with_retry(
            lambda: self.http_client.list_controls(limit=limit, offset=offset),
            max_retries=3,
        )

    async def get_control(self, control_id: str) -> Dict[str, Any]:
        """GET /controls/{id}."""
        return await with_retry(
            lambda: self.http_client.get_control(control_id),
            max_retries=3,
        )

    async def list_evidence(
        self,
        limit: int = 100,
        offset: int = 0,
        control_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /evidence — list collected evidence items."""
        return await with_retry(
            lambda: self.http_client.list_evidence(
                limit=limit, offset=offset, control_id=control_id,
            ),
            max_retries=3,
        )

    async def list_risks(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /risks — list risk register entries."""
        return await with_retry(
            lambda: self.http_client.list_risks(limit=limit, offset=offset),
            max_retries=3,
        )

    async def list_vendors(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /vendors — list tracked vendors."""
        return await with_retry(
            lambda: self.http_client.list_vendors(limit=limit, offset=offset),
            max_retries=3,
        )

    async def get_vendor(self, vendor_id: str) -> Dict[str, Any]:
        """GET /vendors/{id}."""
        return await with_retry(
            lambda: self.http_client.get_vendor(vendor_id),
            max_retries=3,
        )

    async def list_audits(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /audits — list audit instances."""
        return await with_retry(
            lambda: self.http_client.list_audits(limit=limit, offset=offset),
            max_retries=3,
        )

    async def list_policies(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /policies — list policy documents."""
        return await with_retry(
            lambda: self.http_client.list_policies(limit=limit, offset=offset),
            max_retries=3,
        )

    async def list_devices(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /devices — list enrolled / monitored endpoints."""
        return await with_retry(
            lambda: self.http_client.list_devices(limit=limit, offset=offset),
            max_retries=3,
        )

    async def list_frameworks(self) -> Dict[str, Any]:
        """GET /frameworks — list active compliance frameworks."""
        return await with_retry(
            lambda: self.http_client.list_frameworks(),
            max_retries=3,
        )
