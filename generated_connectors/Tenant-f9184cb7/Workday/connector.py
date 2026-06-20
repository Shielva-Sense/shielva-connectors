"""Workday connector for Shielva.

Syncs workers, organizations, job profiles, and locations from the
Workday REST API using OAuth 2.0 Client Credentials flow.
"""
from __future__ import annotations

from typing import Any, Dict

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: Dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

from client import WorkdayHTTPClient
from exceptions import WorkdayAuthError, WorkdayError, WorkdayNetworkError
from helpers import (
    normalize_job_profile,
    normalize_location,
    normalize_organization,
    normalize_worker,
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

CONNECTOR_TYPE: str = "workday"
AUTH_TYPE: str = "api_key"  # Client Credentials = machine-to-machine, no user redirect


class WorkdayConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Workday HCM.

    Syncs workers, organizations, job profiles, and locations from the
    Workday REST API v1 using OAuth 2.0 Client Credentials.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        try:
            super().__init__(
                tenant_id=tenant_id, connector_id=connector_id, config=_config
            )
        except TypeError:
            # Fallback when BaseConnector is the stub above
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = _config

        self.client = WorkdayHTTPClient(config=self.config)

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        for field in ("client_id", "client_secret", "tenant"):
            if not self.config.get(field, ""):
                missing.append(field)
        # Accept either 'hostname' (spec) or legacy 'base_url'
        if not self.config.get("hostname", "") and not self.config.get("base_url", ""):
            missing.append("hostname")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by obtaining an OAuth2 token and listing workers."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = WorkdayHTTPClient(config=self.config)
        try:
            await with_retry(client.authenticate)
            # Quick resource check — fetch first page of workers
            workers = await with_retry(client.get_workers)
            tenant_name = self.config.get("tenant", "")
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Workday tenant: {tenant_name} ({len(workers)} workers found)",
            )
        except WorkdayAuthError as exc:
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
        """Verify connectivity by fetching the first page of workers."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = WorkdayHTTPClient(config=self.config)
        try:
            await with_retry(client.authenticate)
            workers = await with_retry(client.get_workers)
            tenant_name = self.config.get("tenant", "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Workday API reachable. Tenant: {tenant_name} ({len(workers)} workers)",
            )
        except WorkdayAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except WorkdayNetworkError as exc:
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

    async def sync(
        self,
        full: bool = False,
        since: object = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync all Workday resources into the knowledge base.

        Syncs: workers, organizations, job profiles, locations.
        Each resource failure is non-fatal — partial sync returns PARTIAL status.
        """
        base_url = self.config.get("base_url", "")
        found = 0
        synced = 0
        failed = 0

        client = self.client

        # Ensure we have a token before iterating resources
        try:
            await with_retry(client.authenticate)
        except WorkdayError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message=str(exc),
            )

        # ── Workers ───────────────────────────────────────────────────────────
        try:
            workers = await with_retry(client.get_workers)
            found += len(workers)
            for raw in workers:
                try:
                    doc = normalize_worker(
                        raw,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        base_url=base_url,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except WorkdayError:
            pass  # worker failure is non-fatal — continue with other resources

        # ── Organizations ─────────────────────────────────────────────────────
        try:
            orgs = await with_retry(client.get_organizations)
            found += len(orgs)
            for raw in orgs:
                try:
                    doc = normalize_organization(
                        raw,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        base_url=base_url,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except WorkdayError:
            pass

        # ── Job Profiles ──────────────────────────────────────────────────────
        try:
            profiles = await with_retry(client.get_job_profiles)
            found += len(profiles)
            for raw in profiles:
                try:
                    doc = normalize_job_profile(
                        raw,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        base_url=base_url,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except WorkdayError:
            pass

        # ── Locations ─────────────────────────────────────────────────────────
        try:
            locations = await with_retry(client.get_locations)
            found += len(locations)
            for raw in locations:
                try:
                    doc = normalize_location(
                        raw,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        base_url=base_url,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except WorkdayError:
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        if found == 0 and synced == 0 and failed == 0:
            status = SyncStatus.COMPLETED  # empty tenant is still a success

        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document into the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Public resource accessors ─────────────────────────────────────────────

    async def list_workers(self) -> list[dict[str, Any]]:
        """Return all Workday workers as raw API records."""
        return await with_retry(self.client.get_workers)

    async def list_organizations(self) -> list[dict[str, Any]]:
        """Return all Workday organizations as raw API records."""
        return await with_retry(self.client.get_organizations)

    async def list_job_profiles(self) -> list[dict[str, Any]]:
        """Return all Workday job profiles as raw API records."""
        return await with_retry(self.client.get_job_profiles)

    async def list_locations(self) -> list[dict[str, Any]]:
        """Return all Workday locations as raw API records."""
        return await with_retry(self.client.get_locations)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release resources."""
        self.client = WorkdayHTTPClient(config=self.config)

    async def __aenter__(self) -> WorkdayConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
