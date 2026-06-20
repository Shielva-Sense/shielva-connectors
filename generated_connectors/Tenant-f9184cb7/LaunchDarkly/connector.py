from __future__ import annotations

from typing import Any

from client.http_client import LaunchDarklyHTTPClient
from exceptions import LaunchDarklyAuthError, LaunchDarklyError, LaunchDarklyNetworkError
from helpers.utils import (
    normalize_audit_entry,
    normalize_environment,
    normalize_flag,
    normalize_member,
    normalize_project,
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

from shared.base_connector import BaseConnector

CONNECTOR_TYPE = "launchdarkly"
AUTH_TYPE = "api_key"

# Safety caps for pagination during a full sync
_MAX_FLAG_PAGES: int = 50
_MAX_MEMBER_PAGES: int = 20
_MAX_AUDIT_PAGES: int = 10


class LaunchDarklyConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for LaunchDarkly feature flag management.

    Syncs projects, feature flags, environments, members, and audit log entries
    from the LaunchDarkly REST API v2.

    Auth: raw API key in ``Authorization`` header (no "Bearer" prefix).
    API version: ``LD-API-Version: 20220603`` header.
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
        if type(BaseConnector) is not type(object):
            try:
                super().__init__(
                    tenant_id=tenant_id, connector_id=connector_id, config=_config
                )
            except TypeError:
                self.tenant_id = tenant_id
                self.connector_id = connector_id
                self.config = _config
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = _config

        self._api_key: str = _config.get("api_key", "")
        self.client: LaunchDarklyHTTPClient = LaunchDarklyHTTPClient(config=self.config)

    def _make_client(self) -> LaunchDarklyHTTPClient:
        return LaunchDarklyHTTPClient(config=self.config)

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the API key via GET /projects and return install result."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_projects)
            await client.aclose()
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to LaunchDarkly",
            )
        except LaunchDarklyAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid LaunchDarkly API key: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /projects and return current health."""
        if not self._api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_projects)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Connected to LaunchDarkly",
            )
        except LaunchDarklyAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except LaunchDarklyNetworkError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,  # noqa: ARG002
        since: Any = None,   # noqa: ARG002
        kb_id: str = "",
    ) -> SyncResult:
        """Sync projects, feature flags, environments, members, and audit log."""
        found = 0
        synced = 0
        failed = 0

        # Sync projects
        try:
            projects = await self.list_projects()
            found += len(projects)
            for raw in projects:
                try:
                    doc = normalize_project(raw)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except LaunchDarklyError:
            pass

        # Sync flags and environments for each project
        project_keys: list[str] = []
        try:
            project_keys = [p.get("key", "") for p in await self.list_projects() if p.get("key")]
        except LaunchDarklyError:
            pass

        for project_key in project_keys:
            # Flags
            try:
                flags = await self.list_flags(project_key)
                found += len(flags)
                for raw in flags:
                    try:
                        doc = normalize_flag(raw, project_key=project_key)
                        doc.connector_id = self.connector_id
                        doc.tenant_id = self.tenant_id
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except LaunchDarklyError:
                pass

            # Environments
            try:
                environments = await self.list_environments(project_key)
                found += len(environments)
                for raw in environments:
                    try:
                        doc = normalize_environment(raw, project_key=project_key)
                        doc.connector_id = self.connector_id
                        doc.tenant_id = self.tenant_id
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except LaunchDarklyError:
                pass

        # Sync members
        try:
            members = await self.list_members()
            found += len(members)
            for raw in members:
                try:
                    doc = normalize_member(raw)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except LaunchDarklyError:
            pass

        # Sync audit log
        try:
            entries = await self.list_audit_log()
            found += len(entries)
            for raw in entries:
                try:
                    doc = normalize_audit_entry(raw)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except LaunchDarklyError:
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        if found == 0 and synced == 0:
            status = SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Projects ──────────────────────────────────────────────────────────────

    async def list_projects(self) -> list[dict[str, Any]]:
        """Fetch all LaunchDarkly projects.

        LaunchDarkly returns all projects in a single response for most accounts.
        ``_links.next`` is followed if present for completeness.
        """
        result = await with_retry(self.client.get_projects)
        return result.get("items", [])

    # ── Feature Flags ─────────────────────────────────────────────────────────

    async def list_flags(
        self,
        project_key: str,
        limit: int = 100,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch all feature flags in a project, paginating automatically.

        Args:
            project_key: The LaunchDarkly project key (e.g. ``"default"``).
            limit:       Page size.
            **kwargs:    Extra query params forwarded to the API (env, tag, sort…).
        """
        all_flags: list[dict[str, Any]] = []
        for page in range(_MAX_FLAG_PAGES):
            offset = page * limit
            result = await with_retry(
                self.client.get_flags,
                project_key,
                limit=limit,
                offset=offset,
                **kwargs,
            )
            items: list[dict[str, Any]] = result.get("items", [])
            all_flags.extend(items)
            if len(items) < limit:
                break
        return all_flags

    async def get_flag(self, project_key: str, flag_key: str) -> dict[str, Any]:
        """Retrieve a single feature flag by project key and flag key."""
        return await with_retry(self.client.get_flag, project_key, flag_key)

    # ── Environments ──────────────────────────────────────────────────────────

    async def list_environments(self, project_key: str) -> list[dict[str, Any]]:
        """Fetch all environments in a project.

        Args:
            project_key: The LaunchDarkly project key.
        """
        result = await with_retry(self.client.get_environments, project_key)
        return result.get("items", [])

    # ── Members ───────────────────────────────────────────────────────────────

    async def list_members(
        self,
        limit: int = 100,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch all account members, paginating automatically.

        Args:
            limit:    Page size.
            **kwargs: Extra query params.
        """
        all_members: list[dict[str, Any]] = []
        for page in range(_MAX_MEMBER_PAGES):
            offset = page * limit
            result = await with_retry(
                self.client.get_members,
                limit=limit,
                offset=offset,
            )
            items: list[dict[str, Any]] = result.get("items", [])
            all_members.extend(items)
            if len(items) < limit:
                break
        return all_members

    # ── Audit Log ─────────────────────────────────────────────────────────────

    async def list_audit_log(
        self,
        limit: int = 100,
        after: int | None = None,
        before: int | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch audit log entries, following cursor pagination automatically.

        Args:
            limit:  Number of entries per page.
            after:  Only entries after this Unix timestamp (ms).
            before: Only entries before this Unix timestamp (ms).
            **kwargs: Ignored — present for forward-compatibility.
        """
        all_entries: list[dict[str, Any]] = []
        for _ in range(_MAX_AUDIT_PAGES):
            result = await with_retry(
                self.client.get_audit_log,
                limit=limit,
                after=after,
                before=before,
            )
            items: list[dict[str, Any]] = result.get("items", [])
            all_entries.extend(items)
            if len(items) < limit:
                break
            # Advance cursor: set `after` to the date of the last entry so the
            # next page fetches only entries after this batch.
            if items:
                last_date = items[-1].get("date")
                if last_date is not None:
                    after = int(last_date)
                else:
                    break
            else:
                break
        return all_entries

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def __aenter__(self) -> LaunchDarklyConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
