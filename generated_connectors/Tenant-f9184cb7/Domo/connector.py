"""Domo connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

try:
    from shared.base_connector import (
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
    try:
        from shared.base_connector import BaseConnector  # type: ignore[no-redef]
        _BASE = BaseConnector  # type: ignore[misc]
        _HAS_SDK = False
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

        _BASE = BaseConnector  # type: ignore[misc]
        _HAS_SDK = False

from client.http_client import DomoHTTPClient
from exceptions import DomoAuthError, DomoError, DomoNetworkError, DomoNotFoundError
from helpers.utils import normalize_dataset, normalize_page, normalize_user, with_retry
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

CONNECTOR_TYPE = "domo"
AUTH_TYPE = "api_key"

_DOMO_BASE = "https://api.domo.com"
_PAGE_SIZE_DATASETS = 50
_PAGE_SIZE_PAGES = 50
_PAGE_SIZE_USERS = 500


class DomoConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for Domo via the Domo REST API.

    Auth: OAuth2 client credentials — GET /oauth/token with BasicAuth(client_id, client_secret).
    All API calls use: Authorization: Bearer <access_token>
    """

    CONNECTOR_TYPE = "domo"
    CONNECTOR_NAME = "Domo"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]

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
        self.client = DomoHTTPClient(config=self.config)

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        """Validate that client_id and client_secret are present in config."""
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        missing: List[str] = []
        if not client_id:
            missing.append("client_id")
        if not client_secret:
            missing.append("client_secret")

        if missing:
            msg = f"Missing required fields: {', '.join(missing)}"
            logger.warning(
                "domo.install.missing_credentials",
                connector_id=self.connector_id,
                missing=missing,
            )
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,  # type: ignore[name-defined]
                    auth_status=AuthStatus.MISSING_CREDENTIALS,  # type: ignore[name-defined]
                    message=msg,
                )
            return InstallResult(
                health=_LocalConnectorHealth.OFFLINE,
                auth_status=_LocalAuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message=msg,
            )

        logger.info("domo.install.ok", connector_id=self.connector_id)
        if _HAS_SDK:
            return ConnectorStatus(  # type: ignore[name-defined]
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                message="Connector installed — client credentials present",
            )
        return InstallResult(
            health=_LocalConnectorHealth.HEALTHY,
            auth_status=_LocalAuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Connector installed — client credentials present",
        )

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        """Acquire an access token then verify connectivity by listing one user."""
        try:
            await with_retry(lambda: self.client.get_token(), max_attempts=2)
            users = await with_retry(
                lambda: self.client.list_users(limit=1, offset=0), max_attempts=2
            )
            user_count = len(users)
            msg = f"Connected — Domo API reachable, {user_count} user(s) visible"

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

        except DomoAuthError as exc:
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

    async def sync(self, **kwargs: Any) -> Any:
        """Sync Domo resources: datasets and dashboard pages.

        Paginates all datasets and pages, normalizes each to ConnectorDocument.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Ensure we have a valid token before sync
            await with_retry(lambda: self.client.get_token(), max_attempts=3)

            # 1. Sync datasets
            ds_found, ds_synced, ds_failed = await self._sync_datasets(kwargs)
            documents_found += ds_found
            documents_synced += ds_synced
            documents_failed += ds_failed

            # 2. Sync pages (dashboards)
            pg_found, pg_synced, pg_failed = await self._sync_pages(kwargs)
            documents_found += pg_found
            documents_synced += pg_synced
            documents_failed += pg_failed

            status = (
                _LocalSyncStatus.COMPLETED
                if documents_failed == 0
                else _LocalSyncStatus.PARTIAL
            )
            msg = (
                f"Synced {documents_synced}/{documents_found} resources "
                f"({documents_failed} failed)"
            )

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
            logger.error(
                "domo.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
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

    async def _sync_datasets(
        self, kwargs: Dict[str, Any]
    ) -> tuple[int, int, int]:
        """Paginate and sync all datasets. Returns (found, synced, failed)."""
        found = 0
        synced = 0
        failed = 0
        offset = 0

        while True:
            try:
                batch = await with_retry(
                    lambda o=offset: self.client.list_datasets(
                        limit=_PAGE_SIZE_DATASETS, offset=o
                    ),
                    max_attempts=3,
                )
                if not batch:
                    break
                found += len(batch)

                for raw in batch:
                    try:
                        doc = normalize_dataset(raw)
                        synced += 1
                    except Exception as exc:
                        logger.error(
                            "domo.sync.dataset_normalize_failed",
                            error=str(exc),
                            dataset_id=raw.get("id"),
                        )
                        failed += 1

                if len(batch) < _PAGE_SIZE_DATASETS:
                    break
                offset += len(batch)

            except Exception as exc:
                logger.error(
                    "domo.sync.datasets_page_failed",
                    offset=offset,
                    error=str(exc),
                )
                break

        return found, synced, failed

    async def _sync_pages(
        self, kwargs: Dict[str, Any]
    ) -> tuple[int, int, int]:
        """Paginate and sync all dashboard pages. Returns (found, synced, failed)."""
        found = 0
        synced = 0
        failed = 0
        offset = 0

        while True:
            try:
                batch = await with_retry(
                    lambda o=offset: self.client.list_pages(
                        limit=_PAGE_SIZE_PAGES, offset=o
                    ),
                    max_attempts=3,
                )
                if not batch:
                    break
                found += len(batch)

                for raw in batch:
                    try:
                        doc = normalize_page(raw)
                        synced += 1
                    except Exception as exc:
                        logger.error(
                            "domo.sync.page_normalize_failed",
                            error=str(exc),
                            page_id=raw.get("id"),
                        )
                        failed += 1

                if len(batch) < _PAGE_SIZE_PAGES:
                    break
                offset += len(batch)

            except Exception as exc:
                logger.error(
                    "domo.sync.pages_page_failed",
                    offset=offset,
                    error=str(exc),
                )
                break

        return found, synced, failed

    # ── public query methods ──────────────────────────────────────────────────

    async def list_datasets(self) -> List[Dict[str, Any]]:
        """Return all Domo datasets (auto-paginates)."""
        all_items: List[Dict[str, Any]] = []
        offset = 0
        while True:
            batch = await with_retry(
                lambda o=offset: self.client.list_datasets(
                    limit=_PAGE_SIZE_DATASETS, offset=o
                ),
                max_attempts=3,
            )
            if not batch:
                break
            all_items.extend(batch)
            if len(batch) < _PAGE_SIZE_DATASETS:
                break
            offset += len(batch)
        return all_items

    async def list_pages(self) -> List[Dict[str, Any]]:
        """Return all Domo dashboard pages (auto-paginates)."""
        all_items: List[Dict[str, Any]] = []
        offset = 0
        while True:
            batch = await with_retry(
                lambda o=offset: self.client.list_pages(
                    limit=_PAGE_SIZE_PAGES, offset=o
                ),
                max_attempts=3,
            )
            if not batch:
                break
            all_items.extend(batch)
            if len(batch) < _PAGE_SIZE_PAGES:
                break
            offset += len(batch)
        return all_items

    async def list_users(self) -> List[Dict[str, Any]]:
        """Return all Domo users (auto-paginates)."""
        all_items: List[Dict[str, Any]] = []
        offset = 0
        while True:
            batch = await with_retry(
                lambda o=offset: self.client.list_users(
                    limit=_PAGE_SIZE_USERS, offset=o
                ),
                max_attempts=3,
            )
            if not batch:
                break
            all_items.extend(batch)
            if len(batch) < _PAGE_SIZE_USERS:
                break
            offset += len(batch)
        return all_items

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """Return a single Domo dataset by ID."""
        return await with_retry(
            lambda: self.client.get_dataset(dataset_id), max_attempts=3
        )

    async def get_page(self, page_id: int) -> Dict[str, Any]:
        """Return a single Domo dashboard page by ID."""
        return await with_retry(
            lambda: self.client.get_page(page_id), max_attempts=3
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> "DomoConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
