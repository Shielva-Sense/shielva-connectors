"""
Comprehensive unit test suite for the Microsoft Power BI connector.
60+ tests — no live network calls.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make the connector root importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent))

from exceptions import (
    PowerBIAuthError,
    PowerBIError,
    PowerBINetworkError,
    PowerBINotFoundError,
    PowerBIRateLimitError,
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
from helpers.utils import (
    normalize_dashboard,
    normalize_dataset,
    normalize_report,
    with_retry,
)
from client.http_client import PowerBIHTTPClient
from connector import PowerBIConnector, CONNECTOR_TYPE, AUTH_TYPE


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

BASE_CONFIG: dict[str, Any] = {
    "client_id": "CLIENT_ID",
    "client_secret": "CLIENT_SECRET",
    "tenant_id_azure": "TENANT_UUID",
    "redirect_uri": "https://app.example.com/callback",
    "access_token": "ACCESS_TOKEN",
    "refresh_token": "REFRESH_TOKEN",
    "token_expires_at": time.monotonic() + 3600,
}


def _make_connector(**overrides: Any) -> PowerBIConnector:
    cfg = {**BASE_CONFIG, **overrides}
    return PowerBIConnector(
        tenant_id="shielva-tenant",
        connector_id="conn-powerbi-01",
        config=cfg,
    )


def _raw_dashboard(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "dash-0000-0000-0000-000000000001",
        "displayName": "Sales Overview",
        "isReadOnly": False,
        "webUrl": "https://app.powerbi.com/dashboards/dash-0000-0000-0000-000000000001",
        "embedUrl": "https://app.powerbi.com/dashboardEmbed?dashboardId=dash-0000-0000-0000-000000000001",
        "workspaceId": "ws-0000-0000-0000-000000000001",
    }
    base.update(overrides)
    return base


def _raw_report(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "report-0000-0000-0000-000000000001",
        "name": "Q3 Revenue Report",
        "reportType": "PowerBIReport",
        "webUrl": "https://app.powerbi.com/reports/report-0000-0000-0000-000000000001",
        "embedUrl": "https://app.powerbi.com/reportEmbed?reportId=report-0000-0000-0000-000000000001",
        "datasetId": "ds-0000-0000-0000-000000000001",
        "workspaceId": "ws-0000-0000-0000-000000000001",
    }
    base.update(overrides)
    return base


def _raw_dataset(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "ds-0000-0000-0000-000000000001",
        "name": "Sales Dataset",
        "configuredBy": "admin@example.com",
        "isRefreshable": True,
        "isOnPremGatewayRequired": False,
        "targetStorageMode": "Import",
        "webUrl": "https://app.powerbi.com/datasets/ds-0000-0000-0000-000000000001",
        "workspaceId": "ws-0000-0000-0000-000000000001",
    }
    base.update(overrides)
    return base


def _raw_workspace(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "ws-0000-0000-0000-000000000001",
        "name": "Sales Team",
        "type": "Workspace",
        "isReadOnly": False,
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# 1. Exception hierarchy (7 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptions:
    def test_base_error_instantiation(self) -> None:
        exc = PowerBIError("something went wrong", status_code=400, code="BAD")
        assert str(exc) == "[400] something went wrong"
        assert exc.status_code == 400
        assert exc.code == "BAD"

    def test_base_error_no_status(self) -> None:
        exc = PowerBIError("oops")
        assert str(exc) == "oops"
        assert exc.status_code == 0
        assert exc.code == ""

    def test_auth_error_is_base(self) -> None:
        exc = PowerBIAuthError("invalid token", status_code=401)
        assert isinstance(exc, PowerBIError)
        assert exc.status_code == 401

    def test_network_error_is_base(self) -> None:
        exc = PowerBINetworkError("timeout", status_code=503)
        assert isinstance(exc, PowerBIError)
        assert "[503]" in str(exc)

    def test_not_found_error_with_id(self) -> None:
        exc = PowerBINotFoundError("dashboard", "dash-abc-123")
        assert isinstance(exc, PowerBIError)
        assert exc.status_code == 404
        assert "dash-abc-123" in str(exc)
        assert exc.code == "NOT_FOUND"

    def test_not_found_error_no_id(self) -> None:
        exc = PowerBINotFoundError("report")
        assert "not found" in str(exc)
        assert exc.status_code == 404

    def test_rate_limit_error(self) -> None:
        exc = PowerBIRateLimitError("slow down", retry_after=30.0)
        assert isinstance(exc, PowerBIError)
        assert exc.status_code == 429
        assert exc.retry_after == 30.0
        assert exc.code == "rate_limit"

    def test_rate_limit_error_default_retry(self) -> None:
        exc = PowerBIRateLimitError("rate limited")
        assert exc.retry_after == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Model dataclasses (6 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestModels:
    def test_install_result_defaults(self) -> None:
        r = InstallResult(success=True, message="ok")
        assert r.connector_type == "powerbi"
        assert r.success is True
        assert r.connector_id == ""
        assert r.health == ConnectorHealth.OFFLINE
        assert r.auth_status == AuthStatus.FAILED

    def test_install_result_full(self) -> None:
        r = InstallResult(
            success=True,
            message="connected",
            connector_type="powerbi",
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="conn-01",
        )
        assert r.connector_id == "conn-01"
        assert r.health == ConnectorHealth.HEALTHY

    def test_health_check_result(self) -> None:
        r = HealthCheckResult(healthy=True, message="ok", details={"x": 1})
        assert r.healthy is True
        assert r.details == {"x": 1}
        assert r.health == ConnectorHealth.OFFLINE

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(success=True)
        assert r.documents == []
        assert r.documents_found == 0
        assert r.status == SyncStatus.COMPLETED

    def test_connector_document(self) -> None:
        doc = ConnectorDocument(
            id="abc123",
            source="powerbi",
            type="report",
            title="My Report",
            content="Report: My Report",
        )
        assert doc.id == "abc123"
        assert doc.metadata == {}
        assert doc.synced_at == ""
        assert doc.source_url == ""

    def test_enum_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert AuthStatus.CONNECTED == "connected"
        assert SyncStatus.PARTIAL == "partial"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Normalizers (12 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeDashboard:
    def test_basic_normalization(self) -> None:
        raw = _raw_dashboard()
        doc = normalize_dashboard(raw)
        assert doc.type == "dashboard"
        assert doc.source == "powerbi"
        assert doc.title == "Sales Overview"
        assert "Dashboard: Sales Overview" in doc.content

    def test_stable_id(self) -> None:
        raw = _raw_dashboard()
        doc1 = normalize_dashboard(raw)
        doc2 = normalize_dashboard(raw)
        assert doc1.id == doc2.id
        assert len(doc1.id) == 16

    def test_id_prefix_dashboard(self) -> None:
        import hashlib
        raw = _raw_dashboard()
        raw_id = raw["id"]
        expected = hashlib.sha256(f"dashboard:{raw_id}".encode()).hexdigest()[:16]
        doc = normalize_dashboard(raw)
        assert doc.id == expected

    def test_metadata_fields(self) -> None:
        raw = _raw_dashboard()
        doc = normalize_dashboard(raw)
        assert doc.metadata["dashboard_id"] == raw["id"]
        assert doc.metadata["is_read_only"] is False
        assert doc.metadata["workspace_id"] == raw["workspaceId"]

    def test_web_url_as_source_url(self) -> None:
        raw = _raw_dashboard()
        doc = normalize_dashboard(raw)
        assert doc.source_url == raw["webUrl"]

    def test_missing_optional_fields(self) -> None:
        raw = {"id": "dash-minimal"}
        doc = normalize_dashboard(raw)
        assert doc.title == "Untitled Dashboard"
        assert doc.source_url == ""
        assert doc.metadata["workspace_id"] == ""

    def test_read_only_in_content(self) -> None:
        raw = _raw_dashboard(isReadOnly=True)
        doc = normalize_dashboard(raw)
        assert "Read-only: Yes" in doc.content

    def test_synced_at_set(self) -> None:
        raw = _raw_dashboard()
        doc = normalize_dashboard(raw)
        assert doc.synced_at != ""


class TestNormalizeReport:
    def test_basic_normalization(self) -> None:
        raw = _raw_report()
        doc = normalize_report(raw)
        assert doc.type == "report"
        assert doc.source == "powerbi"
        assert doc.title == "Q3 Revenue Report"
        assert "Report: Q3 Revenue Report" in doc.content

    def test_stable_id(self) -> None:
        import hashlib
        raw = _raw_report()
        raw_id = raw["id"]
        expected = hashlib.sha256(f"report:{raw_id}".encode()).hexdigest()[:16]
        doc = normalize_report(raw)
        assert doc.id == expected

    def test_metadata_fields(self) -> None:
        raw = _raw_report()
        doc = normalize_report(raw)
        assert doc.metadata["report_id"] == raw["id"]
        assert doc.metadata["report_type"] == "PowerBIReport"
        assert doc.metadata["dataset_id"] == raw["datasetId"]
        assert doc.metadata["workspace_id"] == raw["workspaceId"]

    def test_missing_optional_fields(self) -> None:
        raw = {"id": "report-minimal"}
        doc = normalize_report(raw)
        assert doc.title == "Untitled Report"
        assert doc.metadata["dataset_id"] == ""
        assert doc.source_url == ""

    def test_type_in_content(self) -> None:
        raw = _raw_report()
        doc = normalize_report(raw)
        assert "Type: PowerBIReport" in doc.content


class TestNormalizeDataset:
    def test_basic_normalization(self) -> None:
        raw = _raw_dataset()
        doc = normalize_dataset(raw)
        assert doc.type == "dataset"
        assert doc.source == "powerbi"
        assert doc.title == "Sales Dataset"
        assert "Dataset: Sales Dataset" in doc.content

    def test_stable_id(self) -> None:
        import hashlib
        raw = _raw_dataset()
        raw_id = raw["id"]
        expected = hashlib.sha256(f"dataset:{raw_id}".encode()).hexdigest()[:16]
        doc = normalize_dataset(raw)
        assert doc.id == expected

    def test_metadata_fields(self) -> None:
        raw = _raw_dataset()
        doc = normalize_dataset(raw)
        assert doc.metadata["dataset_id"] == raw["id"]
        assert doc.metadata["configured_by"] == "admin@example.com"
        assert doc.metadata["is_refreshable"] is True
        assert doc.metadata["target_storage_mode"] == "Import"

    def test_missing_optional_fields(self) -> None:
        raw = {"id": "ds-minimal"}
        doc = normalize_dataset(raw)
        assert doc.title == "Untitled Dataset"
        assert doc.metadata["configured_by"] == ""

    def test_on_prem_gateway_in_content(self) -> None:
        raw = _raw_dataset(isOnPremGatewayRequired=True)
        doc = normalize_dataset(raw)
        assert "On-premises gateway required: Yes" in doc.content

    def test_refreshable_in_content(self) -> None:
        raw = _raw_dataset(isRefreshable=False)
        doc = normalize_dataset(raw)
        assert "Refreshable: No" in doc.content


# ─────────────────────────────────────────────────────────────────────────────
# 4. with_retry (6 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"value": []})
        result = await with_retry(fn)
        assert result == {"value": []}
        assert fn.call_count == 1

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(side_effect=[
            PowerBINetworkError("timeout"),
            PowerBINetworkError("timeout"),
            {"value": []},
        ])
        result = await with_retry(fn, base_delay=0.0)
        assert result == {"value": []}
        assert fn.call_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=PowerBIAuthError("unauthorized", status_code=401))
        with pytest.raises(PowerBIAuthError):
            await with_retry(fn)
        assert fn.call_count == 1

    async def test_exhausted_retries_raises_last_error(self) -> None:
        fn = AsyncMock(side_effect=PowerBINetworkError("server error", status_code=500))
        with pytest.raises(PowerBINetworkError):
            await with_retry(fn, max_attempts=2, base_delay=0.0)
        assert fn.call_count == 2

    async def test_rate_limit_uses_retry_after(self) -> None:
        exc = PowerBIRateLimitError("rate limited", retry_after=0.0)
        fn = AsyncMock(side_effect=[exc, {"value": []}])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_retry(fn, base_delay=0.0)
        assert result == {"value": []}
        assert fn.call_count == 2

    async def test_rate_limit_exhausted(self) -> None:
        exc = PowerBIRateLimitError("rate limited", retry_after=0.0)
        fn = AsyncMock(side_effect=exc)
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(PowerBIRateLimitError):
                await with_retry(fn, max_attempts=2, base_delay=0.0)
        assert fn.call_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# 5. PowerBIHTTPClient (16 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestPowerBIHTTPClient:
    def _make_client(self, **overrides: Any) -> PowerBIHTTPClient:
        cfg = {**BASE_CONFIG, **overrides}
        return PowerBIHTTPClient(config=cfg)

    def test_bearer_header(self) -> None:
        client = self._make_client()
        headers = client._auth_headers()
        assert headers["Authorization"] == "Bearer ACCESS_TOKEN"
        assert headers["Accept"] == "application/json"

    async def test_get_dashboards(self) -> None:
        client = self._make_client()
        dashboards = [_raw_dashboard()]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"value": dashboards})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_ensure_token", new_callable=AsyncMock):
            with patch.object(client._get_session(), "request", return_value=mock_resp):
                result = await client.get_dashboards()
        assert result == dashboards

    async def test_get_reports(self) -> None:
        client = self._make_client()
        reports = [_raw_report()]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"value": reports})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_ensure_token", new_callable=AsyncMock):
            with patch.object(client._get_session(), "request", return_value=mock_resp):
                result = await client.get_reports()
        assert result == reports

    async def test_get_datasets(self) -> None:
        client = self._make_client()
        datasets = [_raw_dataset()]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"value": datasets})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_ensure_token", new_callable=AsyncMock):
            with patch.object(client._get_session(), "request", return_value=mock_resp):
                result = await client.get_datasets()
        assert result == datasets

    async def test_get_workspaces(self) -> None:
        client = self._make_client()
        workspaces = [_raw_workspace()]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"value": workspaces})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_ensure_token", new_callable=AsyncMock):
            with patch.object(client._get_session(), "request", return_value=mock_resp):
                result = await client.get_workspaces()
        assert result == workspaces

    async def test_get_workspace_reports(self) -> None:
        client = self._make_client()
        reports = [_raw_report()]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"value": reports})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_ensure_token", new_callable=AsyncMock):
            with patch.object(client._get_session(), "request", return_value=mock_resp):
                result = await client.get_workspace_reports("ws-001")
        assert result == reports

    async def test_get_workspace_dashboards(self) -> None:
        client = self._make_client()
        dashboards = [_raw_dashboard()]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"value": dashboards})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_ensure_token", new_callable=AsyncMock):
            with patch.object(client._get_session(), "request", return_value=mock_resp):
                result = await client.get_workspace_dashboards("ws-001")
        assert result == dashboards

    def test_raise_for_status_401(self) -> None:
        with pytest.raises(PowerBIAuthError) as exc_info:
            PowerBIHTTPClient._raise_for_status(401, {"message": "Unauthorized"})
        assert exc_info.value.status_code == 401

    def test_raise_for_status_403(self) -> None:
        with pytest.raises(PowerBIAuthError) as exc_info:
            PowerBIHTTPClient._raise_for_status(403, {"message": "Forbidden"})
        assert exc_info.value.status_code == 403

    def test_raise_for_status_404(self) -> None:
        with pytest.raises(PowerBINotFoundError):
            PowerBIHTTPClient._raise_for_status(404, {})

    def test_raise_for_status_429(self) -> None:
        with pytest.raises(PowerBIRateLimitError):
            PowerBIHTTPClient._raise_for_status(429, {"message": "Too many requests"})

    def test_raise_for_status_500(self) -> None:
        with pytest.raises(PowerBINetworkError) as exc_info:
            PowerBIHTTPClient._raise_for_status(500, {"message": "Internal Server Error"})
        assert exc_info.value.status_code == 500

    def test_raise_for_status_generic_error(self) -> None:
        with pytest.raises(PowerBIError) as exc_info:
            PowerBIHTTPClient._raise_for_status(400, {"message": "Bad Request"})
        assert exc_info.value.status_code == 400

    def test_token_not_expired_with_future_expiry(self) -> None:
        client = self._make_client(token_expires_at=time.monotonic() + 3600)
        assert client._is_token_expired() is False

    def test_token_expired_with_past_expiry(self) -> None:
        client = self._make_client(token_expires_at=time.monotonic() - 1)
        assert client._is_token_expired() is True

    def test_token_expired_no_access_token(self) -> None:
        client = self._make_client(access_token="")
        assert client._is_token_expired() is True

    async def test_refresh_token_updates_access_token(self) -> None:
        client = self._make_client()
        client._refresh_token = "REFRESH_TOKEN"
        client._az_tenant_id = "TENANT_UUID"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "access_token": "NEW_ACCESS_TOKEN",
            "refresh_token": "NEW_REFRESH_TOKEN",
            "expires_in": 3600,
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client._get_session(), "post", return_value=mock_resp):
            result = await client.refresh_token()

        assert client._access_token == "NEW_ACCESS_TOKEN"
        assert client._refresh_token == "NEW_REFRESH_TOKEN"
        assert result["access_token"] == "NEW_ACCESS_TOKEN"

    async def test_refresh_token_no_refresh_token_raises(self) -> None:
        client = self._make_client()
        client._refresh_token = ""
        with pytest.raises(PowerBIAuthError, match="No refresh_token"):
            await client.refresh_token()

    async def test_refresh_token_no_tenant_raises(self) -> None:
        client = self._make_client()
        client._az_tenant_id = ""
        with pytest.raises(PowerBIAuthError, match="tenant_id_azure"):
            await client.refresh_token()


# ─────────────────────────────────────────────────────────────────────────────
# 6. authorize() (4 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthorize:
    async def test_returns_string(self) -> None:
        conn = _make_connector()
        url = await conn.authorize()
        assert isinstance(url, str)
        assert url.startswith("https://login.microsoftonline.com/")

    async def test_contains_tenant_id(self) -> None:
        conn = _make_connector()
        url = await conn.authorize()
        assert "TENANT_UUID" in url

    async def test_contains_client_id(self) -> None:
        conn = _make_connector()
        url = await conn.authorize()
        assert "CLIENT_ID" in url

    async def test_contains_powerbi_scope(self) -> None:
        conn = _make_connector()
        url = await conn.authorize()
        assert "powerbi" in url.lower() or "analysis.windows.net" in url

    async def test_falls_back_to_common_when_no_tenant(self) -> None:
        conn = _make_connector(tenant_id_azure="")
        url = await conn.authorize()
        assert "common" in url


# ─────────────────────────────────────────────────────────────────────────────
# 7. install() (6 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestInstall:
    async def test_success(self) -> None:
        conn = _make_connector()
        conn.client.get_reports = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.install()
        assert result.success is True
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == "conn-powerbi-01"

    async def test_missing_client_id(self) -> None:
        conn = _make_connector(client_id="")
        result = await conn.install()
        assert result.success is False
        assert "client_id" in result.message
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_missing_client_secret(self) -> None:
        conn = _make_connector(client_secret="")
        result = await conn.install()
        assert result.success is False
        assert "client_secret" in result.message
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_missing_tenant_id_azure(self) -> None:
        conn = _make_connector(tenant_id_azure="")
        result = await conn.install()
        assert result.success is False
        assert "tenant_id_azure" in result.message
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_missing_access_token(self) -> None:
        conn = _make_connector(access_token="")
        result = await conn.install()
        assert result.success is False
        assert "access_token" in result.message.lower() or "OAuth" in result.message

    async def test_auth_error_during_validation(self) -> None:
        conn = _make_connector()
        conn.client.get_reports = AsyncMock(  # type: ignore[method-assign]
            side_effect=PowerBIAuthError("invalid token", status_code=401)
        )
        result = await conn.install()
        assert result.success is False
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_unexpected_error_during_validation(self) -> None:
        conn = _make_connector()
        conn.client.get_reports = AsyncMock(  # type: ignore[method-assign]
            side_effect=PowerBINetworkError("connection refused")
        )
        result = await conn.install()
        assert result.success is False
        assert result.auth_status == AuthStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 8. health_check() (6 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthCheck:
    async def test_healthy(self) -> None:
        conn = _make_connector()
        conn.client.get_reports = AsyncMock(return_value=[_raw_report()])  # type: ignore[method-assign]
        result = await conn.health_check()
        assert result.healthy is True
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_missing_token(self) -> None:
        conn = _make_connector(access_token="")
        result = await conn.health_check()
        assert result.healthy is False
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_auth_error(self) -> None:
        conn = _make_connector()
        conn.client.get_reports = AsyncMock(  # type: ignore[method-assign]
            side_effect=PowerBIAuthError("Unauthorized", status_code=401)
        )
        result = await conn.health_check()
        assert result.healthy is False
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_network_error(self) -> None:
        conn = _make_connector()
        conn.client.get_reports = AsyncMock(  # type: ignore[method-assign]
            side_effect=PowerBINetworkError("timeout")
        )
        result = await conn.health_check()
        assert result.healthy is False
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_generic_error(self) -> None:
        conn = _make_connector()
        conn.client.get_reports = AsyncMock(  # type: ignore[method-assign]
            side_effect=Exception("unexpected error")
        )
        result = await conn.health_check()
        assert result.healthy is False
        assert result.health == ConnectorHealth.DEGRADED

    async def test_details_include_report_count(self) -> None:
        conn = _make_connector()
        conn.client.get_reports = AsyncMock(return_value=[_raw_report(), _raw_report()])  # type: ignore[method-assign]
        result = await conn.health_check()
        assert result.details.get("report_count") == 2


# ─────────────────────────────────────────────────────────────────────────────
# 9. sync() (7 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestSync:
    async def test_returns_sync_result(self) -> None:
        conn = _make_connector()
        conn.client.get_dashboards = AsyncMock(return_value=[_raw_dashboard()])  # type: ignore[method-assign]
        conn.client.get_reports = AsyncMock(return_value=[_raw_report()])  # type: ignore[method-assign]
        conn.client.get_datasets = AsyncMock(return_value=[_raw_dataset()])  # type: ignore[method-assign]
        result = await conn.sync()
        assert isinstance(result, SyncResult)

    async def test_correct_document_count(self) -> None:
        conn = _make_connector()
        conn.client.get_dashboards = AsyncMock(return_value=[_raw_dashboard(), _raw_dashboard(id="d2")])  # type: ignore[method-assign]
        conn.client.get_reports = AsyncMock(return_value=[_raw_report()])  # type: ignore[method-assign]
        conn.client.get_datasets = AsyncMock(return_value=[_raw_dataset()])  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.documents_synced == 4
        assert result.documents_failed == 0
        assert result.success is True
        assert result.status == SyncStatus.COMPLETED

    async def test_all_entities_in_metadata(self) -> None:
        conn = _make_connector()
        conn.client.get_dashboards = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.client.get_reports = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.client.get_datasets = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        assert "dashboards" in result.metadata["entities"]
        assert "reports" in result.metadata["entities"]
        assert "datasets" in result.metadata["entities"]

    async def test_partial_failure_graceful(self) -> None:
        conn = _make_connector()
        conn.client.get_dashboards = AsyncMock(side_effect=PowerBINetworkError("error"))  # type: ignore[method-assign]
        conn.client.get_reports = AsyncMock(return_value=[_raw_report()])  # type: ignore[method-assign]
        conn.client.get_datasets = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.documents_synced == 1
        assert result.documents_failed == 1
        assert result.success is False
        assert result.status == SyncStatus.PARTIAL

    async def test_total_failure(self) -> None:
        conn = _make_connector()
        conn.client.get_dashboards = AsyncMock(side_effect=PowerBIError("error"))  # type: ignore[method-assign]
        conn.client.get_reports = AsyncMock(side_effect=PowerBIError("error"))  # type: ignore[method-assign]
        conn.client.get_datasets = AsyncMock(side_effect=PowerBIError("error"))  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.success is False
        assert result.status == SyncStatus.FAILED

    async def test_documents_are_connector_documents(self) -> None:
        conn = _make_connector()
        conn.client.get_dashboards = AsyncMock(return_value=[_raw_dashboard()])  # type: ignore[method-assign]
        conn.client.get_reports = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.client.get_datasets = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        assert all(isinstance(d, ConnectorDocument) for d in result.documents)

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        conn = _make_connector()
        conn.client.get_dashboards = AsyncMock(return_value=[_raw_dashboard()])  # type: ignore[method-assign]
        conn.client.get_reports = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.client.get_datasets = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn._ingest_document = AsyncMock()  # type: ignore[method-assign]
        await conn.sync(kb_id="kb-123")
        assert conn._ingest_document.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# 10. list_* entity accessors (8 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestListMethods:
    async def test_list_dashboards_returns_list(self) -> None:
        conn = _make_connector()
        conn.client.get_dashboards = AsyncMock(return_value=[_raw_dashboard()])  # type: ignore[method-assign]
        result = await conn.list_dashboards()
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_dashboards_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_dashboards = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.list_dashboards()
        assert result == []

    async def test_list_reports_returns_list(self) -> None:
        conn = _make_connector()
        conn.client.get_reports = AsyncMock(return_value=[_raw_report()])  # type: ignore[method-assign]
        result = await conn.list_reports()
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_reports_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_reports = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.list_reports()
        assert result == []

    async def test_list_datasets_returns_list(self) -> None:
        conn = _make_connector()
        conn.client.get_datasets = AsyncMock(return_value=[_raw_dataset()])  # type: ignore[method-assign]
        result = await conn.list_datasets()
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_datasets_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_datasets = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.list_datasets()
        assert result == []

    async def test_list_workspaces_returns_list(self) -> None:
        conn = _make_connector()
        conn.client.get_workspaces = AsyncMock(return_value=[_raw_workspace()])  # type: ignore[method-assign]
        result = await conn.list_workspaces()
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_workspaces_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_workspaces = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.list_workspaces()
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# 11. Module-level constants & connector identity (4 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectorIdentity:
    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "powerbi"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "oauth2"

    def test_connector_class_attributes(self) -> None:
        assert PowerBIConnector.CONNECTOR_TYPE == "powerbi"
        assert PowerBIConnector.AUTH_TYPE == "oauth2"

    def test_connector_stores_config(self) -> None:
        conn = _make_connector()
        assert conn.config["client_id"] == "CLIENT_ID"
        assert conn.tenant_id == "shielva-tenant"
        assert conn.connector_id == "conn-powerbi-01"
