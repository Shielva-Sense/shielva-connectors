"""Mixpanel connector — Shielva connector implementation."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from client.http_client import MixpanelHTTPClient
from exceptions import MixpanelAuthError, MixpanelError, MixpanelNetworkError
from helpers.utils import normalize_event, with_retry
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


CONNECTOR_TYPE: str = "mixpanel"
AUTH_TYPE: str = "api_key"

_DEFAULT_SYNC_DAYS: int = 30
_DATE_FMT: str = "%Y-%m-%d"


class MixpanelConnector(BaseConnector):
    """Shielva connector for Mixpanel.

    Auth: HTTP Basic Auth — username = service account username,
                            password = service account secret.

    Install fields:
        username    — Mixpanel Service Account Username  (required)
        secret      — Mixpanel Service Account Secret    (required, password)
        project_id  — Mixpanel Project ID                (required)
        region      — "US" or "EU"                       (optional, default "US")
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
            tenant_id=tenant_id,
            connector_id=connector_id,
            config=_config,
        )
        self._username: str = _config.get("username", "")
        self._secret: str = _config.get("secret", "")
        self._project_id: str = _config.get("project_id", "")
        self._region: str = (_config.get("region", "") or "US").upper()

    def _missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self._username:
            missing.append("username")
        if not self._secret:
            missing.append("secret")
        if not self._project_id:
            missing.append("project_id")
        return missing

    def _make_client(self) -> MixpanelHTTPClient:
        return MixpanelHTTPClient(
            username=self._username,
            secret=self._secret,
            project_id=self._project_id,
            region=self._region,
        )

    def _default_date_range(self, days: int = _DEFAULT_SYNC_DAYS) -> tuple[str, str]:
        to_dt = datetime.utcnow()
        from_dt = to_dt - timedelta(days=days)
        return from_dt.strftime(_DATE_FMT), to_dt.strftime(_DATE_FMT)

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling get_projects().

        Returns InstallResult with HEALTHY/CONNECTED on success.
        """
        missing = self._missing_fields()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            await client.get_projects()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=(
                    f"Connected to Mixpanel as {self._username} "
                    f"(project: {self._project_id})"
                ),
            )
        except MixpanelAuthError as exc:
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

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Probe get_projects() and return current health status."""
        missing = self._missing_fields()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            await client.get_projects()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Mixpanel API reachable. Project: {self._project_id}",
            )
        except MixpanelAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except MixpanelNetworkError as exc:
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

    # ── sync ──────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Query events for last 30 days, normalize, return SyncResult."""
        from_date, to_date = self._default_date_range()

        client = self._make_client()
        documents: list[ConnectorDocument] = []
        messages: list[str] = []
        failed = 0

        try:
            events = await self.query_events(from_date=from_date, to_date=to_date)
            for raw in events:
                try:
                    doc = normalize_event(raw)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    documents.append(doc)
                except Exception as exc:
                    failed += 1
                    messages.append(f"Normalization error: {exc}")
        except MixpanelError as exc:
            messages.append(f"Event query failed: {exc}")
            failed += 1

        found = len(documents) + failed
        synced = len(documents)

        if found == 0 and failed == 0:
            status = SyncStatus.COMPLETED
        elif failed > 0 and synced == 0:
            status = SyncStatus.FAILED
        elif failed > 0:
            status = SyncStatus.PARTIAL
        else:
            status = SyncStatus.COMPLETED

        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
            message="; ".join(messages) if messages else "",
        )

    # ── query_events ──────────────────────────────────────────────────────────

    async def query_events(
        self,
        event_names: list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Export raw events via NDJSON endpoint, return as a list of event dicts."""
        client = self._make_client()
        gen = await client.query_events(
            event_names=event_names,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
        results: list[dict[str, Any]] = []
        async for event in gen:
            results.append(event)
        return results

    # ── list_funnels ──────────────────────────────────────────────────────────

    async def list_funnels(self) -> list[dict[str, Any]]:
        """Return list of funnel dicts from the Mixpanel project."""
        client = self._make_client()
        response = await with_retry(client.list_funnels)
        results = response.get("results", response)
        if isinstance(results, list):
            return results
        return [results] if results else []

    # ── query_funnel ──────────────────────────────────────────────────────────

    async def query_funnel(
        self,
        funnel_id: int | str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Return funnel data dict for the given funnel_id."""
        if from_date is None or to_date is None:
            from_date, to_date = self._default_date_range()
        client = self._make_client()
        return await with_retry(
            client.query_funnels,
            funnel_id,
            from_date,
            to_date,
        )

    # ── query_segmentation ────────────────────────────────────────────────────

    async def query_segmentation(
        self,
        event: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Return segmentation data dict for the given event."""
        if from_date is None or to_date is None:
            from_date, to_date = self._default_date_range()
        client = self._make_client()
        return await with_retry(
            client.query_segmentation,
            event,
            from_date,
            to_date,
        )
