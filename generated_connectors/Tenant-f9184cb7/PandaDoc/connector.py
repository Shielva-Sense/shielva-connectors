from __future__ import annotations

from typing import Any, Optional

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: Optional[dict[str, Any]] = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

from client.http_client import PandaDocHTTPClient
from exceptions import PandaDocAuthError, PandaDocError, PandaDocNetworkError
from helpers.utils import (
    normalize_contact,
    normalize_document,
    normalize_form,
    normalize_template,
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

CONNECTOR_TYPE: str = "pandadoc"
AUTH_TYPE: str = "api_key"

SYNC_PAGE_SIZE: int = 100


class PandaDocConnector(BaseConnector):
    """
    Shielva connector for PandaDoc API v1.

    Authenticates via API Key (Authorization: API-Key {key}).
    Syncs documents, templates, contacts, forms, and members.
    """

    CONNECTOR_TYPE: str = "pandadoc"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        _config = config or {}
        super().__init__(
            tenant_id=tenant_id,
            connector_id=connector_id,
            config=_config,
        )
        self._api_key: str = _config.get("api_key", "")
        self.client = PandaDocHTTPClient(config=_config)

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key is present, then verify with a health check."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        try:
            data = await with_retry(self.client.get_workspaces)
            workspaces: list[dict[str, Any]] = data.get("workspaces", [])
            workspace_name: str = (
                workspaces[0].get("name", "") if workspaces else ""
            )
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=(
                    f"PandaDoc API key validated."
                    + (f" Workspace: {workspace_name}" if workspace_name else "")
                ),
            )
        except PandaDocAuthError:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="Invalid API key — check your PandaDoc credentials",
            )
        except PandaDocNetworkError as exc:
            return InstallResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            return InstallResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Health Check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """GET /workspaces/ and return health status."""
        if not self._api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="No API key configured",
            )
        try:
            data = await with_retry(self.client.get_workspaces)
            workspaces: list[dict[str, Any]] = data.get("workspaces", [])
            workspace_name: str = (
                workspaces[0].get("name", "") if workspaces else ""
            )
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="PandaDoc API is reachable",
                workspace_name=workspace_name,
            )
        except PandaDocAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except PandaDocNetworkError as exc:
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

    async def sync(self, **kwargs: Any) -> SyncResult:
        """
        Sync all PandaDoc resources (documents, templates, contacts, forms)
        into the knowledge base.
        """
        if not self._api_key:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="No API key configured",
            )

        kb_id: str = kwargs.get("kb_id", "")
        found = 0
        synced = 0
        failed = 0

        # Sync documents
        try:
            docs = await self.list_documents()
            found += len(docs)
            for raw in docs:
                try:
                    doc = normalize_document(raw)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except PandaDocError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to sync documents: {exc}",
            )

        # Sync templates
        try:
            templates = await self.list_templates()
            found += len(templates)
            for raw in templates:
                try:
                    doc = normalize_template(raw)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except PandaDocError:
            failed += 1

        # Sync contacts
        try:
            contacts = await self.list_contacts()
            found += len(contacts)
            for raw in contacts:
                try:
                    doc = normalize_contact(raw)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except PandaDocError:
            failed += 1

        # Sync forms
        try:
            forms = await self.list_forms()
            found += len(forms)
            for raw in forms:
                try:
                    doc = normalize_form(raw)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except PandaDocError:
            failed += 1

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
        """Push a normalized document into the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── List methods ──────────────────────────────────────────────────────────

    async def list_documents(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch all documents using count+page pagination."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            page_data = await with_retry(
                self.client.get_documents,
                page=page,
                count=SYNC_PAGE_SIZE,
                **kwargs,
            )
            items: list[dict[str, Any]] = page_data.get("results", [])
            if not items:
                break
            results.extend(items)
            if len(items) < SYNC_PAGE_SIZE:
                break
            page += 1
        return results

    async def list_templates(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch all templates using count+page pagination."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            page_data = await with_retry(
                self.client.get_templates,
                page=page,
                count=SYNC_PAGE_SIZE,
            )
            items: list[dict[str, Any]] = page_data.get("results", [])
            if not items:
                break
            results.extend(items)
            if len(items) < SYNC_PAGE_SIZE:
                break
            page += 1
        return results

    async def list_contacts(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch all contacts using count+page pagination."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            page_data = await with_retry(
                self.client.get_contacts,
                page=page,
                count=SYNC_PAGE_SIZE,
            )
            items: list[dict[str, Any]] = page_data.get("results", [])
            if not items:
                break
            results.extend(items)
            if len(items) < SYNC_PAGE_SIZE:
                break
            page += 1
        return results

    async def list_forms(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch all forms using count+page pagination."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            page_data = await with_retry(
                self.client.get_forms,
                page=page,
                count=SYNC_PAGE_SIZE,
            )
            items: list[dict[str, Any]] = page_data.get("results", [])
            if not items:
                break
            results.extend(items)
            if len(items) < SYNC_PAGE_SIZE:
                break
            page += 1
        return results

    # ── Single resource getters ───────────────────────────────────────────────

    async def get_document(self, document_id: str) -> dict[str, Any]:
        """GET /documents/{id}"""
        return await with_retry(self.client.get_document, document_id)

    async def get_document_details(self, document_id: str) -> dict[str, Any]:
        """GET /documents/{id}/details"""
        return await with_retry(self.client.get_document_details, document_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> PandaDocConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
