"""Vanta connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: OAuth 2.0 client_credentials. POST {token_url} mints a short-lived
access token; subsequent requests carry `Authorization: Bearer <token>`.
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

from client.http_client import VantaHTTPClient
from exceptions import (
    VantaAuthError,
    VantaError,
    VantaNetworkError,
    VantaNotFound,
)
from helpers.normalizer import (
    normalize_control,
    normalize_finding,
    normalize_framework,
    normalize_personnel,
    normalize_vendor,
)
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_VANTA_BASE_URL = "https://api.vanta.com/v1"
_VANTA_TOKEN_URL = "https://api.vanta.com/oauth/token"
_DEFAULT_SCOPES = "vanta-api.all:read vanta-api.vendors:write"


class VantaConnector(BaseConnector):
    """Shielva connector for the Vanta compliance-automation REST API."""

    CONNECTOR_TYPE = "vanta"
    CONNECTOR_NAME = "Vanta"
    AUTH_TYPE = "oauth2_client_credentials"

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
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.scopes: str = self.config.get("scopes", _DEFAULT_SCOPES) or _DEFAULT_SCOPES
        self.base_url: str = self.config.get("base_url", _VANTA_BASE_URL) or _VANTA_BASE_URL
        self.token_url: str = self.config.get("token_url", _VANTA_TOKEN_URL) or _VANTA_TOKEN_URL
        self.rate_limit_per_min: int = int(self.config.get("rate_limit_per_min", 60) or 60)

        self.http_client = VantaHTTPClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            base_url=self.base_url,
            token_url=self.token_url,
            scopes=self.scopes,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config, mint an access token, mark installed.

        Vanta OAuth2 client_credentials install requires `client_id` and
        `client_secret`. We attempt to mint the token immediately so the
        operator gets a fast green/red signal at install time.
        """
        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")

        if not client_id or not client_secret:
            logger.warning(
                "vanta.install.missing_credentials",
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
                "scopes": self.scopes,
                "base_url": self.base_url,
                "token_url": self.token_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )

        try:
            data = await self.http_client.authenticate(force=True)
        except VantaAuthError as exc:
            logger.warning(
                "vanta.install.auth_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Vanta token mint failed: {exc}",
            )
        except VantaNetworkError as exc:
            logger.error(
                "vanta.install.network_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.PENDING,
                message=f"Vanta token endpoint unreachable: {exc}",
            )

        logger.info(
            "vanta.install.ok",
            connector_id=self.connector_id,
            expires_in=int(data.get("expires_in", 0)),
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Vanta connector installed and authenticated",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """OAuth2 client_credentials grant — auth_code/state unused.

        Mints (or returns the cached) access token via the HTTP client.
        Returned for surface compatibility with the BaseConnector ABI.
        """
        data = await self.http_client.authenticate()
        return TokenInfo(
            access_token=data.get("access_token", ""),
            refresh_token=None,
            expires_at=None,
            token_type=data.get("token_type", "Bearer"),
            scopes=(data.get("scope") or self.scopes).split() if data.get("scope") or self.scopes else [],
            raw=data,
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Vanta API connectivity by probing `/frameworks?pageSize=1`."""
        try:
            await with_retry(
                lambda: self.http_client.health_probe(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Vanta API reachable",
            )
        except VantaAuthError as exc:
            status_code = getattr(exc, "status_code", 401)
            if status_code == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Vanta scope insufficient: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Vanta auth failed: {exc}",
            )
        except VantaNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Vanta network error: {exc}",
            )
        except VantaError as exc:
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
        """Sync Vanta frameworks + controls + vendors + personnel into the KB.

        Pages each surface with cursor pagination, normalises into
        NormalizedDocument, and dispatches to `ingest_document`.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            for page_fn, normalize_fn in (
                (self.http_client.list_frameworks, normalize_framework),
                (self.http_client.list_controls, normalize_control),
                (self.http_client.list_vendors, normalize_vendor),
                (self.http_client.list_personnel, normalize_personnel),
            ):
                cursor: Optional[str] = None
                while True:
                    try:
                        page = await page_fn(page_size=100, page_cursor=cursor)
                    except TypeError:
                        # list_personnel accepts includes_inactive kwarg
                        page = await page_fn(
                            page_size=100,
                            page_cursor=cursor,
                            includes_inactive=False,
                        )

                    items = (
                        page.get("results", [])
                        or page.get("data", [])
                        or []
                    )
                    documents_found += len(items)
                    for item in items:
                        try:
                            doc = normalize_fn(item, self.connector_id, self.tenant_id)
                            await self.ingest_document(
                                doc,
                                kb_id=kb_id or "",
                                webhook_url=webhook_url,
                            )
                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "vanta.sync.document_failed",
                                error=str(exc),
                                connector_id=self.connector_id,
                            )
                            documents_failed += 1

                    page_info = page.get("pageInfo") if isinstance(page.get("pageInfo"), dict) else None
                    if page_info:
                        cursor = page_info.get("endCursor") or page_info.get("nextCursor")
                    else:
                        cursor = page.get("nextPageCursor")
                    if not cursor:
                        break

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Vanta documents",
            )
        except Exception as exc:
            logger.error(
                "vanta.sync.failed",
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

    # ── Frameworks ─────────────────────────────────────────────────────────

    async def list_frameworks(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /frameworks — list compliance frameworks."""
        return await with_retry(
            lambda: self.http_client.list_frameworks(
                page_size=page_size, page_cursor=page_cursor
            ),
            max_retries=3,
        )

    async def get_framework(self, framework_id: str) -> Dict[str, Any]:
        """GET /frameworks/{framework_id}."""
        return await with_retry(
            lambda: self.http_client.get_framework(framework_id),
            max_retries=3,
        )

    # ── Controls ───────────────────────────────────────────────────────────

    async def list_controls(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
        framework_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /controls — list controls (optionally scoped to a framework)."""
        return await with_retry(
            lambda: self.http_client.list_controls(
                page_size=page_size,
                page_cursor=page_cursor,
                framework_id=framework_id,
            ),
            max_retries=3,
        )

    async def get_control(self, control_id: str) -> Dict[str, Any]:
        """GET /controls/{control_id}."""
        return await with_retry(
            lambda: self.http_client.get_control(control_id),
            max_retries=3,
        )

    # ── Vendors ────────────────────────────────────────────────────────────

    async def list_vendors(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /vendors — list third-party vendors."""
        return await with_retry(
            lambda: self.http_client.list_vendors(
                page_size=page_size, page_cursor=page_cursor
            ),
            max_retries=3,
        )

    async def get_vendor(self, vendor_id: str) -> Dict[str, Any]:
        """GET /vendors/{vendor_id}."""
        return await with_retry(
            lambda: self.http_client.get_vendor(vendor_id),
            max_retries=3,
        )

    # ── Personnel ──────────────────────────────────────────────────────────

    async def list_personnel(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
        includes_inactive: bool = False,
    ) -> Dict[str, Any]:
        """GET /personnel — list personnel (employees + contractors)."""
        return await with_retry(
            lambda: self.http_client.list_personnel(
                page_size=page_size,
                page_cursor=page_cursor,
                includes_inactive=includes_inactive,
            ),
            max_retries=3,
        )

    async def get_personnel(self, person_id: str) -> Dict[str, Any]:
        """GET /personnel/{person_id}."""
        return await with_retry(
            lambda: self.http_client.get_personnel(person_id),
            max_retries=3,
        )

    # ── Risks ──────────────────────────────────────────────────────────────

    async def list_risks(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /risks — list risk register entries."""
        return await with_retry(
            lambda: self.http_client.list_risks(
                page_size=page_size, page_cursor=page_cursor
            ),
            max_retries=3,
        )

    # ── Incidents ──────────────────────────────────────────────────────────

    async def list_incidents(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /incidents — list security incidents."""
        return await with_retry(
            lambda: self.http_client.list_incidents(
                page_size=page_size,
                page_cursor=page_cursor,
                severity=severity,
                status=status,
            ),
            max_retries=3,
        )

    # ── Documents ──────────────────────────────────────────────────────────

    async def list_documents(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /documents — list policies and SOPs."""
        return await with_retry(
            lambda: self.http_client.list_documents(
                page_size=page_size, page_cursor=page_cursor
            ),
            max_retries=3,
        )

    # ── Tests ──────────────────────────────────────────────────────────────

    async def list_tests(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
        test_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /tests — list continuous control tests."""
        return await with_retry(
            lambda: self.http_client.list_tests(
                page_size=page_size,
                page_cursor=page_cursor,
                test_status=test_status,
            ),
            max_retries=3,
        )

    # ── Findings ───────────────────────────────────────────────────────────

    async def list_findings(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /findings — list open findings."""
        return await with_retry(
            lambda: self.http_client.list_findings(
                page_size=page_size,
                page_cursor=page_cursor,
                severity=severity,
                status=status,
            ),
            max_retries=3,
        )

    # ── Audits ─────────────────────────────────────────────────────────────

    async def list_audits(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /audits — list audit engagements."""
        return await with_retry(
            lambda: self.http_client.list_audits(
                page_size=page_size, page_cursor=page_cursor
            ),
            max_retries=3,
        )
