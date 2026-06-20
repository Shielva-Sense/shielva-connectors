from __future__ import annotations

from typing import Any

from client import SentryHTTPClient
from exceptions import SentryAuthError, SentryError, SentryNetworkError
from helpers import (
    normalize_event,
    normalize_issue,
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


CONNECTOR_TYPE: str = "sentry"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_LIMIT: int = 100


class SentryConnector(BaseConnector):
    """Shielva connector for Sentry.

    Syncs issues, projects, releases, and issue events from the Sentry REST API.
    Auth: Bearer token via ``Authorization: Bearer {auth_token}``.
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
        self.client: SentryHTTPClient = SentryHTTPClient(config=_config)

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._auth_token:
            missing.append("auth_token")
        if not self._org_slug:
            missing.append("organization_slug")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling GET /organizations/{slug}/."""
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
                message=f"Connected to Sentry organization: {org_name}",
            )
        except SentryAuthError as exc:
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
        """Ping GET /organizations/{slug}/ and return current health status."""
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
                message=f"Sentry API reachable. Organization: {org_name}",
            )
        except SentryAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except SentryNetworkError as exc:
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
        """Sync projects, issues (per project), releases, and issue events."""
        found = 0
        synced = 0
        failed = 0

        # 1. Projects
        try:
            projects = await with_retry(self.list_projects)
            found += len(projects)
            project_slugs: list[str] = []
            for doc in projects:
                try:
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                    slug = doc.metadata.get("slug", "")
                    if slug:
                        project_slugs.append(slug)
                except Exception:
                    failed += 1
        except SentryError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # 2. Issues per project
        for project_slug in project_slugs:
            try:
                issues = await with_retry(self.list_issues, project_slug=project_slug)
                found += len(issues)
                for doc in issues:
                    try:
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except SentryError:
                pass  # non-fatal per project

        # 3. Releases
        try:
            releases = await with_retry(self.list_releases)
            found += len(releases)
            for doc in releases:
                try:
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except SentryError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

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

    # ── Issues ────────────────────────────────────────────────────────────────

    async def list_issues(
        self, project_slug: str | None = None, **kwargs: Any
    ) -> list[ConnectorDocument]:
        """Fetch all issues for the org (optionally filtered by project) via cursor pagination."""
        docs: list[ConnectorDocument] = []
        cursor: str | None = None

        while True:
            items, next_cursor = await with_retry(
                self.client.get_issues,
                self._org_slug,
                project=project_slug,
                cursor=cursor,
                limit=SYNC_PAGE_LIMIT,
            )
            for raw in items:
                doc = normalize_issue(raw, self._org_slug)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
            if not next_cursor or not items:
                break
            cursor = next_cursor

        return docs

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        """Return a single raw Sentry issue by ID."""
        return await with_retry(self.client.get_issue, issue_id)

    # ── Projects ──────────────────────────────────────────────────────────────

    async def list_projects(self) -> list[ConnectorDocument]:
        """Fetch all projects for the organization."""
        raw_list = await with_retry(self.client.get_projects, self._org_slug)
        docs: list[ConnectorDocument] = []
        for raw in raw_list:
            doc = normalize_project(raw, self._org_slug)
            doc.connector_id = self.connector_id
            doc.tenant_id = self.tenant_id
            docs.append(doc)
        return docs

    # ── Releases ──────────────────────────────────────────────────────────────

    async def list_releases(
        self, project_slug: str | None = None
    ) -> list[ConnectorDocument]:
        """Fetch all releases for the org (optionally filtered by project) via cursor pagination."""
        docs: list[ConnectorDocument] = []
        cursor: str | None = None

        while True:
            items, next_cursor = await with_retry(
                self.client.get_releases,
                self._org_slug,
                project=project_slug,
                cursor=cursor,
            )
            for raw in items:
                doc = normalize_release(raw, self._org_slug)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
            if not next_cursor or not items:
                break
            cursor = next_cursor

        return docs

    # ── Issue Events ──────────────────────────────────────────────────────────

    async def list_issue_events(self, issue_id: str) -> list[ConnectorDocument]:
        """Fetch all events for a specific issue."""
        raw_list = await with_retry(self.client.get_issue_events, issue_id)
        docs: list[ConnectorDocument] = []
        for raw in raw_list:
            doc = normalize_event(raw, issue_id)
            doc.connector_id = self.connector_id
            doc.tenant_id = self.tenant_id
            docs.append(doc)
        return docs

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> SentryConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
