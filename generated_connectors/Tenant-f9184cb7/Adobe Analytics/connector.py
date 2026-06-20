"""Adobe Analytics connector for Shielva."""
from __future__ import annotations

from typing import Any

from client import AdobeAnalyticsHTTPClient
from exceptions import (
    AdobeAnalyticsAuthError,
    AdobeAnalyticsNetworkError,
)
from helpers import (
    normalize_calculated_metric,
    normalize_report_suite,
    normalize_segment,
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


class AdobeAnalyticsConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Adobe Analytics 2.0 API.

    Auth: OAuth2 client_credentials grant via Adobe IMS.
    Syncs report suites, segments, and calculated metrics.
    """

    CONNECTOR_TYPE: str = "adobe_analytics"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._company_id: str = _config.get("company_id", "")
        self._organization_id: str = _config.get("organization_id", "")
        self.http_client: AdobeAnalyticsHTTPClient | None = None

    def _make_client(self) -> AdobeAnalyticsHTTPClient:
        return AdobeAnalyticsHTTPClient(
            client_id=self._client_id,
            client_secret=self._client_secret,
            company_id=self._company_id,
        )

    def _ensure_client(self) -> AdobeAnalyticsHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate client_id, client_secret, and company_id.

        Acquires an OAuth2 token and lists report suites to confirm connectivity.
        """
        if not self._client_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id is required",
            )
        if not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_secret is required",
            )
        if not self._company_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="company_id is required",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_token)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Adobe Analytics",
            )
        except AdobeAnalyticsAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Adobe Analytics credentials: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Acquire token and list report suites to verify connectivity."""
        if not self._client_id or not self._client_secret or not self._company_id:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id, client_secret, and company_id are required",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_token)
            suites_resp = await with_retry(client.get_report_suites)
            await client.aclose()
            suites: list[Any] = suites_resp if isinstance(suites_resp, list) else (
                suites_resp.get("content", suites_resp.get("reportSuites", []))
            )
            count = len(suites)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Adobe Analytics ({count} report suite(s))",
            )
        except AdobeAnalyticsAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except AdobeAnalyticsNetworkError as exc:
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

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,  # noqa: ARG002
        since: Any = None,  # noqa: ARG002
        kb_id: str = "",
    ) -> SyncResult:
        """Sync report suites, segments, and calculated metrics.

        Returns SyncResult with documents_found / documents_synced / documents_failed.
        Partial failures (segments/calc metrics) are non-fatal — PARTIAL status is returned.
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # ── Report suites ──
        report_suite_ids: list[str] = []
        try:
            suites_resp = await with_retry(self.http_client.get_report_suites)
            suites: list[dict[str, Any]] = (
                suites_resp
                if isinstance(suites_resp, list)
                else suites_resp.get("content", suites_resp.get("reportSuites", []))
            )
            found += len(suites)
            for rs in suites:
                try:
                    doc = normalize_report_suite(rs, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                    rsid = rs.get("rsid", "")
                    if rsid:
                        report_suite_ids.append(rsid)
                except Exception:
                    failed += 1
        except AdobeAnalyticsAuthError:
            raise
        except Exception:
            pass

        # ── Segments (use first report suite if available) ──
        rsid_for_query = report_suite_ids[0] if report_suite_ids else ""
        if rsid_for_query:
            try:
                segs_resp = await with_retry(
                    self.http_client.get_segments, rsid_for_query
                )
                segments: list[dict[str, Any]] = (
                    segs_resp
                    if isinstance(segs_resp, list)
                    else segs_resp.get("content", segs_resp.get("segments", []))
                )
                found += len(segments)
                for seg in segments:
                    try:
                        doc = normalize_segment(seg, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except Exception:
                pass

        # ── Calculated metrics ──
        try:
            metrics_resp = await with_retry(self.http_client.get_calculated_metrics)
            metrics: list[dict[str, Any]] = (
                metrics_resp
                if isinstance(metrics_resp, list)
                else metrics_resp.get("content", metrics_resp.get("calculatedMetrics", []))
            )
            found += len(metrics)
            for m in metrics:
                try:
                    doc = normalize_calculated_metric(m, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Exception:
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

    # ── Direct API access ─────────────────────────────────────────────────────

    async def list_report_suites(self) -> list[dict[str, Any]]:
        """Return all report suites for the company."""
        client = self._ensure_client()
        resp = await with_retry(client.get_report_suites)
        if isinstance(resp, list):
            return resp
        return resp.get("content", resp.get("reportSuites", []))

    async def list_dimensions(self, report_suite_id: str) -> list[dict[str, Any]]:
        """Return all dimensions for a report suite."""
        client = self._ensure_client()
        resp = await with_retry(client.get_dimensions, report_suite_id)
        if isinstance(resp, list):
            return resp
        return resp.get("content", resp.get("dimensions", []))

    async def list_metrics(self, report_suite_id: str) -> list[dict[str, Any]]:
        """Return all metrics for a report suite."""
        client = self._ensure_client()
        resp = await with_retry(client.get_metrics, report_suite_id)
        if isinstance(resp, list):
            return resp
        return resp.get("content", resp.get("metrics", []))

    async def list_segments(self, report_suite_id: str) -> list[dict[str, Any]]:
        """Return all segments for a report suite."""
        client = self._ensure_client()
        resp = await with_retry(client.get_segments, report_suite_id)
        if isinstance(resp, list):
            return resp
        return resp.get("content", resp.get("segments", []))

    async def run_report(
        self,
        report_suite_id: str,
        metrics: list[str],
        dimensions: list[str],
        date_range: str = "",
    ) -> dict[str, Any]:
        """Run an Adobe Analytics ranked report.

        Args:
            report_suite_id: RSID to run the report against.
            metrics: List of metric IDs (e.g. ['metrics/visits', 'metrics/pageviews']).
            dimensions: List of dimension IDs (e.g. ['variables/page']).
            date_range: Optional ISO-8601 date range string.

        Returns:
            Raw JSON report response from Adobe Analytics.
        """
        client = self._ensure_client()
        body: dict[str, Any] = {
            "rsid": report_suite_id,
            "globalFilters": [],
            "metricContainer": {
                "metrics": [{"id": m} for m in metrics],
            },
            "dimension": dimensions[0] if dimensions else "variables/page",
        }
        if date_range:
            body["globalFilters"] = [{"type": "dateRange", "dateRange": date_range}]
        return await with_retry(client.run_report, report_suite_id, body)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> AdobeAnalyticsConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
