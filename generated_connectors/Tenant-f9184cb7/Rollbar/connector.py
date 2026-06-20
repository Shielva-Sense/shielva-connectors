from __future__ import annotations

from typing import Any

from client import RollbarHTTPClient
from exceptions import RollbarAuthError, RollbarError, RollbarNetworkError
from helpers import (
    normalize_deploy,
    normalize_item,
    normalize_occurrence,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

try:
    from shielva_connectors.base import BaseConnector
except ImportError:

    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}


CONNECTOR_TYPE: str = "rollbar"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_LIMIT: int = 5  # conservative page cap per resource type per sync run


class RollbarConnector(BaseConnector):
    """Shielva connector for Rollbar.

    Syncs error items, raw occurrences, and deploys from the Rollbar REST API v1.
    Auth: ``?access_token={token}`` query parameter on every request.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(
            tenant_id=tenant_id, connector_id=connector_id, config=_config
        )
        self._access_token: str = _config.get("access_token", "")
        self._account_access_token: str = _config.get("account_access_token", "")
        self.client: RollbarHTTPClient = RollbarHTTPClient(config=_config)

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._access_token:
            missing.append("access_token")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling GET /api/1/project/."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        try:
            project = await with_retry(self.client.get_project)
            project_name: str = project.get("name", "unknown")
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Rollbar project: {project_name}",
            )
        except RollbarAuthError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /api/1/project/ and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        try:
            project = await with_retry(self.client.get_project)
            project_name: str = project.get("name", "unknown")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Rollbar API reachable. Project: {project_name}",
            )
        except RollbarAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except RollbarNetworkError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(self, kb_id: str = "", **kwargs: Any) -> SyncResult:
        """Sync items (errors) and deploys from Rollbar."""
        found = 0
        synced = 0
        failed = 0

        # 1. Items (error groups)
        try:
            items = await with_retry(self.list_items)
            found += len(items)
            for doc in items:
                try:
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except RollbarError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # 2. Deploys
        try:
            deploys = await with_retry(self.list_deploys)
            found += len(deploys)
            for doc in deploys:
                try:
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except RollbarError:
            pass  # non-fatal — deploys are supplementary

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(
        self, doc: ConnectorDocument, kb_id: str
    ) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Items ─────────────────────────────────────────────────────────────────

    async def list_items(
        self,
        level: str | None = None,
        status: str | None = None,
    ) -> list[ConnectorDocument]:
        """Fetch error items (first SYNC_PAGE_LIMIT pages), optionally filtered by level/status."""
        docs: list[ConnectorDocument] = []
        for page in range(1, SYNC_PAGE_LIMIT + 1):
            body = await with_retry(self.client.get_items, page=page, level=level, status=status)
            result = body.get("result", {}) if isinstance(body, dict) else {}
            raw_items: list[dict[str, Any]] = result.get("items", []) if isinstance(result, dict) else []
            if not raw_items:
                break
            for raw in raw_items:
                doc = normalize_item(raw)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
        return docs

    async def list_occurrences(self) -> list[ConnectorDocument]:
        """Fetch raw error occurrences (first SYNC_PAGE_LIMIT pages)."""
        docs: list[ConnectorDocument] = []
        for page in range(1, SYNC_PAGE_LIMIT + 1):
            body = await with_retry(self.client.get_occurrences, page=page)
            result = body.get("result", {}) if isinstance(body, dict) else {}
            raw_list: list[dict[str, Any]] = result.get("instances", []) if isinstance(result, dict) else []
            if not raw_list:
                break
            for raw in raw_list:
                doc = normalize_occurrence(raw)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
        return docs

    async def list_deploys(self) -> list[ConnectorDocument]:
        """Fetch deploys (first SYNC_PAGE_LIMIT pages)."""
        docs: list[ConnectorDocument] = []
        for page in range(1, SYNC_PAGE_LIMIT + 1):
            body = await with_retry(self.client.get_deploys, page=page)
            result = body.get("result", {}) if isinstance(body, dict) else {}
            raw_list: list[dict[str, Any]] = result.get("deploys", []) if isinstance(result, dict) else []
            if not raw_list:
                break
            for raw in raw_list:
                doc = normalize_deploy(raw)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
        return docs

    async def get_item(self, item_id: int | str) -> dict[str, Any]:
        """Return a single raw Rollbar item by ID."""
        return await with_retry(self.client.get_item, item_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> RollbarConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
