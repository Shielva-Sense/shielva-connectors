"""Mailchimp connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

try:
    from shielva_connectors.base import BaseConnector  # type: ignore[import]
    _BASE = BaseConnector
    _HAS_SDK = True
except ImportError:
    try:
        from shared.base_connector import (  # type: ignore[import]
            AuthStatus,
            BaseConnector,
            ConnectorHealth,
            ConnectorStatus,
            NormalizedDocument,
            SyncResult,
            SyncStatus,
        )
        _BASE = BaseConnector
        _HAS_SDK = True
    except ImportError:
        class BaseConnector:  # type: ignore[no-redef]
            def __init__(
                self,
                tenant_id: str = "",
                connector_id: str = "",
                config: Optional[Dict[str, Any]] = None,
            ) -> None:
                self.tenant_id = tenant_id
                self.connector_id = connector_id
                self.config = config or {}
        _BASE = BaseConnector  # type: ignore[assignment,misc]
        _HAS_SDK = False

from client.http_client import MailchimpHTTPClient
from exceptions import MailchimpAuthError, MailchimpError, MailchimpNetworkError
from helpers.utils import (
    extract_dc_from_api_key,
    get_subscriber_hash,
    normalize_campaign,
    normalize_member,
    with_retry,
)
from models import (
    AuthStatus as _LocalAuthStatus,
    ConnectorHealth as _LocalConnectorHealth,
    ConnectorDocument,
    InstallResult,
    HealthCheckResult,
    SyncResult as _LocalSyncResult,
    SyncStatus as _LocalSyncStatus,
)

logger = structlog.get_logger(__name__)

_DEFAULT_COUNT = 100


class MailchimpConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for Mailchimp via the Marketing API v3."""

    CONNECTOR_TYPE = "mailchimp"
    CONNECTOR_NAME = "Mailchimp"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS = ["api_key"]

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        if _HAS_SDK:
            super().__init__(tenant_id, connector_id, cfg)
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = cfg
        self._http_client: Optional[MailchimpHTTPClient] = None

    def _get_api_key(self) -> str:
        return self.config.get("api_key", "")

    def _get_dc(self) -> str:
        return extract_dc_from_api_key(self._get_api_key())

    def _ensure_client(self) -> MailchimpHTTPClient:
        if self._http_client is None:
            api_key = self._get_api_key()
            dc = extract_dc_from_api_key(api_key)
            self._http_client = MailchimpHTTPClient(dc=dc, api_key=api_key)
        return self._http_client

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        """Validate api_key presence and extract dc suffix.

        Does not make network calls — token validity is confirmed at health_check().
        """
        api_key = self._get_api_key()

        if not api_key:
            logger.warning("mailchimp.install.missing_credentials", connector_id=self.connector_id)
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,  # type: ignore[name-defined]
                    auth_status=AuthStatus.MISSING_CREDENTIALS,  # type: ignore[name-defined]
                    message="api_key is required",
                )
            return InstallResult(
                health=_LocalConnectorHealth.OFFLINE,
                auth_status=_LocalAuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="api_key is required",
            )

        dc = extract_dc_from_api_key(api_key)
        msg = f"Connector installed — API key present, data center: {dc or 'unknown'}"
        logger.info("mailchimp.install.ok", connector_id=self.connector_id, dc=dc)

        if _HAS_SDK:
            return ConnectorStatus(  # type: ignore[name-defined]
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                message=msg,
            )
        return InstallResult(
            health=_LocalConnectorHealth.HEALTHY,
            auth_status=_LocalAuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message=msg,
        )

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        """GET / — verify credentials and return account name."""
        try:
            data = await with_retry(
                lambda: self._ensure_client().get_root(),
                max_attempts=2,
            )
            account_name = data.get("account_name", "unknown account")
            msg = f"Connected to Mailchimp account: {account_name}"

            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                    auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                    message=msg,
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.HEALTHY,
                auth_status=_LocalAuthStatus.CONNECTED,
                message=msg,
            )
        except MailchimpAuthError as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.INVALID_CREDENTIALS,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.FAILED,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.FAILED,
                message=str(exc),
            )

    # ── sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: Optional[Any] = None,
        kb_id: str = "",
    ) -> Any:
        """Sync all audiences and their members.

        Fetches all lists/audiences, then for each list fetches all members
        and normalizes them into ConnectorDocuments.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            audiences = await self.list_audiences()

            for audience in audiences:
                list_id = audience.get("id", "")
                list_name = audience.get("name", list_id)
                if not list_id:
                    continue

                try:
                    members = await self.list_members(list_id=list_id)
                    documents_found += len(members)

                    for member in members:
                        try:
                            doc = normalize_member(
                                member,
                                list_id,
                                list_name,
                                self.connector_id,
                                self.tenant_id,
                            )
                            if _HAS_SDK:
                                normalized = NormalizedDocument(  # type: ignore[name-defined]
                                    id=doc.id,
                                    source_id=member.get("unique_email_id", doc.id),
                                    title=doc.title,
                                    content=doc.content,
                                    content_type="text",
                                    source_url="",
                                    author=member.get("email_address", ""),
                                    source="mailchimp",
                                    tenant_id=self.tenant_id,
                                    connector_id=self.connector_id,
                                    metadata=doc.metadata,
                                )
                                await self.ingest_document(normalized, kb_id=kb_id or "")
                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "mailchimp.sync.member_failed",
                                list_id=list_id,
                                email=member.get("email_address", ""),
                                error=str(exc),
                            )
                            documents_failed += 1

                except MailchimpAuthError:
                    raise
                except Exception as exc:
                    logger.error(
                        "mailchimp.sync.list_failed",
                        list_id=list_id,
                        error=str(exc),
                    )
                    documents_failed += 1

            status = _LocalSyncStatus.COMPLETED if documents_failed == 0 else _LocalSyncStatus.PARTIAL
            msg = f"Synced {documents_synced}/{documents_found} members from {len(audiences)} audiences"

            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=msg,
                )
            return _LocalSyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=msg,
            )

        except Exception as exc:
            logger.error("mailchimp.sync.failed", error=str(exc), connector_id=self.connector_id)
            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.FAILED,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=str(exc),
                )
            return _LocalSyncResult(
                status=_LocalSyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── convenience methods ───────────────────────────────────────────────────

    async def list_audiences(self, count: int = _DEFAULT_COUNT) -> List[Dict[str, Any]]:
        """List all audiences/lists via paginated GET /lists."""
        audiences: List[Dict[str, Any]] = []
        offset = 0

        while True:
            resp = await with_retry(
                lambda o=offset: self._ensure_client().get_lists(count=count, offset=o),
                max_attempts=3,
            )
            batch = resp.get("lists", [])
            audiences.extend(batch)
            total = resp.get("total_items", 0)
            offset += len(batch)
            if offset >= total or not batch:
                break

        return audiences

    async def get_audience(self, list_id: str) -> Dict[str, Any]:
        """GET /lists/{list_id} — fetch a single audience by ID."""
        return await with_retry(
            lambda: self._ensure_client().get_list(list_id),
            max_attempts=3,
        )

    async def list_members(
        self, list_id: str, count: int = _DEFAULT_COUNT
    ) -> List[Dict[str, Any]]:
        """List all members of an audience via paginated GET /lists/{list_id}/members."""
        members: List[Dict[str, Any]] = []
        offset = 0

        while True:
            resp = await with_retry(
                lambda o=offset: self._ensure_client().get_members(
                    list_id, count=count, offset=o
                ),
                max_attempts=3,
            )
            batch = resp.get("members", [])
            members.extend(batch)
            total = resp.get("total_items", 0)
            offset += len(batch)
            if offset >= total or not batch:
                break

        return members

    async def get_member(self, list_id: str, subscriber_hash: str) -> Dict[str, Any]:
        """GET /lists/{list_id}/members/{subscriber_hash} — fetch a single member."""
        return await with_retry(
            lambda: self._ensure_client().get_member(list_id, subscriber_hash),
            max_attempts=3,
        )

    async def list_campaigns(
        self,
        count: int = _DEFAULT_COUNT,
        status: Optional[str] = None,
        type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List all campaigns via paginated GET /campaigns."""
        campaigns: List[Dict[str, Any]] = []
        offset = 0

        while True:
            resp = await with_retry(
                lambda o=offset: self._ensure_client().get_campaigns(
                    count=count, offset=o, status=status, type=type
                ),
                max_attempts=3,
            )
            batch = resp.get("campaigns", [])
            campaigns.extend(batch)
            total = resp.get("total_items", 0)
            offset += len(batch)
            if offset >= total or not batch:
                break

        return campaigns

    async def get_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """GET /campaigns/{campaign_id} — fetch a single campaign by ID."""
        return await with_retry(
            lambda: self._ensure_client().get_campaign(campaign_id),
            max_attempts=3,
        )

    async def list_automations(
        self, count: int = _DEFAULT_COUNT
    ) -> List[Dict[str, Any]]:
        """List all classic automations via paginated GET /automations."""
        automations: List[Dict[str, Any]] = []
        offset = 0

        while True:
            resp = await with_retry(
                lambda o=offset: self._ensure_client().list_automations(
                    count=count, offset=o
                ),
                max_attempts=3,
            )
            batch = resp.get("automations", [])
            automations.extend(batch)
            total = resp.get("total_items", 0)
            offset += len(batch)
            if offset >= total or not batch:
                break

        return automations

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> "MailchimpConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
