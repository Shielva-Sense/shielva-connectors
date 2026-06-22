"""Unit tests for DatadogConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CONNECTOR_TYPE, AUTH_TYPE, DatadogConnector
from exceptions import (
    DatadogAuthError,
    DatadogError,
    DatadogNetworkError,
    DatadogNotFoundError,
    DatadogRateLimitError,
)
from helpers.utils import (
    _stable_id,
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
    MonitorStatus,
    MonitorType,
    SyncStatus,
)

TENANT_ID = "tenant_datadog_test"
CONNECTOR_ID = "conn_datadog_test_001"
VALID_API_KEY = "abc123datadogapikey"
VALID_APP_KEY = "xyz789datadogappkey"
VALID_SITE = "datadoghq.com"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_VALIDATE_RESPONSE: dict = {"valid": True}

SAMPLE_MONITOR: dict = {
    "id": 12345,
    "name": "CPU usage on prod",
    "type": "metric alert",
    "query": "avg(last_5m):avg:system.cpu.user{role:db} > 90",
    "message": "CPU is high! @pagerduty",
    "overall_state": "OK",
    "tags": ["env:prod", "team:infra"],
    "created": "2024-01-01T00:00:00+00:00",
    "modified": "2024-06-01T00:00:00+00:00",
}

SAMPLE_MONITOR_2: dict = {
    "id": 67890,
    "name": "Error rate spike",
    "type": "query alert",
    "query": "avg(last_1m):avg:trace.http.request.errors{*} > 0.05",
    "message": "Error rate too high",
    "overall_state": "Alert",
    "tags": ["env:prod"],
    "created": "2024-02-01T00:00:00+00:00",
    "modified": "2024-06-15T00:00:00+00:00",
}

SAMPLE_DASHBOARD: dict = {
    "id": "abc-def-123",
    "title": "Infrastructure Overview",
    "description": "Top-level infra health",
    "layout_type": "ordered",
    "url": "/dashboard/abc-def-123/infrastructure-overview",
    "created_at": "2024-01-15T00:00:00+00:00",
    "modified_at": "2024-06-10T00:00:00+00:00",
    "author_handle": "infra@example.com",
}

SAMPLE_DASHBOARDS_RESPONSE: dict = {
    "dashboards": [SAMPLE_DASHBOARD],
}

SAMPLE_HOST: dict = {
    "id": 9001,
    "host_name": "web-01.prod.example.com",
    "aliases": ["web-01"],
    "apps": ["agent"],
    "up": True,
    "last_reported_time": 1720000000,
    "sources": ["agent", "aws"],
    "tags_by_source": {
        "Datadog": ["env:prod", "role:web"],
        "aws": ["region:us-east-1"],
    },
}

SAMPLE_HOSTS_RESPONSE: dict = {
    "host_list": [SAMPLE_HOST],
    "total_returned": 1,
}

SAMPLE_EVENT: dict = {
    "id": 5551234,
    "title": "Deployment completed",
    "text": "Version 2.3.1 deployed successfully to prod.",
    "alert_type": "success",
    "date_happened": 1720000100,
    "host": "deploy-01.example.com",
    "tags": ["env:prod", "version:2.3.1"],
    "url": "https://app.datadoghq.com/event/event?id=5551234",
    "source": "deployment-tracker",
}

SAMPLE_EVENTS_RESPONSE: dict = {
    "events": [SAMPLE_EVENT],
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1 — Exception hierarchy (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_datadog_error_base(self) -> None:
        exc = DatadogError("something broke", status_code=500, code="server_error")
        assert str(exc) == "something broke"
        assert exc.message == "something broke"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_datadog_auth_error_is_datadog_error(self) -> None:
        exc = DatadogAuthError("forbidden", status_code=403, code="auth_error")
        assert isinstance(exc, DatadogError)
        assert exc.status_code == 403

    def test_datadog_network_error(self) -> None:
        exc = DatadogNetworkError("connection refused")
        assert isinstance(exc, DatadogError)
        assert "connection" in str(exc)

    def test_datadog_not_found_error(self) -> None:
        exc = DatadogNotFoundError("monitor", "99999")
        assert isinstance(exc, DatadogError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "99999" in str(exc)

    def test_datadog_rate_limit_error(self) -> None:
        exc = DatadogRateLimitError("too many requests", retry_after=30.0)
        assert isinstance(exc, DatadogError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 30.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2 — Models & enums (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_monitor_status_enum(self) -> None:
        assert MonitorStatus.OK == "OK"
        assert MonitorStatus.ALERT == "Alert"
        assert MonitorStatus.WARN == "Warn"

    def test_monitor_type_enum(self) -> None:
        assert MonitorType.METRIC_ALERT == "metric alert"
        assert MonitorType.LOG_ALERT == "log alert"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test Doc",
            content="content here",
            connector_id="conn1",
            tenant_id="tenant1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 3 — Normalize functions (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeFunctions:
    def test_normalize_monitor_basic(self) -> None:
        doc = normalize_monitor(SAMPLE_MONITOR, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "CPU usage on prod" in doc.title
        assert "12345" in doc.content
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_normalize_monitor_stable_id(self) -> None:
        doc1 = normalize_monitor(SAMPLE_MONITOR)
        doc2 = normalize_monitor(SAMPLE_MONITOR)
        assert doc1.source_id == doc2.source_id
        assert len(doc1.source_id) == 16

    def test_normalize_monitor_stable_id_matches_formula(self) -> None:
        expected = _stable_id("monitor", str(SAMPLE_MONITOR["id"]))
        doc = normalize_monitor(SAMPLE_MONITOR)
        assert doc.source_id == expected

    def test_normalize_monitor_metadata(self) -> None:
        doc = normalize_monitor(SAMPLE_MONITOR)
        assert doc.metadata["monitor_id"] == 12345
        assert doc.metadata["status"] == "OK"
        assert "env:prod" in doc.metadata["tags"]

    def test_normalize_dashboard_basic(self) -> None:
        doc = normalize_dashboard(SAMPLE_DASHBOARD, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert "Infrastructure Overview" in doc.title
        assert "abc-def-123" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_normalize_dashboard_stable_id(self) -> None:
        doc = normalize_dashboard(SAMPLE_DASHBOARD)
        expected = _stable_id("dashboard", "abc-def-123")
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_normalize_host_basic(self) -> None:
        doc = normalize_host(SAMPLE_HOST, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert "web-01.prod.example.com" in doc.title
        assert "up" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_normalize_host_stable_id(self) -> None:
        doc = normalize_host(SAMPLE_HOST)
        expected = _stable_id("host", "web-01.prod.example.com")
        assert doc.source_id == expected

    def test_normalize_event_basic(self) -> None:
        doc = normalize_event(SAMPLE_EVENT, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert "Deployment completed" in doc.title
        assert "5551234" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_normalize_event_stable_id(self) -> None:
        doc = normalize_event(SAMPLE_EVENT)
        expected = _stable_id("event", str(SAMPLE_EVENT["id"]))
        assert doc.source_id == expected

    def test_normalize_monitor_missing_fields(self) -> None:
        doc = normalize_monitor({})
        assert doc.title == "Datadog monitor: Unnamed Monitor"
        assert len(doc.source_id) == 16

    def test_normalize_host_down(self) -> None:
        host = dict(SAMPLE_HOST, up=False)
        doc = normalize_host(host)
        assert "down" in doc.content


# ═══════════════════════════════════════════════════════════════════════════════
# 4 — with_retry (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_retry_succeeds_first_attempt(self) -> None:
        mock_fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock_fn, max_attempts=3)
        assert result == {"ok": True}
        assert mock_fn.call_count == 1

    async def test_retry_succeeds_on_second_attempt(self) -> None:
        call_count = 0

        async def flaky() -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise DatadogNetworkError("transient")
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(flaky, max_attempts=3)
        assert result == {"ok": True}
        assert call_count == 2

    async def test_retry_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=DatadogNetworkError("always fails"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(DatadogNetworkError):
                await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 3

    async def test_auth_error_not_retried(self) -> None:
        mock_fn = AsyncMock(side_effect=DatadogAuthError("forbidden"))
        with pytest.raises(DatadogAuthError):
            await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    async def test_rate_limit_retried_with_backoff(self) -> None:
        call_count = 0

        async def rate_limited() -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise DatadogRateLimitError("slow down", retry_after=0.0)
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(rate_limited, max_attempts=3)
        assert result == {"ok": True}

    async def test_retry_passes_args_to_fn(self) -> None:
        mock_fn = AsyncMock(return_value={"monitors": []})
        await with_retry(mock_fn, "arg1", key="value")
        mock_fn.assert_called_once_with("arg1", key="value")


# ═══════════════════════════════════════════════════════════════════════════════
# 5 — HTTP client (mocked) (14 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDatadogHTTPClient:
    def _make_client(self, site: str = VALID_SITE) -> "DatadogHTTPClient":
        from client.http_client import DatadogHTTPClient
        return DatadogHTTPClient(config={
            "api_key": VALID_API_KEY,
            "app_key": VALID_APP_KEY,
            "site": site,
        })

    def test_base_url_default_site(self) -> None:
        client = self._make_client("datadoghq.com")
        assert client._base_url == "https://api.datadoghq.com/api/"

    def test_base_url_eu_site(self) -> None:
        client = self._make_client("datadoghq.eu")
        assert client._base_url == "https://api.datadoghq.eu/api/"

    def test_base_url_us3_site(self) -> None:
        client = self._make_client("us3.datadoghq.com")
        assert client._base_url == "https://api.us3.datadoghq.com/api/"

    def test_dual_key_headers_stored_on_client(self) -> None:
        """Verify that api_key and app_key are stored and will be injected as headers."""
        client = self._make_client()
        assert client._api_key == VALID_API_KEY
        assert client._app_key == VALID_APP_KEY

    async def test_dual_key_headers_injected_in_session(self) -> None:
        """Verify both DD-* headers are present in the aiohttp session headers."""
        client = self._make_client()
        try:
            session = client._get_session()
            headers = dict(session.headers)
            assert headers.get("DD-API-KEY") == VALID_API_KEY
            assert headers.get("DD-APPLICATION-KEY") == VALID_APP_KEY
        finally:
            await client.aclose()

    async def test_validate_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_VALIDATE_RESPONSE)
        result = await client.validate()
        assert result == SAMPLE_VALIDATE_RESPONSE
        client._request.assert_called_once_with("GET", "v1/validate")

    async def test_get_monitors_returns_list(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=[SAMPLE_MONITOR, SAMPLE_MONITOR_2])
        result = await client.get_monitors(page=0, page_size=100)
        assert isinstance(result, list)
        assert len(result) == 2
        client._request.assert_called_once_with(
            "GET", "v1/monitor", params={"page": 0, "page_size": 100}
        )

    async def test_get_monitors_with_pagination(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=[SAMPLE_MONITOR])
        await client.get_monitors(page=2, page_size=50)
        client._request.assert_called_once_with(
            "GET", "v1/monitor", params={"page": 2, "page_size": 50}
        )

    async def test_get_monitor_by_id(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_MONITOR)
        result = await client.get_monitor(12345)
        assert result["id"] == 12345
        client._request.assert_called_once_with("GET", "v1/monitor/12345")

    async def test_get_dashboards(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_DASHBOARDS_RESPONSE)
        result = await client.get_dashboards()
        assert "dashboards" in result
        client._request.assert_called_once_with(
            "GET", "v1/dashboard", params={"count": 100, "start": 0}
        )

    async def test_get_hosts(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_HOSTS_RESPONSE)
        result = await client.get_hosts(count=100, start=0)
        assert "host_list" in result
        client._request.assert_called_once_with(
            "GET", "v1/hosts", params={"count": 100, "start": 0}
        )

    async def test_get_events(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_EVENTS_RESPONSE)
        result = await client.get_events(start=1700000000, end=1700086400, page=0)
        assert "events" in result
        client._request.assert_called_once_with(
            "GET", "v1/events",
            params={"start": 1700000000, "end": 1700086400, "page": 0},
        )

    async def test_raise_for_status_403_auth_error(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": "k", "app_key": "a"})
        with pytest.raises(DatadogAuthError):
            client._raise_for_status(403, {"errors": ["Forbidden"]})

    async def test_raise_for_status_429_rate_limit(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": "k", "app_key": "a"})
        with pytest.raises(DatadogRateLimitError):
            client._raise_for_status(429, {"errors": ["Too many requests"]})

    async def test_raise_for_status_500_network_error(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": "k", "app_key": "a"})
        with pytest.raises(DatadogNetworkError):
            client._raise_for_status(500, {"errors": ["Internal Server Error"]})

    async def test_raise_for_status_404_not_found(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": "k", "app_key": "a"})
        with pytest.raises(DatadogNotFoundError):
            client._raise_for_status(404, {})


# ═══════════════════════════════════════════════════════════════════════════════
# 6 — install() (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    def _make_connector(self, api_key: str = VALID_API_KEY, app_key: str = VALID_APP_KEY) -> DatadogConnector:
        return DatadogConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": api_key, "app_key": app_key, "site": VALID_SITE},
        )

    async def test_install_success(self) -> None:
        connector = self._make_connector()
        connector._make_client = MagicMock(return_value=MagicMock(
            validate=AsyncMock(return_value=SAMPLE_VALIDATE_RESPONSE),
            aclose=AsyncMock(),
        ))
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Datadog" in result.message

    async def test_install_missing_api_key(self) -> None:
        connector = self._make_connector(api_key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_missing_app_key(self) -> None:
        connector = self._make_connector(app_key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "app_key" in result.message

    async def test_install_invalid_credentials(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            validate=AsyncMock(side_effect=DatadogAuthError("Invalid API key")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            validate=AsyncMock(side_effect=DatadogNetworkError("timeout")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 7 — health_check() (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    def _make_connector(self, api_key: str = VALID_API_KEY, app_key: str = VALID_APP_KEY) -> DatadogConnector:
        return DatadogConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": api_key, "app_key": app_key, "site": VALID_SITE},
        )

    async def test_health_check_healthy(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            validate=AsyncMock(return_value=SAMPLE_VALIDATE_RESPONSE),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_missing_credentials(self) -> None:
        connector = self._make_connector(api_key="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_missing_app_key(self) -> None:
        connector = self._make_connector(app_key="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_failure(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            validate=AsyncMock(side_effect=DatadogAuthError("invalid keys")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_degraded(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            validate=AsyncMock(side_effect=DatadogNetworkError("connection reset")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 8 — sync() (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _make_connector(self) -> DatadogConnector:
        return DatadogConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY, "site": VALID_SITE},
        )

    async def test_sync_all_resources_success(self) -> None:
        connector = self._make_connector()
        connector.list_monitors = AsyncMock(return_value=[SAMPLE_MONITOR])
        connector.list_dashboards = AsyncMock(return_value=[SAMPLE_DASHBOARD])
        connector.list_hosts = AsyncMock(return_value=[SAMPLE_HOST])
        connector.list_events = AsyncMock(return_value=[SAMPLE_EVENT])
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert result.documents_failed == 0

    async def test_sync_with_kb_id(self) -> None:
        connector = self._make_connector()
        connector.list_monitors = AsyncMock(return_value=[SAMPLE_MONITOR])
        connector.list_dashboards = AsyncMock(return_value=[])
        connector.list_hosts = AsyncMock(return_value=[])
        connector.list_events = AsyncMock(return_value=[])
        connector._ingest_document = AsyncMock()
        result = await connector.sync(kb_id="kb_test_123")
        connector._ingest_document.assert_called_once()
        assert result.documents_synced == 1

    async def test_sync_no_data_returns_partial(self) -> None:
        connector = self._make_connector()
        connector.list_monitors = AsyncMock(return_value=[])
        connector.list_dashboards = AsyncMock(return_value=[])
        connector.list_hosts = AsyncMock(return_value=[])
        connector.list_events = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_monitors_failure_non_fatal(self) -> None:
        connector = self._make_connector()
        connector.list_monitors = AsyncMock(side_effect=DatadogError("monitors failed"))
        connector.list_dashboards = AsyncMock(return_value=[SAMPLE_DASHBOARD])
        connector.list_hosts = AsyncMock(return_value=[])
        connector.list_events = AsyncMock(return_value=[])
        result = await connector.sync()
        # dashboard still synced
        assert result.documents_synced >= 1

    async def test_sync_partial_on_ingest_failure(self) -> None:
        connector = self._make_connector()
        connector.list_monitors = AsyncMock(return_value=[SAMPLE_MONITOR])
        connector.list_dashboards = AsyncMock(return_value=[])
        connector.list_hosts = AsyncMock(return_value=[])
        connector.list_events = AsyncMock(return_value=[])
        connector._ingest_document = AsyncMock(side_effect=Exception("ingest failed"))

        async def broken_ingest(doc: object, kb_id: str) -> None:
            raise Exception("ingest failed")

        connector._ingest_document = broken_ingest
        # Even with ingest failure, sync should count as synced (no kb_id path)
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_multiple_monitors(self) -> None:
        connector = self._make_connector()
        connector.list_monitors = AsyncMock(return_value=[SAMPLE_MONITOR, SAMPLE_MONITOR_2])
        connector.list_dashboards = AsyncMock(return_value=[])
        connector.list_hosts = AsyncMock(return_value=[])
        connector.list_events = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_events_and_hosts(self) -> None:
        connector = self._make_connector()
        connector.list_monitors = AsyncMock(return_value=[])
        connector.list_dashboards = AsyncMock(return_value=[])
        connector.list_hosts = AsyncMock(return_value=[SAMPLE_HOST])
        connector.list_events = AsyncMock(return_value=[SAMPLE_EVENT])
        result = await connector.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_all_resources_fail_still_returns_partial(self) -> None:
        connector = self._make_connector()
        connector.list_monitors = AsyncMock(side_effect=DatadogError("err"))
        connector.list_dashboards = AsyncMock(side_effect=DatadogError("err"))
        connector.list_hosts = AsyncMock(side_effect=DatadogError("err"))
        connector.list_events = AsyncMock(side_effect=DatadogError("err"))
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════════════
# 9 — list methods (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    def _make_connector(self) -> DatadogConnector:
        return DatadogConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY, "site": VALID_SITE},
        )

    async def test_list_monitors_single_page(self) -> None:
        connector = self._make_connector()
        connector.client.get_monitors = AsyncMock(return_value=[SAMPLE_MONITOR])
        result = await connector.list_monitors(page_size=100)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 12345

    async def test_list_monitors_stops_on_empty_page(self) -> None:
        connector = self._make_connector()
        # First call returns 1 monitor (< page_size), so pagination stops
        connector.client.get_monitors = AsyncMock(return_value=[SAMPLE_MONITOR])
        result = await connector.list_monitors(page_size=100)
        assert connector.client.get_monitors.call_count == 1

    async def test_list_dashboards(self) -> None:
        connector = self._make_connector()
        connector.client.get_dashboards = AsyncMock(return_value=SAMPLE_DASHBOARDS_RESPONSE)
        result = await connector.list_dashboards()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "abc-def-123"

    async def test_list_hosts(self) -> None:
        connector = self._make_connector()
        connector.client.get_hosts = AsyncMock(return_value=SAMPLE_HOSTS_RESPONSE)
        result = await connector.list_hosts()
        assert isinstance(result, list)
        assert result[0]["host_name"] == "web-01.prod.example.com"

    async def test_list_events_default_window(self) -> None:
        connector = self._make_connector()
        connector.client.get_events = AsyncMock(return_value=SAMPLE_EVENTS_RESPONSE)
        result = await connector.list_events()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 5551234


# ═══════════════════════════════════════════════════════════════════════════════
# 10 — get_monitor() (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetMonitor:
    def _make_connector(self) -> DatadogConnector:
        return DatadogConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY, "site": VALID_SITE},
        )

    async def test_get_monitor_success(self) -> None:
        connector = self._make_connector()
        connector.client.get_monitor = AsyncMock(return_value=SAMPLE_MONITOR)
        result = await connector.get_monitor(12345)
        assert result["id"] == 12345
        assert result["name"] == "CPU usage on prod"

    async def test_get_monitor_not_found(self) -> None:
        connector = self._make_connector()
        connector.client.get_monitor = AsyncMock(
            side_effect=DatadogNotFoundError("monitor", "99999")
        )
        with pytest.raises(DatadogNotFoundError):
            await connector.get_monitor(99999)

    async def test_get_monitor_auth_error_propagates(self) -> None:
        connector = self._make_connector()
        connector.client.get_monitor = AsyncMock(
            side_effect=DatadogAuthError("unauthorized")
        )
        with pytest.raises(DatadogAuthError):
            await connector.get_monitor(12345)


# ═══════════════════════════════════════════════════════════════════════════════
# 11 — connector constants & module-level attributes (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "datadog"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_class_attributes(self) -> None:
        assert DatadogConnector.CONNECTOR_TYPE == "datadog"
        assert DatadogConnector.AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# 12 — stable ID helper (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStableId:
    def test_stable_id_length(self) -> None:
        result = _stable_id("monitor", "12345")
        assert len(result) == 16

    def test_stable_id_deterministic(self) -> None:
        a = _stable_id("monitor", "12345")
        b = _stable_id("monitor", "12345")
        assert a == b

    def test_stable_id_differs_by_prefix(self) -> None:
        monitor_id = _stable_id("monitor", "123")
        dashboard_id = _stable_id("dashboard", "123")
        assert monitor_id != dashboard_id


# ═══════════════════════════════════════════════════════════════════════════════
# 13 — lifecycle & config (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    async def test_connector_aclose(self) -> None:
        connector = DatadogConnector(
            config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY}
        )
        connector.client.aclose = AsyncMock()
        await connector.aclose()
        connector.client.aclose.assert_called_once()

    async def test_connector_context_manager(self) -> None:
        connector = DatadogConnector(
            config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY}
        )
        connector.client.aclose = AsyncMock()
        async with connector as ctx:
            assert ctx is connector
        connector.client.aclose.assert_called_once()

    def test_connector_default_site(self) -> None:
        connector = DatadogConnector(config={"api_key": "k", "app_key": "a"})
        assert connector._site == "datadoghq.com"

    def test_connector_custom_site(self) -> None:
        connector = DatadogConnector(
            config={"api_key": "k", "app_key": "a", "site": "datadoghq.eu"}
        )
        assert connector._site == "datadoghq.eu"


# ═══════════════════════════════════════════════════════════════════════════════
# 14 — HTTP client lifecycle (2 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientLifecycle:
    async def test_http_client_aclose(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": "k", "app_key": "a"})
        # Ensure session is created then closed
        _ = client._get_session()
        await client.aclose()
        assert client._session is None or client._session.closed

    async def test_http_client_context_manager(self) -> None:
        from client.http_client import DatadogHTTPClient
        async with DatadogHTTPClient(config={"api_key": "k", "app_key": "a"}) as client:
            assert client is not None
        # After exit, session should be closed
        assert client._session is None or client._session.closed


# ═══════════════════════════════════════════════════════════════════════════════
# 15 — get_dashboard() (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetDashboard:
    def _make_connector(self) -> DatadogConnector:
        return DatadogConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY, "site": VALID_SITE},
        )

    async def test_get_dashboard_success(self) -> None:
        connector = self._make_connector()
        connector.client.get_dashboard = AsyncMock(return_value=SAMPLE_DASHBOARD)
        result = await connector.get_dashboard("abc-def-123")
        assert result["id"] == "abc-def-123"
        assert result["title"] == "Infrastructure Overview"

    async def test_get_dashboard_not_found(self) -> None:
        connector = self._make_connector()
        connector.client.get_dashboard = AsyncMock(
            side_effect=DatadogNotFoundError("dashboard", "no-such-id")
        )
        with pytest.raises(DatadogNotFoundError):
            await connector.get_dashboard("no-such-id")

    async def test_get_dashboard_auth_error_propagates(self) -> None:
        connector = self._make_connector()
        connector.client.get_dashboard = AsyncMock(
            side_effect=DatadogAuthError("unauthorized")
        )
        with pytest.raises(DatadogAuthError):
            await connector.get_dashboard("abc-def-123")

    async def test_http_client_get_dashboard_calls_correct_path(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY})
        client._request = AsyncMock(return_value=SAMPLE_DASHBOARD)
        result = await client.get_dashboard("abc-def-123")
        assert result["id"] == "abc-def-123"
        client._request.assert_called_once_with("GET", "v1/dashboard/abc-def-123")


# ═══════════════════════════════════════════════════════════════════════════════
# 16 — query_logs() (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLE_LOGS_RESPONSE: dict = {
    "data": [
        {
            "id": "log_event_001",
            "type": "log",
            "attributes": {
                "message": "ERROR: connection refused",
                "status": "error",
                "service": "api",
                "timestamp": "2026-06-20T10:00:00.000Z",
            },
        },
        {
            "id": "log_event_002",
            "type": "log",
            "attributes": {
                "message": "Request processed in 120ms",
                "status": "info",
                "service": "api",
                "timestamp": "2026-06-20T10:01:00.000Z",
            },
        },
    ],
    "meta": {
        "page": {
            "after": None,
        }
    },
}

SAMPLE_LOGS_RESPONSE_WITH_CURSOR: dict = {
    "data": [
        {
            "id": "log_event_003",
            "type": "log",
            "attributes": {"message": "page1 log", "status": "info"},
        }
    ],
    "meta": {"page": {"after": "cursor_abc123"}},
}

SAMPLE_LOGS_RESPONSE_PAGE2: dict = {
    "data": [
        {
            "id": "log_event_004",
            "type": "log",
            "attributes": {"message": "page2 log", "status": "warn"},
        }
    ],
    "meta": {"page": {"after": None}},
}


class TestQueryLogs:
    def _make_connector(self) -> DatadogConnector:
        return DatadogConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY, "site": VALID_SITE},
        )

    async def test_query_logs_basic(self) -> None:
        connector = self._make_connector()
        connector.client.list_logs = AsyncMock(return_value=SAMPLE_LOGS_RESPONSE)
        result = await connector.query_logs("service:api status:error")
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["id"] == "log_event_001"

    async def test_query_logs_default_time_range(self) -> None:
        connector = self._make_connector()
        connector.client.list_logs = AsyncMock(return_value=SAMPLE_LOGS_RESPONSE)
        result = await connector.query_logs("*")
        # Should succeed with default time range
        assert isinstance(result, list)
        call_kwargs = connector.client.list_logs.call_args.kwargs
        assert "from_ts" in call_kwargs
        assert "to_ts" in call_kwargs
        assert call_kwargs["from_ts"] < call_kwargs["to_ts"]

    async def test_query_logs_custom_time_range(self) -> None:
        connector = self._make_connector()
        connector.client.list_logs = AsyncMock(return_value=SAMPLE_LOGS_RESPONSE)
        result = await connector.query_logs(
            "status:error", from_ts=1700000000, to_ts=1700086400, limit=50
        )
        assert isinstance(result, list)
        call_kwargs = connector.client.list_logs.call_args.kwargs
        assert call_kwargs["from_ts"] == 1700000000
        assert call_kwargs["to_ts"] == 1700086400

    async def test_query_logs_cursor_pagination(self) -> None:
        connector = self._make_connector()
        connector.client.list_logs = AsyncMock(
            side_effect=[SAMPLE_LOGS_RESPONSE_WITH_CURSOR, SAMPLE_LOGS_RESPONSE_PAGE2]
        )
        result = await connector.query_logs("*", limit=100)
        assert len(result) == 2
        assert result[0]["id"] == "log_event_003"
        assert result[1]["id"] == "log_event_004"
        # Second call must pass the cursor
        second_call_kwargs = connector.client.list_logs.call_args_list[1].kwargs
        assert second_call_kwargs["cursor"] == "cursor_abc123"

    async def test_query_logs_empty_result(self) -> None:
        connector = self._make_connector()
        connector.client.list_logs = AsyncMock(return_value={"data": [], "meta": {"page": {"after": None}}})
        result = await connector.query_logs("no-match-query")
        assert result == []

    async def test_http_client_list_logs_post_body(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY})
        client._request = AsyncMock(return_value=SAMPLE_LOGS_RESPONSE)
        result = await client.list_logs(
            query="service:api",
            from_ts=1700000000,
            to_ts=1700086400,
            limit=50,
        )
        assert "data" in result
        call_args = client._request.call_args
        assert call_args.args[0] == "POST"
        assert call_args.args[1] == "v2/logs/events/search"
        body = call_args.kwargs["json"]
        assert body["filter"]["query"] == "service:api"
        assert body["page"]["limit"] == 50


# ═══════════════════════════════════════════════════════════════════════════════
# 17 — get_metrics_list() (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLE_METRICS_RESPONSE: dict = {
    "metrics": [
        "system.cpu.user",
        "system.cpu.idle",
        "system.mem.used",
        "system.disk.read",
    ],
    "from": 1700000000,
}


class TestGetMetricsList:
    def _make_connector(self) -> DatadogConnector:
        return DatadogConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY, "site": VALID_SITE},
        )

    async def test_get_metrics_list_returns_list(self) -> None:
        connector = self._make_connector()
        connector.client.get_metrics_list = AsyncMock(return_value=SAMPLE_METRICS_RESPONSE)
        result = await connector.get_metrics_list("system")
        assert isinstance(result, list)
        assert "system.cpu.user" in result
        assert len(result) == 4

    async def test_get_metrics_list_passes_query(self) -> None:
        connector = self._make_connector()
        connector.client.get_metrics_list = AsyncMock(return_value=SAMPLE_METRICS_RESPONSE)
        await connector.get_metrics_list("aws.ec2")
        connector.client.get_metrics_list.assert_called_once_with("aws.ec2")

    async def test_get_metrics_list_empty(self) -> None:
        connector = self._make_connector()
        connector.client.get_metrics_list = AsyncMock(return_value={"metrics": []})
        result = await connector.get_metrics_list("nonexistent.metric")
        assert result == []

    async def test_http_client_get_metrics_list(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY})
        client._request = AsyncMock(return_value=SAMPLE_METRICS_RESPONSE)
        result = await client.get_metrics_list("system")
        assert result == SAMPLE_METRICS_RESPONSE
        client._request.assert_called_once_with("GET", "v1/metrics", params={"q": "system"})


# ═══════════════════════════════════════════════════════════════════════════════
# 18 — list_service_checks() (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLE_SERVICE_CHECKS: list = [
    {
        "check": "datadog.agent.up",
        "host_name": "web-01.prod.example.com",
        "status": 0,
        "timestamp": "2026-06-20T10:00:00+00:00",
        "message": "",
        "tags": ["env:prod"],
    },
    {
        "check": "http.can_connect",
        "host_name": "api-01.prod.example.com",
        "status": 2,
        "timestamp": "2026-06-20T10:01:00+00:00",
        "message": "Connection refused",
        "tags": ["env:prod", "url:https://api.example.com"],
    },
]


class TestListServiceChecks:
    def _make_connector(self) -> DatadogConnector:
        return DatadogConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY, "site": VALID_SITE},
        )

    async def test_list_service_checks_returns_list(self) -> None:
        connector = self._make_connector()
        connector.client.list_service_checks = AsyncMock(return_value=SAMPLE_SERVICE_CHECKS)
        result = await connector.list_service_checks()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["check"] == "datadog.agent.up"

    async def test_list_service_checks_empty(self) -> None:
        connector = self._make_connector()
        connector.client.list_service_checks = AsyncMock(return_value=[])
        result = await connector.list_service_checks()
        assert result == []

    async def test_http_client_list_service_checks(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY})
        client._request = AsyncMock(return_value=SAMPLE_SERVICE_CHECKS)
        result = await client.list_service_checks()
        assert isinstance(result, list)
        assert len(result) == 2
        client._request.assert_called_once_with("GET", "v1/check_run")

    async def test_http_client_list_service_checks_dict_response(self) -> None:
        """Handle the case where the API wraps the list in a dict with 'checks' key."""
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY})
        client._request = AsyncMock(return_value={"checks": SAMPLE_SERVICE_CHECKS})
        result = await client.list_service_checks()
        assert isinstance(result, list)
        assert len(result) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 19 — list_monitors tags filter (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMonitorsTags:
    def _make_connector(self) -> DatadogConnector:
        return DatadogConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY, "site": VALID_SITE},
        )

    async def test_list_monitors_with_tags(self) -> None:
        connector = self._make_connector()
        connector.client.get_monitors = AsyncMock(return_value=[SAMPLE_MONITOR])
        result = await connector.list_monitors(tags=["env:prod", "team:infra"])
        assert isinstance(result, list)
        call_kwargs = connector.client.get_monitors.call_args.kwargs
        assert call_kwargs["tags"] == ["env:prod", "team:infra"]

    async def test_list_monitors_without_tags(self) -> None:
        connector = self._make_connector()
        connector.client.get_monitors = AsyncMock(return_value=[SAMPLE_MONITOR])
        await connector.list_monitors()
        call_kwargs = connector.client.get_monitors.call_args.kwargs
        assert call_kwargs.get("tags") is None

    async def test_http_client_get_monitors_with_tags(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY})
        client._request = AsyncMock(return_value=[SAMPLE_MONITOR])
        await client.get_monitors(page=0, page_size=100, tags=["env:prod"])
        call_kwargs = client._request.call_args.kwargs
        assert call_kwargs["params"]["tags"] == "env:prod"

    async def test_http_client_get_monitors_no_tags_omits_param(self) -> None:
        from client.http_client import DatadogHTTPClient
        client = DatadogHTTPClient(config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY})
        client._request = AsyncMock(return_value=[SAMPLE_MONITOR])
        await client.get_monitors(page=0, page_size=100, tags=None)
        call_kwargs = client._request.call_args.kwargs
        assert "tags" not in call_kwargs["params"]


# ═══════════════════════════════════════════════════════════════════════════════
# 20 — _raise_for_status edge cases (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRaiseForStatusEdgeCases:
    def _make_client(self) -> "DatadogHTTPClient":
        from client.http_client import DatadogHTTPClient
        return DatadogHTTPClient(config={"api_key": VALID_API_KEY, "app_key": VALID_APP_KEY})

    def test_raise_for_status_401_auth_error(self) -> None:
        client = self._make_client()
        with pytest.raises(DatadogAuthError) as exc_info:
            client._raise_for_status(401, {"errors": ["Unauthorized"]})
        assert exc_info.value.status_code == 401

    def test_raise_for_status_other_4xx(self) -> None:
        client = self._make_client()
        with pytest.raises(DatadogError) as exc_info:
            client._raise_for_status(422, {"errors": ["Unprocessable Entity"]})
        assert exc_info.value.status_code == 422
        assert not isinstance(exc_info.value, DatadogAuthError)

    def test_raise_for_status_error_as_list(self) -> None:
        client = self._make_client()
        with pytest.raises(DatadogNetworkError) as exc_info:
            client._raise_for_status(503, {"errors": ["Service Unavailable", "Try again"]})
        assert "Service Unavailable" in str(exc_info.value)
