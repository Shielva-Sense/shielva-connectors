from __future__ import annotations

from typing import Any

from client import BugsnagHTTPClient
from exceptions import BugsnagAuthError, BugsnagError, BugsnagNetworkError, BugsnagNotFoundError
from helpers import (
    normalize_error,
    normalize_project,
    normalize_release,
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


CONNECTOR_TYPE: str = "bugsnag"
AUTH_TYPE: str = "api_key"
SYNC_ERROR_LIMIT: int = 25


class BugsnagConnector(BaseConnector):
    """Shielva connector for Bugsnag.

    Syncs projects, errors (top errors per project), and releases from the
    Bugsnag Data Access API v2.
    Auth: ``Authorization: token {auth_token}`` (note: ``token`` prefix, NOT ``Bearer``).
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
        self._auth_token: str = _config.get("auth_token", "")
        self._org_slug: str = _config.get("organization_slug", "")
        self.client: BugsnagHTTPClient = BugsnagHTTPClient(config=_config)

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._auth_token:
            missing.append("auth_token")
        if not self._org_slug:
            missing.append("organization_slug")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by verifying auth_token and organization_slug are present.

        A lightweight credential presence check — calls get_organization to confirm
        the token has access to the specified organization.
        """
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        try:
            org = await with_retry(self.client.get_organization, self._org_slug)
            org_name: str = org.get("name", self._org_slug)
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Bugsnag organization: {org_name}",
            )
        except BugsnagAuthError as exc:
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
        """Ping GET /organizations/{org_slug} and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        try:
            org = await with_retry(self.client.get_organization, self._org_slug)
            org_name: str = org.get("name", self._org_slug)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Bugsnag API reachable. Organization: {org_name}",
            )
        except BugsnagAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except BugsnagNetworkError as exc:
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
        """Sync projects and top errors (per project) from Bugsnag.

        1. Fetch all projects for the organization.
        2. For each project fetch top errors (up to SYNC_ERROR_LIMIT per project).
        3. Normalize each resource to a ConnectorDocument and optionally ingest.
        """
        found = 0
        synced = 0
        failed = 0

        # 1. Projects
        project_ids: list[str] = []
        try:
            projects = await with_retry(self.list_projects)
            found += len(projects)
            for doc in projects:
                try:
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                    pid = doc.metadata.get("project_id", "")
                    if pid:
                        project_ids.append(pid)
                except Exception:
                    failed += 1
        except BugsnagError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # 2. Top errors per project
        for project_id in project_ids:
            try:
                errors = await with_retry(
                    self.list_errors, project_id=project_id
                )
                found += len(errors)
                for doc in errors:
                    try:
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except BugsnagError:
                pass  # non-fatal per project

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

    # ── Projects ──────────────────────────────────────────────────────────────

    async def list_projects(self) -> list[ConnectorDocument]:
        """Fetch all projects for the organization via offset pagination."""
        docs: list[ConnectorDocument] = []
        offset: int | None = None

        while True:
            items, next_url = await with_retry(
                self.client.get_projects,
                self._org_slug,
                per_page=100,
                offset=offset,
            )
            for raw in items:
                doc = normalize_project(raw)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
            if not next_url or not items:
                break
            # Increment offset for next page (fallback if X-Next-Page-Link not usable)
            offset = (offset or 0) + len(items)

        return docs

    # ── Errors ────────────────────────────────────────────────────────────────

    async def list_errors(
        self,
        project_id: str,
        severity: str | None = None,
    ) -> list[ConnectorDocument]:
        """Fetch errors for a project via X-Next-Page-Link pagination."""
        docs: list[ConnectorDocument] = []
        offset: int | None = None

        while True:
            items, next_url = await with_retry(
                self.client.get_errors,
                project_id,
                per_page=SYNC_ERROR_LIMIT,
                per_page_offset=offset,
                severity=severity,
            )
            for raw in items:
                doc = normalize_error(raw, project_id=project_id)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
            if not next_url or not items:
                break
            offset = (offset or 0) + len(items)

        return docs

    async def get_error(self, project_id: str, error_id: str) -> dict[str, Any]:
        """Return a single raw Bugsnag error by project_id and error_id."""
        return await with_retry(self.client.get_error, project_id, error_id)

    # ── Releases ──────────────────────────────────────────────────────────────

    async def list_releases(self, project_id: str) -> list[ConnectorDocument]:
        """Fetch releases for a specific project."""
        raw_list = await with_retry(self.client.get_releases, project_id)
        docs: list[ConnectorDocument] = []
        for raw in raw_list:
            doc = normalize_release(raw)
            doc.connector_id = self.connector_id
            doc.tenant_id = self.tenant_id
            docs.append(doc)
        return docs

    # ── Collaborators ─────────────────────────────────────────────────────────

    async def list_collaborators(self) -> list[dict[str, Any]]:
        """Fetch all collaborators for the organization."""
        return await with_retry(self.client.get_collaborators, self._org_slug)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> BugsnagConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
