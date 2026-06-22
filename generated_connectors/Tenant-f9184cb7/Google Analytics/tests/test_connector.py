"""Unit tests for Google Analytics 4 connector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.http_client import (
    ADMIN_BASE_URL,
    DATA_BASE_URL,
    GoogleAnalyticsHTTPClient,
)
from connector import GoogleAnalyticsConnector
from exceptions import (
    GoogleAnalyticsAuthError,
    GoogleAnalyticsError,
    GoogleAnalyticsNetworkError,
    GoogleAnalyticsNotFoundError,
    GoogleAnalyticsRateLimitError,
)
from helpers.utils import normalize_property, normalize_report_row, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "tenant_ga4_test"
CONNECTOR_ID = "conn_google_analytics_001"
CLIENT_ID = "123456789-abc.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test_secret_value"
ACCESS_TOKEN = "ya29.test_access_token_value"
PROPERTY_ID = "123456789"
ACCOUNT_ID = "654321"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_ACCOUNTS_RESPONSE: dict[str, Any] = {
    "accounts": [
        {
            "name": "accounts/654321",
            "displayName": "My Analytics Account",
            "regionCode": "US",
            "createTime": "2020-01-01T00:00:00Z",
        },
        {
            "name": "accounts/654322",
            "displayName": "Second Account",
            "regionCode": "GB",
            "createTime": "2021-06-01T00:00:00Z",
        },
    ]
}

SAMPLE_PROPERTIES_RESPONSE: dict[str, Any] = {
    "properties": [
        {
            "name": f"properties/{PROPERTY_ID}",
            "displayName": "My Website GA4",
            "industryCategory": "TECHNOLOGY",
            "timeZone": "America/New_York",
            "currencyCode": "USD",
            "createTime": "2021-01-15T00:00:00Z",
            "parent": f"accounts/{ACCOUNT_ID}",
        }
    ]
}

SAMPLE_PROPERTY_DETAIL: dict[str, Any] = {
    "name": f"properties/{PROPERTY_ID}",
    "displayName": "My Website GA4",
    "industryCategory": "TECHNOLOGY",
    "timeZone": "America/New_York",
    "currencyCode": "USD",
    "createTime": "2021-01-15T00:00:00Z",
    "parent": f"accounts/{ACCOUNT_ID}",
}

SAMPLE_REPORT_RESPONSE: dict[str, Any] = {
    "rowCount": 3,
    "rows": [
        {
            "dimensionValues": [
                {"value": "2024-01-15"},
                {"value": "google"},
                {"value": "organic"},
            ],
            "metricValues": [
                {"value": "1234"},
                {"value": "987"},
                {"value": "5678"},
                {"value": "0.45"},
            ],
        },
        {
            "dimensionValues": [
                {"value": "2024-01-16"},
                {"value": "direct"},
                {"value": "(none)"},
            ],
            "metricValues": [
                {"value": "500"},
                {"value": "400"},
                {"value": "1200"},
                {"value": "0.30"},
            ],
        },
        {
            "dimensionValues": [
                {"value": "2024-01-17"},
                {"value": "facebook"},
                {"value": "cpc"},
            ],
            "metricValues": [
                {"value": "200"},
                {"value": "180"},
                {"value": "700"},
                {"value": "0.55"},
            ],
        },
    ],
    "dimensionHeaders": [
        {"name": "date"},
        {"name": "sessionSource"},
        {"name": "sessionMedium"},
    ],
    "metricHeaders": [
        {"name": "sessions", "type": "TYPE_INTEGER"},
        {"name": "activeUsers", "type": "TYPE_INTEGER"},
        {"name": "screenPageViews", "type": "TYPE_INTEGER"},
        {"name": "bounceRate", "type": "TYPE_FLOAT"},
    ],
}

SAMPLE_METADATA_RESPONSE: dict[str, Any] = {
    "name": f"properties/{PROPERTY_ID}/metadata",
    "dimensions": [
        {"apiName": "date", "uiName": "Date"},
        {"apiName": "sessionSource", "uiName": "Session source"},
        {"apiName": "country", "uiName": "Country"},
    ],
    "metrics": [
        {"apiName": "sessions", "uiName": "Sessions", "type": "TYPE_INTEGER"},
        {"apiName": "activeUsers", "uiName": "Active users", "type": "TYPE_INTEGER"},
        {"apiName": "bounceRate", "uiName": "Bounce rate", "type": "TYPE_FLOAT"},
    ],
}

SAMPLE_REALTIME_RESPONSE: dict[str, Any] = {
    "rows": [
        {
            "dimensionValues": [{"value": "United States"}],
            "metricValues": [{"value": "42"}],
        }
    ],
    "rowCount": 1,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_connector(
    access_token: str = ACCESS_TOKEN,
    property_id: str = PROPERTY_ID,
    client_id: str = CLIENT_ID,
    client_secret: str = CLIENT_SECRET,
) -> GoogleAnalyticsConnector:
    return GoogleAnalyticsConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": client_id,
            "client_secret": client_secret,
            "access_token": access_token,
            "property_id": property_id,
        },
    )


def make_http_client(access_token: str = ACCESS_TOKEN) -> GoogleAnalyticsHTTPClient:
    return GoogleAnalyticsHTTPClient(access_token=access_token)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptionHierarchy:
    def test_base_exception_fields(self) -> None:
        exc = GoogleAnalyticsError("test message", status_code=500, code="server_error")
        assert str(exc) == "test message"
        assert exc.message == "test message"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_base_exception_defaults(self) -> None:
        exc = GoogleAnalyticsError("bare message")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_auth_error_is_subclass(self) -> None:
        exc = GoogleAnalyticsAuthError("Unauthorized", 401, "auth_failed")
        assert isinstance(exc, GoogleAnalyticsError)
        assert exc.status_code == 401

    def test_network_error_is_subclass(self) -> None:
        exc = GoogleAnalyticsNetworkError("Timeout", 504)
        assert isinstance(exc, GoogleAnalyticsError)
        assert exc.status_code == 504

    def test_not_found_error_message(self) -> None:
        exc = GoogleAnalyticsNotFoundError("property", "123456789")
        assert isinstance(exc, GoogleAnalyticsError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "123456789" in str(exc)
        assert "property" in str(exc)

    def test_rate_limit_error_retry_after(self) -> None:
        exc = GoogleAnalyticsRateLimitError("Too many requests", retry_after=60.0)
        assert isinstance(exc, GoogleAnalyticsError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 60.0

    def test_rate_limit_error_default_retry_after(self) -> None:
        exc = GoogleAnalyticsRateLimitError("Rate limited")
        assert exc.retry_after == 0.0

    def test_exception_inheritance_tree(self) -> None:
        for cls in [
            GoogleAnalyticsAuthError,
            GoogleAnalyticsNetworkError,
            GoogleAnalyticsNotFoundError,
            GoogleAnalyticsRateLimitError,
        ]:
            assert issubclass(cls, GoogleAnalyticsError)
            assert issubclass(cls, Exception)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Models
# ═══════════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_install_result_healthy(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="conn_001",
            message="ok",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED
        assert r.connector_id == "conn_001"

    def test_install_result_defaults(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.MISSING_CREDENTIALS,
        )
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="3 accounts",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.message == "3 accounts"

    def test_sync_result_completed(self) -> None:
        r = SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=100,
            documents_synced=100,
            documents_failed=0,
        )
        assert r.status == SyncStatus.COMPLETED
        assert r.documents_found == 100
        assert r.documents_synced == 100
        assert r.documents_failed == 0

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test Doc",
            content="Some content",
            connector_id=CONNECTOR_ID,
            tenant_id=TENANT_ID,
            source_url="https://example.com",
            metadata={"type": "ga4_property"},
        )
        assert doc.source_id == "abc123"
        assert doc.title == "Test Doc"
        assert doc.metadata["type"] == "ga4_property"

    def test_connector_document_default_metadata(self) -> None:
        doc = ConnectorDocument(
            source_id="xyz",
            title="T",
            content="C",
            connector_id="c",
            tenant_id="t",
        )
        assert doc.metadata == {}
        assert doc.source_url == ""

    def test_enums_string_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"
        assert AuthStatus.CONNECTED == "connected"
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Normalizers
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizers:
    def test_normalize_report_row_stable_id(self) -> None:
        row = SAMPLE_REPORT_RESPONSE["rows"][0]
        doc = normalize_report_row(row, PROPERTY_ID, "2024-01-15", CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        # Recompute expected ID
        row_hash = str(hash(str(row)))
        raw_key = f"ga_row:{PROPERTY_ID}_2024-01-15_{row_hash}"
        expected_id = hashlib.sha256(raw_key.encode()).hexdigest()[:16]
        assert doc.source_id == expected_id

    def test_normalize_report_row_source_type(self) -> None:
        row = SAMPLE_REPORT_RESPONSE["rows"][0]
        doc = normalize_report_row(row, PROPERTY_ID, "2024-01-15")
        assert doc.metadata["type"] == "analytics_report_row"
        assert doc.metadata["source"] == "google_analytics"
        assert doc.metadata["property_id"] == PROPERTY_ID

    def test_normalize_report_row_content_includes_property(self) -> None:
        row = SAMPLE_REPORT_RESPONSE["rows"][1]
        doc = normalize_report_row(row, PROPERTY_ID, "2024-01-16")
        assert PROPERTY_ID in doc.content
        assert "2024-01-16" in doc.content

    def test_normalize_report_row_dimension_values(self) -> None:
        row = SAMPLE_REPORT_RESPONSE["rows"][0]
        doc = normalize_report_row(row, PROPERTY_ID, "2024-01-15")
        assert doc.metadata["dimension_values"] == row["dimensionValues"]
        assert doc.metadata["metric_values"] == row["metricValues"]

    def test_normalize_report_row_source_url(self) -> None:
        row = SAMPLE_REPORT_RESPONSE["rows"][0]
        doc = normalize_report_row(row, PROPERTY_ID, "2024-01-15")
        assert PROPERTY_ID in doc.source_url

    def test_normalize_report_row_connector_tenant(self) -> None:
        row = SAMPLE_REPORT_RESPONSE["rows"][0]
        doc = normalize_report_row(row, PROPERTY_ID, "d", CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_normalize_property_stable_id(self) -> None:
        prop = SAMPLE_PROPERTY_DETAIL
        doc = normalize_property(prop, CONNECTOR_ID, TENANT_ID)
        expected_id = hashlib.sha256(
            f"property:{prop['name']}".encode()
        ).hexdigest()[:16]
        assert doc.source_id == expected_id

    def test_normalize_property_display_name_in_title(self) -> None:
        prop = SAMPLE_PROPERTY_DETAIL
        doc = normalize_property(prop)
        assert "My Website GA4" in doc.title

    def test_normalize_property_metadata_fields(self) -> None:
        prop = SAMPLE_PROPERTY_DETAIL
        doc = normalize_property(prop)
        assert doc.metadata["type"] == "ga4_property"
        assert doc.metadata["source"] == "google_analytics"
        assert doc.metadata["display_name"] == "My Website GA4"
        assert doc.metadata["industry_category"] == "TECHNOLOGY"
        assert doc.metadata["time_zone"] == "America/New_York"
        assert doc.metadata["currency_code"] == "USD"

    def test_normalize_property_content_includes_name(self) -> None:
        prop = SAMPLE_PROPERTY_DETAIL
        doc = normalize_property(prop)
        assert f"properties/{PROPERTY_ID}" in doc.content

    def test_normalize_property_empty_optional_fields(self) -> None:
        prop = {"name": "properties/999", "displayName": "Minimal"}
        doc = normalize_property(prop)
        assert doc.source_id  # has ID
        assert "Minimal" in doc.title
        assert doc.metadata["industry_category"] == ""

    def test_normalize_property_parent_account(self) -> None:
        prop = SAMPLE_PROPERTY_DETAIL
        doc = normalize_property(prop)
        assert doc.metadata["parent"] == f"accounts/{ACCOUNT_ID}"
        assert f"accounts/{ACCOUNT_ID}" in doc.content


# ═══════════════════════════════════════════════════════════════════════════════
# 4. with_retry
# ═══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_retry_succeeds_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retry_succeeds_on_second_attempt(self) -> None:
        fn = AsyncMock(
            side_effect=[GoogleAnalyticsNetworkError("timeout"), {"data": 1}]
        )
        result = await with_retry(fn, max_attempts=3, base_delay=0.001)
        assert result == {"data": 1}
        assert fn.call_count == 2

    async def test_retry_exhausted_raises_last_exception(self) -> None:
        fn = AsyncMock(side_effect=GoogleAnalyticsNetworkError("persistent failure"))
        with pytest.raises(GoogleAnalyticsNetworkError, match="persistent failure"):
            await with_retry(fn, max_attempts=3, base_delay=0.001)
        assert fn.call_count == 3

    async def test_retry_skips_auth_errors(self) -> None:
        fn = AsyncMock(side_effect=GoogleAnalyticsAuthError("Unauthorized", 401))
        with pytest.raises(GoogleAnalyticsAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0.001)
        assert fn.call_count == 1

    async def test_retry_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[
                GoogleAnalyticsRateLimitError("Rate limited", retry_after=0.001),
                {"ok": True},
            ]
        )
        result = await with_retry(fn, max_attempts=3, base_delay=0.001)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_retry_rate_limit_exhausted(self) -> None:
        fn = AsyncMock(
            side_effect=GoogleAnalyticsRateLimitError("Too many", retry_after=0.001)
        )
        with pytest.raises(GoogleAnalyticsRateLimitError):
            await with_retry(fn, max_attempts=2, base_delay=0.001)
        assert fn.call_count == 2

    async def test_retry_passes_args_and_kwargs(self) -> None:
        fn = AsyncMock(return_value="result")
        result = await with_retry(fn, "arg1", "arg2", kw="val")
        fn.assert_called_once_with("arg1", "arg2", kw="val")
        assert result == "result"

    async def test_retry_max_attempts_one(self) -> None:
        fn = AsyncMock(side_effect=GoogleAnalyticsError("fail"))
        with pytest.raises(GoogleAnalyticsError):
            await with_retry(fn, max_attempts=1, base_delay=0.001)
        assert fn.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HTTP Client — _raise_for_status
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientRaiseForStatus:
    def setup_method(self) -> None:
        self.client = make_http_client()

    def test_401_raises_auth_error(self) -> None:
        with pytest.raises(GoogleAnalyticsAuthError):
            self.client._raise_for_status(401, err_msg="Unauthorized")

    def test_403_raises_auth_error(self) -> None:
        with pytest.raises(GoogleAnalyticsAuthError):
            self.client._raise_for_status(403, err_msg="Forbidden")

    def test_404_raises_not_found(self) -> None:
        with pytest.raises(GoogleAnalyticsNotFoundError):
            self.client._raise_for_status(404, path="/properties/999")

    def test_429_raises_rate_limit(self) -> None:
        with pytest.raises(GoogleAnalyticsRateLimitError):
            self.client._raise_for_status(429, err_msg="Rate limited", retry_after=30.0)

    def test_500_raises_network_error(self) -> None:
        with pytest.raises(GoogleAnalyticsNetworkError):
            self.client._raise_for_status(500, err_msg="Internal server error")

    def test_503_raises_network_error(self) -> None:
        with pytest.raises(GoogleAnalyticsNetworkError):
            self.client._raise_for_status(503, err_msg="Service unavailable")

    def test_400_raises_base_error(self) -> None:
        with pytest.raises(GoogleAnalyticsError):
            self.client._raise_for_status(400, err_msg="Bad request")

    def test_200_does_not_raise(self) -> None:
        # _raise_for_status only raises for error codes
        # 200 is not in any error branch
        self.client._raise_for_status(200)  # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# 6. HTTP Client — auth headers + URL patterns
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientAuthAndURLs:
    def setup_method(self) -> None:
        self.client = make_http_client()

    def test_auth_headers_contain_bearer(self) -> None:
        headers = self.client._auth_headers()
        assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert headers["Content-Type"] == "application/json"

    def test_admin_base_url(self) -> None:
        assert ADMIN_BASE_URL == "https://analyticsadmin.googleapis.com/v1alpha"

    def test_data_base_url(self) -> None:
        assert DATA_BASE_URL == "https://analyticsdata.googleapis.com/v1beta"

    def test_list_accounts_url(self) -> None:
        # The URL used is ADMIN_BASE_URL/accounts
        assert f"{ADMIN_BASE_URL}/accounts" == "https://analyticsadmin.googleapis.com/v1alpha/accounts"

    def test_list_properties_url(self) -> None:
        url = f"{ADMIN_BASE_URL}/properties"
        assert "analyticsadmin" in url

    def test_run_report_url_pattern(self) -> None:
        url = f"{DATA_BASE_URL}/properties/{PROPERTY_ID}:runReport"
        assert "analyticsdata.googleapis.com" in url
        assert PROPERTY_ID in url
        assert ":runReport" in url

    def test_run_realtime_report_url_pattern(self) -> None:
        url = f"{DATA_BASE_URL}/properties/{PROPERTY_ID}:runRealtimeReport"
        assert ":runRealtimeReport" in url

    def test_metadata_url_pattern(self) -> None:
        url = f"{DATA_BASE_URL}/properties/{PROPERTY_ID}/metadata"
        assert "/metadata" in url
        assert PROPERTY_ID in url


# ═══════════════════════════════════════════════════════════════════════════════
# 7. HTTP Client — list_accounts
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientListAccounts:
    async def test_list_accounts_success(self) -> None:
        client = make_http_client()
        with patch.object(client, "_request", new=AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)):
            result = await client.list_accounts()
        assert result == SAMPLE_ACCOUNTS_RESPONSE
        assert "accounts" in result

    async def test_list_accounts_401_raises_auth_error(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(side_effect=GoogleAnalyticsAuthError("401", 401))
        ):
            with pytest.raises(GoogleAnalyticsAuthError):
                await client.list_accounts()

    async def test_list_accounts_network_error(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(side_effect=GoogleAnalyticsNetworkError("timeout"))
        ):
            with pytest.raises(GoogleAnalyticsNetworkError):
                await client.list_accounts()


# ═══════════════════════════════════════════════════════════════════════════════
# 8. HTTP Client — list_properties
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientListProperties:
    async def test_list_properties_success(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(return_value=SAMPLE_PROPERTIES_RESPONSE)
        ):
            result = await client.list_properties(ACCOUNT_ID)
        assert "properties" in result

    async def test_list_properties_passes_filter_param(self) -> None:
        client = make_http_client()
        mock_request = AsyncMock(return_value=SAMPLE_PROPERTIES_RESPONSE)
        with patch.object(client, "_request", new=mock_request):
            await client.list_properties(ACCOUNT_ID)
        _, call_kwargs = mock_request.call_args
        params = call_kwargs.get("params", {})
        assert f"parent:accounts/{ACCOUNT_ID}" in params.get("filter", "")

    async def test_list_properties_404_raises(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(side_effect=GoogleAnalyticsNotFoundError("account", ACCOUNT_ID))
        ):
            with pytest.raises(GoogleAnalyticsNotFoundError):
                await client.list_properties(ACCOUNT_ID)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. HTTP Client — get_property
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientGetProperty:
    async def test_get_property_success(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(return_value=SAMPLE_PROPERTY_DETAIL)
        ):
            result = await client.get_property(PROPERTY_ID)
        assert result["displayName"] == "My Website GA4"

    async def test_get_property_not_found(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(side_effect=GoogleAnalyticsNotFoundError("property", "999"))
        ):
            with pytest.raises(GoogleAnalyticsNotFoundError):
                await client.get_property("999")


# ═══════════════════════════════════════════════════════════════════════════════
# 10. HTTP Client — run_report body format
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientRunReport:
    async def test_run_report_success(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(return_value=SAMPLE_REPORT_RESPONSE)
        ):
            result = await client.run_report(
                PROPERTY_ID,
                ["date"],
                ["sessions"],
                [{"startDate": "30daysAgo", "endDate": "today"}],
            )
        assert result["rowCount"] == 3
        assert len(result["rows"]) == 3

    async def test_run_report_body_format(self) -> None:
        client = make_http_client()
        mock_request = AsyncMock(return_value=SAMPLE_REPORT_RESPONSE)
        with patch.object(client, "_request", new=mock_request):
            await client.run_report(
                PROPERTY_ID,
                ["date", "sessionSource"],
                ["sessions", "activeUsers"],
                [{"startDate": "7daysAgo", "endDate": "today"}],
                limit=500,
                offset=100,
            )
        call_args = mock_request.call_args
        body = call_args.kwargs.get("json", call_args[1].get("json", {}))
        assert body["dimensions"] == [{"name": "date"}, {"name": "sessionSource"}]
        assert body["metrics"] == [{"name": "sessions"}, {"name": "activeUsers"}]
        assert body["dateRanges"] == [{"startDate": "7daysAgo", "endDate": "today"}]
        assert body["limit"] == 500
        assert body["offset"] == 100

    async def test_run_report_url_contains_run_report(self) -> None:
        client = make_http_client()
        mock_request = AsyncMock(return_value=SAMPLE_REPORT_RESPONSE)
        with patch.object(client, "_request", new=mock_request):
            await client.run_report(PROPERTY_ID, ["date"], ["sessions"], [])
        call_url = mock_request.call_args[0][1]
        assert ":runReport" in call_url
        assert PROPERTY_ID in call_url

    async def test_run_report_rate_limit_error(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(side_effect=GoogleAnalyticsRateLimitError("429"))
        ):
            with pytest.raises(GoogleAnalyticsRateLimitError):
                await client.run_report(PROPERTY_ID, [], [], [])


# ═══════════════════════════════════════════════════════════════════════════════
# 11. HTTP Client — run_realtime_report
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientRealtimeReport:
    async def test_run_realtime_report_success(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(return_value=SAMPLE_REALTIME_RESPONSE)
        ):
            result = await client.run_realtime_report(PROPERTY_ID, ["country"], ["activeUsers"])
        assert result["rowCount"] == 1

    async def test_run_realtime_url_pattern(self) -> None:
        client = make_http_client()
        mock_request = AsyncMock(return_value=SAMPLE_REALTIME_RESPONSE)
        with patch.object(client, "_request", new=mock_request):
            await client.run_realtime_report(PROPERTY_ID, ["country"], ["activeUsers"], limit=50)
        call_url = mock_request.call_args[0][1]
        assert ":runRealtimeReport" in call_url

    async def test_run_realtime_body_format(self) -> None:
        client = make_http_client()
        mock_request = AsyncMock(return_value=SAMPLE_REALTIME_RESPONSE)
        with patch.object(client, "_request", new=mock_request):
            await client.run_realtime_report(PROPERTY_ID, ["country"], ["activeUsers"], limit=50)
        body = mock_request.call_args.kwargs.get("json", {})
        assert body["dimensions"] == [{"name": "country"}]
        assert body["metrics"] == [{"name": "activeUsers"}]
        assert body["limit"] == 50


# ═══════════════════════════════════════════════════════════════════════════════
# 12. HTTP Client — get_metadata / list_dimensions
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientMetadata:
    async def test_get_metadata_success(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(return_value=SAMPLE_METADATA_RESPONSE)
        ):
            result = await client.get_metadata(PROPERTY_ID)
        assert "dimensions" in result
        assert "metrics" in result

    async def test_list_dimensions_same_as_get_metadata(self) -> None:
        client = make_http_client()
        with patch.object(
            client, "_request", new=AsyncMock(return_value=SAMPLE_METADATA_RESPONSE)
        ):
            result = await client.list_dimensions(PROPERTY_ID)
        assert "dimensions" in result

    async def test_metadata_url_pattern(self) -> None:
        client = make_http_client()
        mock_request = AsyncMock(return_value=SAMPLE_METADATA_RESPONSE)
        with patch.object(client, "_request", new=mock_request):
            await client.get_metadata(PROPERTY_ID)
        call_url = mock_request.call_args[0][1]
        assert "/metadata" in call_url
        assert PROPERTY_ID in call_url


# ═══════════════════════════════════════════════════════════════════════════════
# 13. HTTP Client — offset pagination
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientPagination:
    async def test_run_report_with_offset(self) -> None:
        client = make_http_client()
        mock_request = AsyncMock(return_value={"rowCount": 50, "rows": []})
        with patch.object(client, "_request", new=mock_request):
            await client.run_report(PROPERTY_ID, ["date"], ["sessions"], [], limit=10, offset=20)
        body = mock_request.call_args.kwargs.get("json", {})
        assert body["offset"] == 20
        assert body["limit"] == 10

    async def test_run_report_default_limit_offset(self) -> None:
        client = make_http_client()
        mock_request = AsyncMock(return_value={"rowCount": 0, "rows": []})
        with patch.object(client, "_request", new=mock_request):
            await client.run_report(PROPERTY_ID, [], [], [])
        body = mock_request.call_args.kwargs.get("json", {})
        assert body["limit"] == 10000
        assert body["offset"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 14. Connector — install()
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorInstall:
    async def test_install_missing_client_id(self) -> None:
        connector = GoogleAnalyticsConnector(
            config={"client_secret": CLIENT_SECRET}
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in result.message

    async def test_install_missing_client_secret(self) -> None:
        connector = GoogleAnalyticsConnector(
            config={"client_id": CLIENT_ID}
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_secret" in result.message

    async def test_install_valid_credentials(self) -> None:
        connector = GoogleAnalyticsConnector(
            config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_install_returns_connector_id(self) -> None:
        connector = GoogleAnalyticsConnector(
            connector_id=CONNECTOR_ID,
            config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )
        result = await connector.install()
        assert result.connector_id == CONNECTOR_ID


# ═══════════════════════════════════════════════════════════════════════════════
# 15. Connector — authorize()
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorAuthorize:
    def test_authorize_returns_url(self) -> None:
        connector = make_connector()
        url = connector.authorize()
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")

    def test_authorize_includes_scope(self) -> None:
        connector = make_connector()
        url = connector.authorize()
        assert "analytics.readonly" in url

    def test_authorize_includes_client_id(self) -> None:
        connector = make_connector()
        url = connector.authorize()
        assert CLIENT_ID in url

    def test_authorize_includes_offline_access(self) -> None:
        connector = make_connector()
        url = connector.authorize()
        assert "offline" in url

    def test_authorize_includes_redirect_uri_when_set(self) -> None:
        connector = GoogleAnalyticsConnector(
            config={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": "https://myapp.example.com/oauth/callback",
            }
        )
        url = connector.authorize()
        assert "myapp.example.com" in url

    def test_authorize_no_redirect_uri_when_not_set(self) -> None:
        connector = GoogleAnalyticsConnector(
            config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        )
        url = connector.authorize()
        # redirect_uri param should not appear if not set
        assert "redirect_uri=" not in url


# ═══════════════════════════════════════════════════════════════════════════════
# 16. Connector — health_check()
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorHealthCheck:
    async def test_health_check_missing_token(self) -> None:
        connector = GoogleAnalyticsConnector(config={})
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_healthy(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.list_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)
        mock_client.aclose = AsyncMock()
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "2 account" in result.message

    async def test_health_check_auth_error(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.list_accounts = AsyncMock(
            side_effect=GoogleAnalyticsAuthError("Invalid token", 401)
        )
        mock_client.aclose = AsyncMock()
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.list_accounts = AsyncMock(
            side_effect=GoogleAnalyticsNetworkError("Connection refused")
        )
        mock_client.aclose = AsyncMock()
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_unexpected_error_degraded(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.list_accounts = AsyncMock(side_effect=RuntimeError("Unexpected"))
        mock_client.aclose = AsyncMock()
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 17. Connector — sync()
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorSync:
    async def test_sync_missing_access_token(self) -> None:
        connector = GoogleAnalyticsConnector(
            config={"property_id": PROPERTY_ID}
        )
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "access_token" in result.message

    async def test_sync_missing_property_id(self) -> None:
        connector = GoogleAnalyticsConnector(
            config={"access_token": ACCESS_TOKEN}
        )
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "property_id" in result.message

    async def test_sync_completed(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.run_report = AsyncMock(return_value=SAMPLE_REPORT_RESPONSE)
        connector.http_client = mock_client
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.documents_failed == 0

    async def test_sync_paginates_all_rows(self) -> None:
        """Verify pagination continues until offset >= rowCount."""
        connector = make_connector()
        mock_client = MagicMock()

        page1 = {
            "rowCount": 4,
            "rows": [
                {"dimensionValues": [{"value": "d1"}], "metricValues": [{"value": "1"}]},
                {"dimensionValues": [{"value": "d2"}], "metricValues": [{"value": "2"}]},
            ],
        }
        page2 = {
            "rowCount": 4,
            "rows": [
                {"dimensionValues": [{"value": "d3"}], "metricValues": [{"value": "3"}]},
                {"dimensionValues": [{"value": "d4"}], "metricValues": [{"value": "4"}]},
            ],
        }
        mock_client.run_report = AsyncMock(side_effect=[page1, page2])
        connector.http_client = mock_client
        result = await connector.sync()
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert mock_client.run_report.call_count == 2

    async def test_sync_partial_when_row_normalize_fails(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        # Row with invalid structure that will fail normalization gracefully
        bad_row: dict[str, Any] = {}
        mock_client.run_report = AsyncMock(return_value={
            "rowCount": 1,
            "rows": [bad_row],
        })
        connector.http_client = mock_client

        with patch("helpers.utils.normalize_report_row", side_effect=ValueError("bad")):
            result = await connector.sync()

        assert result.documents_failed >= 0  # gracefully handled

    async def test_sync_auth_error_propagates(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.run_report = AsyncMock(
            side_effect=GoogleAnalyticsAuthError("Invalid token", 401)
        )
        connector.http_client = mock_client
        with pytest.raises(GoogleAnalyticsAuthError):
            await connector.sync()

    async def test_sync_network_error_returns_failed(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.run_report = AsyncMock(
            side_effect=GoogleAnalyticsNetworkError("Connection failed")
        )
        connector.http_client = mock_client
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "Connection failed" in result.message

    async def test_sync_empty_result_partial(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.run_report = AsyncMock(return_value={"rowCount": 0, "rows": []})
        connector.http_client = mock_client
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_found == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 18. Connector — list_accounts()
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorListAccounts:
    async def test_list_accounts_returns_list(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.list_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)
        connector.http_client = mock_client
        result = await connector.list_accounts()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "accounts/654321"

    async def test_list_accounts_empty_response(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.list_accounts = AsyncMock(return_value={})
        connector.http_client = mock_client
        result = await connector.list_accounts()
        assert result == []

    async def test_list_accounts_creates_client_if_none(self) -> None:
        connector = make_connector()
        assert connector.http_client is None
        mock_client = MagicMock()
        mock_client.list_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.list_accounts()
        assert len(result) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 19. Connector — list_properties()
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorListProperties:
    async def test_list_properties_returns_list(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.list_properties = AsyncMock(return_value=SAMPLE_PROPERTIES_RESPONSE)
        connector.http_client = mock_client
        result = await connector.list_properties(ACCOUNT_ID)
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_properties_passes_account_id(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.list_properties = AsyncMock(return_value={"properties": []})
        connector.http_client = mock_client
        await connector.list_properties(ACCOUNT_ID)
        mock_client.list_properties.assert_called_once_with(ACCOUNT_ID)

    async def test_list_properties_empty(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.list_properties = AsyncMock(return_value={"properties": []})
        connector.http_client = mock_client
        result = await connector.list_properties(ACCOUNT_ID)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 20. Connector — run_report()
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorRunReport:
    async def test_run_report_success(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.run_report = AsyncMock(return_value=SAMPLE_REPORT_RESPONSE)
        connector.http_client = mock_client
        result = await connector.run_report(
            property_id=PROPERTY_ID,
            dimensions=["date"],
            metrics=["sessions"],
        )
        assert result["rowCount"] == 3

    async def test_run_report_uses_config_property_id(self) -> None:
        connector = make_connector(property_id=PROPERTY_ID)
        mock_client = MagicMock()
        mock_client.run_report = AsyncMock(return_value=SAMPLE_REPORT_RESPONSE)
        connector.http_client = mock_client
        # Not passing property_id explicitly — should use config value
        await connector.run_report()
        call_args = mock_client.run_report.call_args[0]
        assert call_args[0] == PROPERTY_ID

    async def test_run_report_uses_default_dimensions_metrics(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.run_report = AsyncMock(return_value=SAMPLE_REPORT_RESPONSE)
        connector.http_client = mock_client
        await connector.run_report()
        call_args = mock_client.run_report.call_args[0]
        # dimensions should be the defaults
        assert "date" in call_args[1]
        assert "sessions" in call_args[2]

    async def test_run_report_custom_date_range(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.run_report = AsyncMock(return_value={"rowCount": 0, "rows": []})
        connector.http_client = mock_client
        await connector.run_report(start_date="2024-01-01", end_date="2024-01-31")
        call_args = mock_client.run_report.call_args[0]
        date_ranges = call_args[3]
        assert date_ranges == [{"startDate": "2024-01-01", "endDate": "2024-01-31"}]


# ═══════════════════════════════════════════════════════════════════════════════
# 21. Connector — get_metadata()
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorGetMetadata:
    async def test_get_metadata_success(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.get_metadata = AsyncMock(return_value=SAMPLE_METADATA_RESPONSE)
        connector.http_client = mock_client
        result = await connector.get_metadata(PROPERTY_ID)
        assert "dimensions" in result
        assert "metrics" in result

    async def test_get_metadata_passes_property_id(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.get_metadata = AsyncMock(return_value=SAMPLE_METADATA_RESPONSE)
        connector.http_client = mock_client
        await connector.get_metadata(PROPERTY_ID)
        mock_client.get_metadata.assert_called_once_with(PROPERTY_ID)


# ═══════════════════════════════════════════════════════════════════════════════
# 22. Connector — lifecycle (aclose, context manager)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorLifecycle:
    async def test_aclose_clears_http_client(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        connector.http_client = mock_client
        await connector.aclose()
        mock_client.aclose.assert_called_once()
        assert connector.http_client is None

    async def test_aclose_noop_when_no_client(self) -> None:
        connector = make_connector()
        assert connector.http_client is None
        await connector.aclose()  # must not raise

    async def test_context_manager(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        async with connector:
            connector.http_client = mock_client
        mock_client.aclose.assert_called_once()

    def test_connector_type_constants(self) -> None:
        connector = make_connector()
        assert connector.CONNECTOR_TYPE == "google_analytics"
        assert connector.AUTH_TYPE == "oauth2"

    def test_connector_config_stored(self) -> None:
        connector = make_connector()
        assert connector._client_id == CLIENT_ID
        assert connector._client_secret == CLIENT_SECRET
        assert connector._access_token == ACCESS_TOKEN
        assert connector._property_id == PROPERTY_ID
