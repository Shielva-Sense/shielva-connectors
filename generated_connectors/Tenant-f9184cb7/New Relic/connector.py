from __future__ import annotations

from typing import Any

from client.http_client import NewRelicHTTPClient
from exceptions import NewRelicAuthError, NewRelicError, NewRelicNetworkError
from helpers.utils import (
    normalize_alert,
    normalize_application,
    normalize_dashboard,
    normalize_incident,
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

CONNECTOR_TYPE = "newrelic"
AUTH_TYPE = "api_key"

# Maximum pages to pull during a full sync (safety cap)
_MAX_ALERT_PAGES: int = 20
_MAX_APP_PAGES: int = 20


class NewRelicConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for New Relic application performance monitoring.

    Provides authentication, health checks, full sync, and direct API access
    for alert policies, applications, incidents, dashboards, and NRQL queries.

    Auth: Single API Key — sent as both ``Api-Key`` and ``X-Api-Key`` headers.
    Region: US (api.newrelic.com) or EU (api.eu.newrelic.com).
    NerdGraph: GraphQL endpoint for incidents and dashboards.
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
        self._account_id: str = str(_config.get("account_id", ""))
        self._region: str = (_config.get("region", "US") or "US").upper()
        self.client: NewRelicHTTPClient = NewRelicHTTPClient(config=self.config)

    def _make_client(self) -> NewRelicHTTPClient:
        return NewRelicHTTPClient(config=self.config)

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key and account_id via GET /v2/applications.json."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        if not self._account_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="account_id is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.validate_api_key)
            await client.aclose()
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to New Relic ({self._region} region)",
            )
        except NewRelicAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid New Relic API key: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /v2/applications.json and return current health."""
        if not self._api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        if not self._account_id:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="account_id is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.validate_api_key)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to New Relic ({self._region} region)",
            )
        except NewRelicAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except NewRelicNetworkError as exc:
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
        """Sync alerts, applications, incidents, and dashboards from New Relic."""
        found = 0
        synced = 0
        failed = 0

        # Sync alert policies
        try:
            alerts = await self.list_alerts()
            found += len(alerts)
            for raw in alerts:
                try:
                    doc = normalize_alert(
                        raw, connector_id=self.connector_id, tenant_id=self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except NewRelicError:
            pass

        # Sync applications
        try:
            apps = await self.list_applications()
            found += len(apps)
            for raw in apps:
                try:
                    doc = normalize_application(
                        raw, connector_id=self.connector_id, tenant_id=self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except NewRelicError:
            pass

        # Sync incidents
        try:
            incidents = await self.list_incidents()
            found += len(incidents)
            for raw in incidents:
                try:
                    doc = normalize_incident(
                        raw, connector_id=self.connector_id, tenant_id=self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except NewRelicError:
            pass

        # Sync dashboards
        try:
            dashboards = await self.list_dashboards()
            found += len(dashboards)
            for raw in dashboards:
                try:
                    doc = normalize_dashboard(
                        raw, connector_id=self.connector_id, tenant_id=self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except NewRelicError:
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

    # ── Alert policies ────────────────────────────────────────────────────────

    async def list_alerts(self) -> list[dict[str, Any]]:
        """Fetch all alert policies from New Relic, paginating automatically."""
        all_policies: list[dict[str, Any]] = []
        for page in range(1, _MAX_ALERT_PAGES + 1):
            result = await with_retry(self.client.get_alert_policies, page=page)
            policies: list[dict[str, Any]] = result.get("policies", [])
            if not policies:
                break
            all_policies.extend(policies)
            # New Relic REST v2 returns up to 25 per page by default
            if len(policies) < 25:
                break
        return all_policies

    # ── Applications ──────────────────────────────────────────────────────────

    async def list_applications(self) -> list[dict[str, Any]]:
        """Fetch all APM applications from New Relic, paginating automatically."""
        all_apps: list[dict[str, Any]] = []
        for page in range(1, _MAX_APP_PAGES + 1):
            result = await with_retry(self.client.get_applications, page=page)
            apps: list[dict[str, Any]] = result.get("applications", [])
            if not apps:
                break
            all_apps.extend(apps)
            if len(apps) < 25:
                break
        return all_apps

    # ── Incidents ─────────────────────────────────────────────────────────────

    async def list_incidents(self) -> list[dict[str, Any]]:
        """Fetch recent alert incidents from New Relic via NerdGraph."""
        result = await with_retry(self.client.get_incidents)
        try:
            incidents: list[dict[str, Any]] = (
                result
                .get("data", {})
                .get("actor", {})
                .get("account", {})
                .get("alerts", {})
                .get("incidents", {})
                .get("incidents", [])
            )
            return incidents if isinstance(incidents, list) else []
        except (AttributeError, TypeError):
            return []

    # ── Dashboards ────────────────────────────────────────────────────────────

    async def list_dashboards(self) -> list[dict[str, Any]]:
        """Fetch dashboards from New Relic via NerdGraph entity search."""
        result = await with_retry(self.client.get_dashboards)
        try:
            entities: list[dict[str, Any]] = (
                result
                .get("data", {})
                .get("actor", {})
                .get("entitySearch", {})
                .get("results", {})
                .get("entities", [])
            )
            return entities if isinstance(entities, list) else []
        except (AttributeError, TypeError):
            return []

    # ── NRQL ──────────────────────────────────────────────────────────────────

    async def run_nrql(self, nrql: str) -> dict[str, Any]:
        """Execute a NRQL query via NerdGraph and return the results.

        Args:
            nrql: A valid NRQL query string (e.g. "SELECT count(*) FROM Transaction SINCE 1 hour ago").

        Returns:
            NerdGraph response dict containing query results under data.actor.account.nrql.
        """
        query = """
        query($accountId: Int!, $nrql: Nrql!) {
          actor {
            account(id: $accountId) {
              nrql(query: $nrql) {
                results
                metadata {
                  eventTypes
                  facets
                  messages
                }
              }
            }
          }
        }
        """
        variables: dict[str, Any] = {
            "accountId": int(self._account_id) if self._account_id else 0,
            "nrql": nrql,
        }
        return await with_retry(self.client.run_nerdgraph, query, variables)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def __aenter__(self) -> NewRelicConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
