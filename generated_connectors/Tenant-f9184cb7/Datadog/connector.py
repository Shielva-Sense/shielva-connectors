from __future__ import annotations

import time
from typing import Any

from client.http_client import DatadogHTTPClient
from exceptions import DatadogAuthError, DatadogError, DatadogNetworkError
from helpers.utils import (
    normalize_dashboard,
    normalize_event,
    normalize_host,
    normalize_monitor,
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

CONNECTOR_TYPE = "datadog"
AUTH_TYPE = "api_key"

# Sync window for events: last 24 hours
_EVENT_WINDOW_S: int = 24 * 60 * 60

# Maximum pages to pull during a full sync (safety cap)
_MAX_MONITOR_PAGES: int = 20
_MAX_HOST_PAGES: int = 20
_MAX_EVENT_PAGES: int = 5


class DatadogConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Datadog monitoring and observability.

    Provides authentication, health checks, full sync, and direct API access
    for monitors, dashboards, incidents, hosts, and events.

    Auth: Dual-key — DD-API-KEY + DD-APPLICATION-KEY headers on every request.
    Site: datadoghq.com (US, default), datadoghq.eu (EU), us3.datadoghq.com.
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
        self._app_key: str = _config.get("app_key", "")
        self._site: str = _config.get("site", "datadoghq.com") or "datadoghq.com"
        self.client: DatadogHTTPClient = DatadogHTTPClient(config=self.config)

    def _make_client(self) -> DatadogHTTPClient:
        return DatadogHTTPClient(config=self.config)

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key and app_key via GET /v1/validate."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        if not self._app_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="app_key is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.validate)
            await client.aclose()
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Datadog ({self._site})",
            )
        except DatadogAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Datadog credentials: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /v1/validate and return current health."""
        if not self._api_key or not self._app_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key and app_key are required",
            )
        client = self._make_client()
        try:
            await with_retry(client.validate)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Datadog ({self._site})",
            )
        except DatadogAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except DatadogNetworkError as exc:
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
        """Sync monitors, dashboards, hosts, and events from Datadog."""
        found = 0
        synced = 0
        failed = 0

        # Sync monitors
        try:
            monitors = await self.list_monitors()
            found += len(monitors)
            for raw in monitors:
                try:
                    doc = normalize_monitor(
                        raw, connector_id=self.connector_id, tenant_id=self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except DatadogError:
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
        except DatadogError:
            pass

        # Sync hosts
        try:
            hosts = await self.list_hosts()
            found += len(hosts)
            for raw in hosts:
                try:
                    doc = normalize_host(
                        raw, connector_id=self.connector_id, tenant_id=self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except DatadogError:
            pass

        # Sync events (last 24 hours)
        try:
            events = await self.list_events()
            found += len(events)
            for raw in events:
                try:
                    doc = normalize_event(
                        raw, connector_id=self.connector_id, tenant_id=self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except DatadogError:
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

    # ── Monitors ──────────────────────────────────────────────────────────────

    async def list_monitors(
        self,
        tags: list[str] | None = None,
        page_size: int = 100,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch all monitors from Datadog, paginating automatically.

        Args:
            tags:      Optional list of tag filters (e.g. ["env:prod", "team:infra"]).
            page_size: Number of monitors per page (max 1000).
        """
        all_monitors: list[dict[str, Any]] = []
        for page in range(_MAX_MONITOR_PAGES):
            batch = await with_retry(
                self.client.get_monitors,
                page=page,
                page_size=page_size,
                tags=tags,
            )
            if not batch:
                break
            all_monitors.extend(batch)
            if len(batch) < page_size:
                break
        return all_monitors

    async def get_monitor(self, monitor_id: int) -> dict[str, Any]:
        """Retrieve a single Datadog monitor by ID."""
        return await with_retry(self.client.get_monitor, monitor_id)

    # ── Dashboards ────────────────────────────────────────────────────────────

    async def list_dashboards(self) -> list[dict[str, Any]]:
        """Fetch all dashboards from Datadog."""
        result = await with_retry(self.client.get_dashboards)
        dashboards: list[dict[str, Any]] = result.get("dashboards", [])
        return dashboards

    async def get_dashboard(self, dashboard_id: str) -> dict[str, Any]:
        """Retrieve a single Datadog dashboard by ID."""
        return await with_retry(self.client.get_dashboard, dashboard_id)

    # ── Hosts ─────────────────────────────────────────────────────────────────

    async def list_hosts(self, count: int = 100, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch all hosts from Datadog, paginating automatically."""
        all_hosts: list[dict[str, Any]] = []
        for page in range(_MAX_HOST_PAGES):
            start = page * count
            result = await with_retry(self.client.get_hosts, count=count, start=start)
            host_list: list[dict[str, Any]] = result.get("host_list", [])
            if not host_list:
                break
            all_hosts.extend(host_list)
            if len(host_list) < count:
                break
        return all_hosts

    # ── Events ────────────────────────────────────────────────────────────────

    async def list_events(
        self,
        start: int | None = None,
        end: int | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch events from Datadog for the last 24 hours (default) or a custom range."""
        now = int(time.time())
        end_ts = end if end is not None else now
        start_ts = start if start is not None else (now - _EVENT_WINDOW_S)

        all_events: list[dict[str, Any]] = []
        for page in range(_MAX_EVENT_PAGES):
            result = await with_retry(
                self.client.get_events, start=start_ts, end=end_ts, page=page
            )
            events: list[dict[str, Any]] = result.get("events", [])
            if not events:
                break
            all_events.extend(events)
            if len(events) < 1000:  # Datadog max per page
                break
        return all_events

    # ── Logs (v2) ─────────────────────────────────────────────────────────────

    async def query_logs(
        self,
        query: str,
        from_ts: int | None = None,
        to_ts: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search Datadog log events via the v2 Logs API.

        Handles cursor-based pagination automatically, collecting up to ``limit`` events.
        If ``from_ts`` or ``to_ts`` are omitted, defaults to the last 24 hours.

        Args:
            query:   Datadog log search query string (e.g. 'service:api status:error').
            from_ts: Start epoch timestamp (seconds). Defaults to 24h ago.
            to_ts:   End epoch timestamp (seconds). Defaults to now.
            limit:   Maximum number of log events to return (default 100, max 1000).

        Returns:
            List of log event dicts (Datadog v2 log event objects).
        """
        import time as _time
        now = int(_time.time())
        end_ts = to_ts if to_ts is not None else now
        start_ts = from_ts if from_ts is not None else (now - _EVENT_WINDOW_S)

        all_logs: list[dict[str, Any]] = []
        cursor: str | None = None
        page_limit = min(limit, 1000)

        while len(all_logs) < limit:
            result = await with_retry(
                self.client.list_logs,
                query=query,
                from_ts=start_ts,
                to_ts=end_ts,
                limit=page_limit,
                cursor=cursor,
            )
            events: list[dict[str, Any]] = result.get("data", [])
            all_logs.extend(events)

            meta: dict[str, Any] = result.get("meta", {})
            next_cursor: str | None = meta.get("page", {}).get("after")
            if not next_cursor or not events:
                break
            cursor = next_cursor

        return all_logs[:limit]

    # ── Metrics ───────────────────────────────────────────────────────────────

    async def get_metrics_list(self, q: str) -> list[str]:
        """Search available Datadog metrics by name query.

        Args:
            q: Metric name prefix or search string (e.g. 'system' or 'aws.ec2').

        Returns:
            List of metric name strings matching the query.
        """
        result = await with_retry(self.client.get_metrics_list, q)
        metrics: list[str] = result.get("metrics", [])
        return metrics

    # ── Service Checks ────────────────────────────────────────────────────────

    async def list_service_checks(self) -> list[dict[str, Any]]:
        """List all Datadog service check results.

        Returns:
            List of service check result dicts.
        """
        return await with_retry(self.client.list_service_checks)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def __aenter__(self) -> DatadogConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
