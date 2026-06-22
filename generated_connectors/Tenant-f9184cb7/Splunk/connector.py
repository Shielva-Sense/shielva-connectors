from __future__ import annotations

from typing import Any

from client.http_client import SplunkHTTPClient
from exceptions import SplunkAuthError, SplunkError, SplunkNetworkError
from helpers.utils import (
    normalize_app,
    normalize_index,
    normalize_saved_search,
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

CONNECTOR_TYPE = "splunk"
AUTH_TYPE = "api_key"


class SplunkConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Splunk Enterprise / Splunk Cloud.

    Provides authentication, health checks, full sync, and direct API access
    for saved searches, indexes, apps, users, and ad-hoc searches.

    Auth: ``Authorization: Bearer {token}`` header on every request.
    Base URL: ``https://{host}:{port}`` (default management port: 8089).
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

        self._token: str = _config.get("token", "")
        self._host: str = _config.get("host", "")
        self._index: str = _config.get("index", "")
        self.client: SplunkHTTPClient = SplunkHTTPClient(config=self.config)

    def _make_client(self) -> SplunkHTTPClient:
        return SplunkHTTPClient(config=self.config)

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate host and token by calling GET /services/server/info."""
        if not self._host:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="host is required",
            )
        if not self._token:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="token is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_info)
            await client.aclose()
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Splunk at {self._host}",
            )
        except SplunkAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Splunk token: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /services/server/info and return current health with server version."""
        if not self._host or not self._token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="host and token are required",
            )
        client = self._make_client()
        try:
            info = await with_retry(client.get_info)
            await client.aclose()
            # Extract version from server info entry content
            version = _extract_server_version(info)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Splunk at {self._host}",
                server_version=version,
            )
        except SplunkAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except SplunkNetworkError as exc:
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
        since: Any = None,  # noqa: ARG002
        kb_id: str = "",
    ) -> SyncResult:
        """Sync saved searches, indexes, and apps from Splunk."""
        found = 0
        synced = 0
        failed = 0

        # Sync saved searches
        try:
            saved_searches = await self.list_saved_searches()
            found += len(saved_searches)
            for raw in saved_searches:
                try:
                    doc = normalize_saved_search(
                        raw, connector_id=self.connector_id, tenant_id=self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except SplunkError:
            pass

        # Sync indexes
        try:
            indexes = await self.list_indexes()
            found += len(indexes)
            for raw in indexes:
                try:
                    doc = normalize_index(
                        raw, connector_id=self.connector_id, tenant_id=self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except SplunkError:
            pass

        # Sync apps
        try:
            apps = await self.list_apps()
            found += len(apps)
            for raw in apps:
                try:
                    doc = normalize_app(
                        raw, connector_id=self.connector_id, tenant_id=self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except SplunkError:
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

    # ── Indexes ───────────────────────────────────────────────────────────────

    async def list_indexes(self) -> list[dict[str, Any]]:
        """Fetch all Splunk indexes."""
        result = await with_retry(self.client.get_indexes)
        entries: list[dict[str, Any]] = result.get("entry", [])
        return entries if isinstance(entries, list) else []

    # ── Saved searches ────────────────────────────────────────────────────────

    async def list_saved_searches(self) -> list[dict[str, Any]]:
        """Fetch all Splunk saved searches."""
        result = await with_retry(self.client.get_saved_searches)
        entries: list[dict[str, Any]] = result.get("entry", [])
        return entries if isinstance(entries, list) else []

    # ── Apps ─────────────────────────────────────────────────────────────────

    async def list_apps(self) -> list[dict[str, Any]]:
        """Fetch all installed Splunk apps."""
        result = await with_retry(self.client.get_apps)
        entries: list[dict[str, Any]] = result.get("entry", [])
        return entries if isinstance(entries, list) else []

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(self) -> list[dict[str, Any]]:
        """Fetch all Splunk users."""
        result = await with_retry(self.client.get_users)
        entries: list[dict[str, Any]] = result.get("entry", [])
        return entries if isinstance(entries, list) else []

    # ── Search ────────────────────────────────────────────────────────────────

    async def run_search(
        self,
        query: str,
        earliest: str = "-24h",
        latest: str = "now",
    ) -> list[dict[str, Any]]:
        """Run a Splunk search and return result rows.

        Args:
            query:    SPL search query string.
            earliest: Earliest time bound (default ``-24h``).
            latest:   Latest time bound (default ``now``).

        Returns:
            List of result row dicts from the search job.
        """
        result = await with_retry(self.client.run_search, query, earliest=earliest, latest=latest)
        rows: list[dict[str, Any]] = result.get("results", [])
        return rows if isinstance(rows, list) else []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def __aenter__(self) -> SplunkConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _extract_server_version(info: dict[str, Any]) -> str:
    """Extract the Splunk server version string from a /services/server/info response."""
    entries: list[Any] = info.get("entry", [])
    if entries and isinstance(entries, list):
        first = entries[0]
        if isinstance(first, dict):
            content: dict[str, Any] = first.get("content", {})
            if isinstance(content, dict):
                return str(content.get("version", ""))
    return ""
