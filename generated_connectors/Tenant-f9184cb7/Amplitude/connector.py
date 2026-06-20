from __future__ import annotations

from typing import Any

from client import AmplitudeHTTPClient
from exceptions import AmplitudeAuthError, AmplitudeError, AmplitudeNetworkError
from helpers import CircuitBreaker, normalize_event_data, with_retry
from helpers.utils import normalize_cohort
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

# Default events to segment during sync
DEFAULT_SYNC_EVENTS: list[str] = [
    "Any Active Event",
    "Any Event",
]

CIRCUIT_BREAKER_THRESHOLD = 5

# Rolling 30-day window for sync (YYYYMMDD)
_SYNC_DAYS = 30


def _default_date_range() -> tuple[str, str]:
    """Return (start_date, end_date) strings as YYYYMMDD for the last 30 days."""
    import datetime

    today = datetime.date.today()
    start = today - datetime.timedelta(days=_SYNC_DAYS)
    return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")


class AmplitudeConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Amplitude Analytics.

    Provides authentication, health checks, full sync, and direct API access
    for event segmentation, cohorts, active users, and raw event export.

    Auth: HTTP Basic Auth — api_key as username, api_secret as password.
    Region: 'us' (default) or 'eu'.
    """

    CONNECTOR_TYPE: str = "amplitude"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        # Amplitude-specific attrs
        self._api_key: str = _config.get("api_key", "")
        self._api_secret: str = _config.get("api_secret", "")
        self._region: str = _config.get("region", "us").lower()
        self.http_client: AmplitudeHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> AmplitudeHTTPClient:
        return AmplitudeHTTPClient(
            api_key=self._api_key,
            api_secret=self._api_secret,
            region=self._region,
        )

    def _ensure_client(self) -> AmplitudeHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key and api_secret via GET /settings."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        if not self._api_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_secret is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_project_settings)
            await client.aclose()
            self.http_client = self._make_client()
            region_label = "EU" if self._region == "eu" else "US"
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Amplitude ({region_label} region)",
            )
        except AmplitudeAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Amplitude credentials: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /settings and return current health with project name."""
        if not self._api_key or not self._api_secret:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key and api_secret are required",
            )
        client = self._make_client()
        try:
            settings = await with_retry(client.get_project_settings)
            await client.aclose()
            self._circuit_breaker.on_success()
            region_label = "EU" if self._region == "eu" else "US"
            project_name: str = ""
            if isinstance(settings, dict):
                project_name = settings.get("projectName", settings.get("name", ""))
            name_part = f" — {project_name}" if project_name else ""
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Amplitude ({region_label} region){name_part}",
            )
        except AmplitudeAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except AmplitudeNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = (
                ConnectorHealth.DEGRADED
                if not self._circuit_breaker.is_open
                else ConnectorHealth.OFFLINE
            )
            return HealthCheckResult(
                health=health,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,  # noqa: ARG002
        since: Any = None,  # noqa: ARG002
        kb_id: str = "",
    ) -> SyncResult:
        """Sync event counts, cohorts, and active user data for the last 30 days."""
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        start_date, end_date = _default_date_range()
        import json as _json

        # Sync event segmentation for default events
        for event_type in DEFAULT_SYNC_EVENTS:
            try:
                event_param = _json.dumps({"event_type": event_type})
                data = await with_retry(
                    self.http_client.get_event_segmentation,
                    event_param,
                    start_date,
                    end_date,
                )
                docs = normalize_event_data(
                    event_type, data, self.connector_id, self.tenant_id, self._api_key
                )
                found += len(docs)
                for doc in docs:
                    try:
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except AmplitudeError:
                # Non-fatal: missing event type is common
                pass

        # Sync active users (DAU)
        try:
            active_data = await with_retry(
                self.http_client.get_active_users, start_date, end_date
            )
            active_docs = normalize_event_data(
                "active_users",
                active_data,
                self.connector_id,
                self.tenant_id,
                self._api_key,
            )
            found += len(active_docs)
            for doc in active_docs:
                try:
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except AmplitudeError:
            pass

        # Sync cohorts
        try:
            cohorts_resp = await with_retry(self.http_client.list_cohorts)
            cohorts: list[dict[str, Any]] = cohorts_resp.get("cohorts", [])
            found += len(cohorts)
            for cohort in cohorts:
                try:
                    doc = normalize_cohort(cohort, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except AmplitudeError:
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

    # ── Export events ─────────────────────────────────────────────────────────

    async def export_events(self, start_date: str, end_date: str) -> bytes:
        """Export raw event data as a ZIP archive.

        Args:
            start_date: Start timestamp YYYYMMDDTHH (e.g. '20240101T00').
            end_date:   End timestamp YYYYMMDDTHH (e.g. '20240101T23').

        Returns:
            Raw ZIP bytes from the Amplitude export API.
        """
        client = self._ensure_client()
        return await with_retry(client.export_events, start_date, end_date)

    # ── Event segmentation ────────────────────────────────────────────────────

    async def get_event_segmentation(
        self,
        event: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """GET /events/segmentation — event counts over a date range.

        Args:
            event:      Event type name (or JSON-encoded Amplitude event dict).
            start_date: Start date YYYYMMDD.
            end_date:   End date YYYYMMDD.

        Returns:
            Raw Amplitude response with series and xValues.
        """
        import json as _json

        # Accept plain event name or pre-encoded JSON
        try:
            _json.loads(event)
            event_param = event
        except (ValueError, TypeError):
            event_param = _json.dumps({"event_type": event})

        client = self._ensure_client()
        return await with_retry(
            client.get_event_segmentation, event_param, start_date, end_date
        )

    # ── Cohorts ───────────────────────────────────────────────────────────────

    async def list_cohorts(self) -> dict[str, Any]:
        """GET /cohorts — list all cohorts in the project."""
        client = self._ensure_client()
        return await with_retry(client.list_cohorts)

    async def get_cohort(self, cohort_id: str) -> dict[str, Any]:
        """GET /cohorts/{cohort_id}/members — list cohort member user IDs."""
        client = self._ensure_client()
        return await with_retry(client.get_cohort_members, cohort_id)

    # ── Active users ─────────────────────────────────────────────────────────

    async def get_active_users(self, start_date: str, end_date: str) -> dict[str, Any]:
        """GET /active — DAU/WAU/MAU data for the given date range.

        Args:
            start_date: Start date YYYYMMDD.
            end_date:   End date YYYYMMDD.

        Returns:
            Raw Amplitude response with series and xValues.
        """
        client = self._ensure_client()
        return await with_retry(client.get_active_users, start_date, end_date)

    # ── User activity ─────────────────────────────────────────────────────────

    async def get_user_activity(self, user_id: str) -> dict[str, Any]:
        """GET /usersearch?user={user_id} — event stream for a specific user.

        Args:
            user_id: Amplitude user ID or device ID to look up.

        Returns:
            Raw Amplitude user events response.
        """
        client = self._ensure_client()
        return await with_retry(client.get_user_activity, user_id)

    # ── Spec-required high-level methods ─────────────────────────────────────

    async def list_events(self, chart_id: str | None = None) -> list[dict[str, Any]]:
        """GET /taxonomy/event — list all event type dicts in the project.

        Args:
            chart_id: Optional chart ID filter.

        Returns:
            List of event type dicts from Amplitude taxonomy.
        """
        client = self._ensure_client()
        resp = await with_retry(client.list_events, chart_id)
        if isinstance(resp, dict):
            data = resp.get("data", resp.get("events", []))
            return data if isinstance(data, list) else []
        return []

    async def list_user_properties(self) -> list[dict[str, Any]]:
        """GET /taxonomy/user-property — list all user property dicts.

        Returns:
            List of user property dicts from Amplitude taxonomy.
        """
        client = self._ensure_client()
        resp = await with_retry(client.list_user_properties)
        if isinstance(resp, dict):
            data = resp.get("data", resp.get("properties", []))
            return data if isinstance(data, list) else []
        return []

    async def list_charts(self) -> list[dict[str, Any]]:
        """GET /chart/list — list user-accessible charts/dashboards.

        Returns:
            List of chart dicts.
        """
        client = self._ensure_client()
        resp = await with_retry(client.list_charts)
        if isinstance(resp, dict):
            data = resp.get("data", resp.get("charts", []))
            return data if isinstance(data, list) else []
        return []

    async def query_event_counts(
        self,
        event: str,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """GET /events/segmentation — segmentation data for a single event type.

        Args:
            event: Plain event type name (e.g. 'PageView').
            start: Start date YYYYMMDD (defaults to 30 days ago).
            end:   End date YYYYMMDD (defaults to today).

        Returns:
            Raw Amplitude segmentation response dict.
        """
        if start is None or end is None:
            _start, _end = _default_date_range()
            start = start or _start
            end = end or _end
        client = self._ensure_client()
        return await with_retry(client.query_event_counts, event, start, end)

    async def get_funnel(self, funnel_id: str) -> dict[str, Any]:
        """GET /funnels?funnel_id={funnel_id} — retrieve a saved funnel by ID.

        Args:
            funnel_id: The Amplitude funnel ID string.

        Returns:
            Raw Amplitude funnel response dict.
        """
        client = self._ensure_client()
        return await with_retry(client.get_funnel, funnel_id)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> AmplitudeConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
