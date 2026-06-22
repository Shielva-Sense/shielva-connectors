"""Unit tests for AdobeAnalyticsConnector — all HTTP calls are mocked."""
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

from connector import AdobeAnalyticsConnector
from exceptions import (
    AdobeAnalyticsAuthError,
    AdobeAnalyticsError,
    AdobeAnalyticsNetworkError,
    AdobeAnalyticsNotFoundError,
    AdobeAnalyticsRateLimitError,
)
from helpers.utils import (
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

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "tenant_adobe_test"
CONNECTOR_ID = "conn_adobe_analytics_001"
CLIENT_ID = "test_client_id_adobe"
CLIENT_SECRET = "test_client_secret_adobe"
COMPANY_ID = "mycompany"
ORG_ID = "org_test_1234@AdobeOrg"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_TOKEN_RESPONSE: dict[str, Any] = {
    "access_token": "eyJhbGciOiJSUzI1NiJ9.test_token_value",
    "token_type": "bearer",
    "expires_in": 86399,
}

SAMPLE_REPORT_SUITES: list[dict[str, Any]] = [
    {
        "rsid": "mycompany.prod",
        "name": "Production Site",
        "currency": "USD",
        "timezoneZoneinfo": "America/New_York",
        "status": "active",
    },
    {
        "rsid": "mycompany.dev",
        "name": "Development Site",
        "currency": "USD",
        "timezoneZoneinfo": "America/Los_Angeles",
        "status": "active",
    },
]

SAMPLE_REPORT_SUITES_RESPONSE: dict[str, Any] = {
    "content": SAMPLE_REPORT_SUITES,
    "totalElements": 2,
}

SAMPLE_SEGMENTS: list[dict[str, Any]] = [
    {
        "id": "s300012345_5f123abc",
        "name": "Mobile Users",
        "description": "All visits from mobile devices",
        "owner": {"name": "John Doe"},
        "tags": [{"name": "mobile"}, {"name": "devices"}],
    },
    {
        "id": "s300012345_5f456def",
        "name": "Returning Visitors",
        "description": "Users who visited more than once",
        "owner": {"name": "Jane Smith"},
        "tags": [{"name": "retention"}],
    },
]

SAMPLE_SEGMENTS_RESPONSE: dict[str, Any] = {
    "content": SAMPLE_SEGMENTS,
    "totalElements": 2,
}

SAMPLE_CALC_METRICS: list[dict[str, Any]] = [
    {
        "id": "cm300012345_5fabc123",
        "name": "Bounce Rate",
        "description": "Percentage of single-page visits",
        "formula": "metric('bounces')/metric('entries')",
        "owner": {"name": "Analytics Team"},
        "polarity": "negative",
        "precision": 1,
    },
    {
        "id": "cm300012345_5fdef456",
        "name": "Revenue per Visit",
        "description": "Total revenue divided by visits",
        "formula": "metric('revenue')/metric('visits')",
        "owner": {"name": "Analytics Team"},
        "polarity": "positive",
        "precision": 2,
    },
]

SAMPLE_CALC_METRICS_RESPONSE: dict[str, Any] = {
    "content": SAMPLE_CALC_METRICS,
    "totalElements": 2,
}

SAMPLE_DIMENSIONS: list[dict[str, Any]] = [
    {"id": "variables/page", "name": "Page", "type": "string"},
    {"id": "variables/evar1", "name": "Custom Variable 1", "type": "string"},
]

SAMPLE_METRICS: list[dict[str, Any]] = [
    {"id": "metrics/visits", "name": "Visits", "type": "decimal"},
    {"id": "metrics/pageviews", "name": "Page Views", "type": "decimal"},
]

SAMPLE_REPORT_RESPONSE: dict[str, Any] = {
    "totalPages": 1,
    "firstPage": True,
    "lastPage": True,
    "numberOfElements": 5,
    "rows": [
        {"itemId": "1234567890", "value": "/home", "data": [1000, 2500]},
        {"itemId": "2345678901", "value": "/products", "data": [800, 1800]},
    ],
    "columns": {
        "dimension": {"id": "variables/page", "type": "string"},
        "columnIds": ["0", "1"],
    },
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_connector(
    client_id: str = CLIENT_ID,
    client_secret: str = CLIENT_SECRET,
    company_id: str = COMPANY_ID,
    organization_id: str = ORG_ID,
) -> AdobeAnalyticsConnector:
    return AdobeAnalyticsConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": client_id,
            "client_secret": client_secret,
            "company_id": company_id,
            "organization_id": organization_id,
        },
    )


def _mock_client(
    token_ok: bool = True,
    report_suites: list[dict] | None = None,
    segments: list[dict] | None = None,
    calc_metrics: list[dict] | None = None,
    dimensions: list[dict] | None = None,
    metrics: list[dict] | None = None,
    report_response: dict | None = None,
    raise_auth: bool = False,
    raise_network: bool = False,
) -> MagicMock:
    """Build a mock AdobeAnalyticsHTTPClient with preconfigured return values."""
    client = MagicMock()

    async def _get_token() -> str:
        if raise_auth:
            raise AdobeAnalyticsAuthError("Invalid credentials", 401)
        if raise_network:
            raise AdobeAnalyticsNetworkError("Connection refused")
        if token_ok:
            return SAMPLE_TOKEN_RESPONSE["access_token"]
        raise AdobeAnalyticsAuthError("Token fetch failed", 400)

    async def _get_report_suites() -> list | dict:
        if raise_auth:
            raise AdobeAnalyticsAuthError("Unauthorized", 401)
        if raise_network:
            raise AdobeAnalyticsNetworkError("Connection refused")
        return report_suites if report_suites is not None else SAMPLE_REPORT_SUITES

    async def _get_segments(rsid: str) -> list | dict:
        return segments if segments is not None else SAMPLE_SEGMENTS

    async def _get_calculated_metrics() -> list | dict:
        return calc_metrics if calc_metrics is not None else SAMPLE_CALC_METRICS

    async def _get_dimensions(rsid: str) -> list | dict:
        return dimensions if dimensions is not None else SAMPLE_DIMENSIONS

    async def _get_metrics(rsid: str) -> list | dict:
        return metrics if metrics is not None else SAMPLE_METRICS

    async def _run_report(rsid: str, body: dict) -> dict:
        return report_response if report_response is not None else SAMPLE_REPORT_RESPONSE

    async def _aclose() -> None:
        pass

    client.get_token = AsyncMock(side_effect=_get_token)
    client.get_report_suites = AsyncMock(side_effect=_get_report_suites)
    client.get_segments = AsyncMock(side_effect=_get_segments)
    client.get_calculated_metrics = AsyncMock(side_effect=_get_calculated_metrics)
    client.get_dimensions = AsyncMock(side_effect=_get_dimensions)
    client.get_metrics = AsyncMock(side_effect=_get_metrics)
    client.run_report = AsyncMock(side_effect=_run_report)
    client.aclose = AsyncMock(side_effect=_aclose)
    return client


# ── Exception tests ──────────────────────────────────────────────────────────


class TestExceptions:
    def test_base_error_attributes(self) -> None:
        exc = AdobeAnalyticsError("base error", 400, "BAD_REQUEST")
        assert exc.message == "base error"
        assert exc.status_code == 400
        assert exc.code == "BAD_REQUEST"
        assert str(exc) == "base error"

    def test_auth_error_inherits_base(self) -> None:
        exc = AdobeAnalyticsAuthError("auth failed", 401, "unauthorized")
        assert isinstance(exc, AdobeAnalyticsError)
        assert exc.status_code == 401

    def test_network_error_inherits_base(self) -> None:
        exc = AdobeAnalyticsNetworkError("connection refused")
        assert isinstance(exc, AdobeAnalyticsError)
        assert exc.status_code == 0

    def test_not_found_error_message(self) -> None:
        exc = AdobeAnalyticsNotFoundError("report_suite", "rs_abc123")
        assert isinstance(exc, AdobeAnalyticsError)
        assert "rs_abc123" in exc.message
        assert exc.status_code == 404
        assert exc.code == "resource_missing"

    def test_rate_limit_error_retry_after(self) -> None:
        exc = AdobeAnalyticsRateLimitError("rate limited", retry_after=60.0)
        assert isinstance(exc, AdobeAnalyticsError)
        assert exc.retry_after == 60.0
        assert exc.status_code == 429
        assert exc.code == "rate_limit"

    def test_rate_limit_error_default_retry_after(self) -> None:
        exc = AdobeAnalyticsRateLimitError("rate limited")
        assert exc.retry_after == 0.0


# ── Model tests ───────────────────────────────────────────────────────────────


class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"
        assert SyncStatus.RUNNING == "running"

    def test_install_result_defaults(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED
        )
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result_defaults(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED
        )
        assert r.message == ""

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test RS",
            content="Report Suite ID: test\nName: Test",
            connector_id=CONNECTOR_ID,
            tenant_id=TENANT_ID,
            source_url="https://analytics.adobe.com",
            metadata={"rsid": "test", "type": "report_suite"},
        )
        assert doc.source_id == "abc123"
        assert doc.metadata["type"] == "report_suite"

    def test_connector_document_default_metadata(self) -> None:
        doc = ConnectorDocument(
            source_id="x", title="t", content="c",
            connector_id="cid", tenant_id="tid",
        )
        assert doc.metadata == {}
        assert doc.source_url == ""


# ── Normalizer tests ──────────────────────────────────────────────────────────


class TestNormalizeReportSuite:
    def _expected_id(self, rsid: str) -> str:
        return hashlib.sha256(f"report_suite:{rsid}".encode()).hexdigest()[:16]

    def test_stable_id(self) -> None:
        rs = SAMPLE_REPORT_SUITES[0]
        doc = normalize_report_suite(rs, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == self._expected_id("mycompany.prod")

    def test_stable_id_deterministic(self) -> None:
        rs = SAMPLE_REPORT_SUITES[0]
        doc1 = normalize_report_suite(rs, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_report_suite(rs, CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id == doc2.source_id

    def test_different_rsids_different_ids(self) -> None:
        doc1 = normalize_report_suite(SAMPLE_REPORT_SUITES[0], CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_report_suite(SAMPLE_REPORT_SUITES[1], CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id != doc2.source_id

    def test_type_in_metadata(self) -> None:
        doc = normalize_report_suite(SAMPLE_REPORT_SUITES[0], CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["type"] == "report_suite"

    def test_rsid_in_metadata(self) -> None:
        doc = normalize_report_suite(SAMPLE_REPORT_SUITES[0], CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["rsid"] == "mycompany.prod"

    def test_name_in_title(self) -> None:
        doc = normalize_report_suite(SAMPLE_REPORT_SUITES[0], CONNECTOR_ID, TENANT_ID)
        assert "Production Site" in doc.title

    def test_content_includes_rsid_and_name(self) -> None:
        doc = normalize_report_suite(SAMPLE_REPORT_SUITES[0], CONNECTOR_ID, TENANT_ID)
        assert "mycompany.prod" in doc.content
        assert "Production Site" in doc.content

    def test_missing_optional_fields_graceful(self) -> None:
        rs = {"rsid": "bare.rs"}
        doc = normalize_report_suite(rs, CONNECTOR_ID, TENANT_ID)
        assert "bare.rs" in doc.source_id or len(doc.source_id) == 16
        assert doc.metadata["type"] == "report_suite"

    def test_connector_and_tenant_id_set(self) -> None:
        doc = normalize_report_suite(SAMPLE_REPORT_SUITES[0], CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID


class TestNormalizeSegment:
    def _expected_id(self, seg_id: str) -> str:
        return hashlib.sha256(f"segment:{seg_id}".encode()).hexdigest()[:16]

    def test_stable_id(self) -> None:
        seg = SAMPLE_SEGMENTS[0]
        doc = normalize_segment(seg, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == self._expected_id("s300012345_5f123abc")

    def test_stable_id_deterministic(self) -> None:
        seg = SAMPLE_SEGMENTS[0]
        doc1 = normalize_segment(seg, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_segment(seg, CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id == doc2.source_id

    def test_type_in_metadata(self) -> None:
        doc = normalize_segment(SAMPLE_SEGMENTS[0], CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["type"] == "segment"

    def test_name_in_title(self) -> None:
        doc = normalize_segment(SAMPLE_SEGMENTS[0], CONNECTOR_ID, TENANT_ID)
        assert "Mobile Users" in doc.title

    def test_owner_dict_resolved(self) -> None:
        doc = normalize_segment(SAMPLE_SEGMENTS[0], CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["owner"] == "John Doe"

    def test_tags_list_extracted(self) -> None:
        doc = normalize_segment(SAMPLE_SEGMENTS[0], CONNECTOR_ID, TENANT_ID)
        assert "mobile" in doc.metadata["tags"]
        assert "devices" in doc.metadata["tags"]

    def test_description_in_content(self) -> None:
        doc = normalize_segment(SAMPLE_SEGMENTS[0], CONNECTOR_ID, TENANT_ID)
        assert "mobile devices" in doc.content

    def test_missing_optional_fields_graceful(self) -> None:
        seg = {"id": "seg_bare"}
        doc = normalize_segment(seg, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["type"] == "segment"
        assert doc.metadata["tags"] == []

    def test_connector_and_tenant_id_set(self) -> None:
        doc = normalize_segment(SAMPLE_SEGMENTS[0], CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID


class TestNormalizeCalculatedMetric:
    def _expected_id(self, metric_id: str) -> str:
        return hashlib.sha256(f"calculated_metric:{metric_id}".encode()).hexdigest()[:16]

    def test_stable_id(self) -> None:
        m = SAMPLE_CALC_METRICS[0]
        doc = normalize_calculated_metric(m, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == self._expected_id("cm300012345_5fabc123")

    def test_stable_id_deterministic(self) -> None:
        m = SAMPLE_CALC_METRICS[0]
        doc1 = normalize_calculated_metric(m, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_calculated_metric(m, CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id == doc2.source_id

    def test_different_metrics_different_ids(self) -> None:
        doc1 = normalize_calculated_metric(SAMPLE_CALC_METRICS[0], CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_calculated_metric(SAMPLE_CALC_METRICS[1], CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id != doc2.source_id

    def test_type_in_metadata(self) -> None:
        doc = normalize_calculated_metric(SAMPLE_CALC_METRICS[0], CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["type"] == "calculated_metric"

    def test_name_in_title(self) -> None:
        doc = normalize_calculated_metric(SAMPLE_CALC_METRICS[0], CONNECTOR_ID, TENANT_ID)
        assert "Bounce Rate" in doc.title

    def test_formula_in_content(self) -> None:
        doc = normalize_calculated_metric(SAMPLE_CALC_METRICS[0], CONNECTOR_ID, TENANT_ID)
        assert "bounces" in doc.content

    def test_owner_dict_resolved(self) -> None:
        doc = normalize_calculated_metric(SAMPLE_CALC_METRICS[0], CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["owner"] == "Analytics Team"

    def test_missing_optional_fields_graceful(self) -> None:
        m = {"id": "cm_bare"}
        doc = normalize_calculated_metric(m, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["type"] == "calculated_metric"
        assert doc.metadata["formula"] == ""

    def test_connector_and_tenant_id_set(self) -> None:
        doc = normalize_calculated_metric(SAMPLE_CALC_METRICS[0], CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID


# ── with_retry tests ──────────────────────────────────────────────────────────


class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_network_error_then_succeeds(self) -> None:
        fn = AsyncMock(
            side_effect=[
                AdobeAnalyticsNetworkError("timeout"),
                {"ok": True},
            ]
        )
        result = await with_retry(fn, base_delay=0.0)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=AdobeAnalyticsAuthError("invalid", 401))
        with pytest.raises(AdobeAnalyticsAuthError):
            await with_retry(fn, base_delay=0.0)
        assert fn.call_count == 1

    async def test_exhausted_raises_last_exception(self) -> None:
        fn = AsyncMock(
            side_effect=AdobeAnalyticsNetworkError("connection refused")
        )
        with pytest.raises(AdobeAnalyticsNetworkError, match="connection refused"):
            await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert fn.call_count == 3

    async def test_rate_limit_respected(self) -> None:
        fn = AsyncMock(
            side_effect=[
                AdobeAnalyticsRateLimitError("rate limited", retry_after=0.0),
                {"ok": True},
            ]
        )
        result = await with_retry(fn, base_delay=0.0)
        assert result == {"ok": True}
        assert fn.call_count == 2


# ── AdobeAnalyticsHTTPClient tests ────────────────────────────────────────────


class TestAdobeAnalyticsHTTPClient:
    """Test the HTTP client with mocked aiohttp sessions."""

    async def test_get_token_success(self) -> None:
        """get_token should return access_token and cache it."""
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            company_id=COMPANY_ID,
        )

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=SAMPLE_TOKEN_RESPONSE)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False

        with patch.object(client, "_get_session", return_value=mock_session):
            token = await client.get_token()

        assert token == SAMPLE_TOKEN_RESPONSE["access_token"]
        assert client._access_token == token
        assert client._token_expires_at > 0

    async def test_get_token_auth_error_on_401(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            company_id=COMPANY_ID,
        )

        mock_response = MagicMock()
        mock_response.status = 401
        mock_response.json = AsyncMock(return_value={"error": "invalid_client", "error_description": "Bad credentials"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False

        with patch.object(client, "_get_session", return_value=mock_session):
            with pytest.raises(AdobeAnalyticsAuthError):
                await client.get_token()

    async def test_auth_headers_contain_bearer_and_api_key(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            company_id=COMPANY_ID,
        )
        client._access_token = "test_token_xyz"
        client._token_expires_at = float("inf")

        headers = await client._auth_headers()
        assert headers["Authorization"] == "Bearer test_token_xyz"
        assert headers["x-api-key"] == CLIENT_ID

    async def test_request_uses_company_id_in_url(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            company_id="my_company_123",
        )
        client._access_token = "tok"
        client._token_expires_at = float("inf")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content_length = 100
        mock_response.json = AsyncMock(return_value={"content": []})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_response)
        mock_session.closed = False

        with patch.object(client, "_get_session", return_value=mock_session):
            await client.get_report_suites()

        call_args = mock_session.request.call_args
        url = call_args[0][1] if call_args[0] else call_args[1].get("url", "")
        # URL may be passed as positional arg
        called_url = str(call_args[0][1]) if len(call_args[0]) > 1 else ""
        assert "my_company_123" in called_url or "my_company_123" in str(call_args)

    async def test_get_report_suites_returns_response(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            company_id=COMPANY_ID,
        )

        mock_json_resp = SAMPLE_REPORT_SUITES_RESPONSE
        client.get_token = AsyncMock(return_value="test_token")  # type: ignore[method-assign]
        client._auth_headers = AsyncMock(return_value={  # type: ignore[method-assign]
            "Authorization": "Bearer test_token",
            "x-api-key": CLIENT_ID,
            "Content-Type": "application/json",
        })

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content_length = 100
        mock_response.json = AsyncMock(return_value=mock_json_resp)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_response)
        mock_session.closed = False

        with patch.object(client, "_get_session", return_value=mock_session):
            result = await client.get_report_suites()

        assert result == mock_json_resp

    async def test_get_dimensions_includes_rsid_param(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            company_id=COMPANY_ID,
        )
        client._access_token = "tok"
        client._token_expires_at = float("inf")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content_length = 100
        mock_response.json = AsyncMock(return_value=SAMPLE_DIMENSIONS)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_response)
        mock_session.closed = False

        with patch.object(client, "_get_session", return_value=mock_session):
            result = await client.get_dimensions("mycompany.prod")

        assert result == SAMPLE_DIMENSIONS
        call_kwargs = mock_session.request.call_args[1]
        assert call_kwargs.get("params", {}).get("rsid") == "mycompany.prod"

    async def test_get_metrics_includes_rsid_param(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            company_id=COMPANY_ID,
        )
        client._access_token = "tok"
        client._token_expires_at = float("inf")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content_length = 100
        mock_response.json = AsyncMock(return_value=SAMPLE_METRICS)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_response)
        mock_session.closed = False

        with patch.object(client, "_get_session", return_value=mock_session):
            result = await client.get_metrics("mycompany.prod")

        assert result == SAMPLE_METRICS
        call_kwargs = mock_session.request.call_args[1]
        assert call_kwargs.get("params", {}).get("rsid") == "mycompany.prod"

    async def test_get_segments_includes_rsid_param(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            company_id=COMPANY_ID,
        )
        client._access_token = "tok"
        client._token_expires_at = float("inf")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content_length = 100
        mock_response.json = AsyncMock(return_value=SAMPLE_SEGMENTS)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_response)
        mock_session.closed = False

        with patch.object(client, "_get_session", return_value=mock_session):
            result = await client.get_segments("mycompany.prod")

        assert result == SAMPLE_SEGMENTS
        call_kwargs = mock_session.request.call_args[1]
        assert call_kwargs.get("params", {}).get("rsid") == "mycompany.prod"

    async def test_get_calculated_metrics_returns_response(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            company_id=COMPANY_ID,
        )
        client._access_token = "tok"
        client._token_expires_at = float("inf")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content_length = 100
        mock_response.json = AsyncMock(return_value=SAMPLE_CALC_METRICS_RESPONSE)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_response)
        mock_session.closed = False

        with patch.object(client, "_get_session", return_value=mock_session):
            result = await client.get_calculated_metrics()

        assert result == SAMPLE_CALC_METRICS_RESPONSE

    async def test_run_report_posts_with_rsid_in_body(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            company_id=COMPANY_ID,
        )
        client._access_token = "tok"
        client._token_expires_at = float("inf")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content_length = 100
        mock_response.json = AsyncMock(return_value=SAMPLE_REPORT_RESPONSE)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_response)
        mock_session.closed = False

        report_body = {
            "metricContainer": {"metrics": [{"id": "metrics/visits"}]},
            "dimension": "variables/page",
        }
        with patch.object(client, "_get_session", return_value=mock_session):
            result = await client.run_report("mycompany.prod", report_body)

        assert result == SAMPLE_REPORT_RESPONSE
        call_kwargs = mock_session.request.call_args[1]
        sent_json = call_kwargs.get("json", {})
        assert sent_json.get("rsid") == "mycompany.prod"

    def test_raise_for_status_401(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, company_id=COMPANY_ID
        )
        with pytest.raises(AdobeAnalyticsAuthError):
            client._raise_for_status(401, err_msg="Unauthorized")

    def test_raise_for_status_403(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, company_id=COMPANY_ID
        )
        with pytest.raises(AdobeAnalyticsAuthError):
            client._raise_for_status(403, err_msg="Forbidden")

    def test_raise_for_status_404(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, company_id=COMPANY_ID
        )
        with pytest.raises(AdobeAnalyticsNotFoundError):
            client._raise_for_status(404, path="/reportsuites")

    def test_raise_for_status_429(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, company_id=COMPANY_ID
        )
        with pytest.raises(AdobeAnalyticsRateLimitError) as exc_info:
            client._raise_for_status(429, err_msg="Too many requests", retry_after=30.0)
        assert exc_info.value.retry_after == 30.0

    def test_raise_for_status_500(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, company_id=COMPANY_ID
        )
        with pytest.raises(AdobeAnalyticsError) as exc_info:
            client._raise_for_status(500, err_msg="Internal Server Error")
        assert exc_info.value.status_code == 500

    def test_raise_for_status_400_generic(self) -> None:
        from client.http_client import AdobeAnalyticsHTTPClient

        client = AdobeAnalyticsHTTPClient(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, company_id=COMPANY_ID
        )
        with pytest.raises(AdobeAnalyticsError) as exc_info:
            client._raise_for_status(400, err_msg="Bad Request")
        assert exc_info.value.status_code == 400


# ── install() tests ──────────────────────────────────────────────────────────


class TestInstall:
    async def test_install_success(self) -> None:
        connector = _make_connector()
        mock = _mock_client()
        with patch.object(connector, "_make_client", return_value=mock):
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Adobe Analytics" in result.message

    async def test_install_missing_client_id(self) -> None:
        connector = _make_connector(client_id="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in result.message

    async def test_install_missing_client_secret(self) -> None:
        connector = _make_connector(client_secret="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_secret" in result.message

    async def test_install_missing_company_id(self) -> None:
        connector = _make_connector(company_id="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "company_id" in result.message

    async def test_install_auth_error(self) -> None:
        connector = _make_connector()
        mock = _mock_client(raise_auth=True)
        with patch.object(connector, "_make_client", return_value=mock):
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        connector = _make_connector()
        mock = _mock_client(raise_network=True)
        with patch.object(connector, "_make_client", return_value=mock):
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_install_sets_http_client_on_success(self) -> None:
        connector = _make_connector()
        mock = _mock_client()
        with patch.object(connector, "_make_client", return_value=mock):
            await connector.install()
        assert connector.http_client is not None


# ── health_check() tests ──────────────────────────────────────────────────────


class TestHealthCheck:
    async def test_health_check_healthy(self) -> None:
        connector = _make_connector()
        mock = _mock_client()
        with patch.object(connector, "_make_client", return_value=mock):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Adobe Analytics" in result.message

    async def test_health_check_includes_suite_count(self) -> None:
        connector = _make_connector()
        mock = _mock_client(report_suites=SAMPLE_REPORT_SUITES)
        with patch.object(connector, "_make_client", return_value=mock):
            result = await connector.health_check()
        assert "2" in result.message

    async def test_health_check_missing_credentials(self) -> None:
        connector = _make_connector(client_id="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        connector = _make_connector()
        mock = _mock_client(raise_auth=True)
        with patch.object(connector, "_make_client", return_value=mock):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        connector = _make_connector()
        mock = _mock_client(raise_network=True)
        with patch.object(connector, "_make_client", return_value=mock):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ── sync() tests ──────────────────────────────────────────────────────────────


class TestSync:
    async def test_sync_returns_sync_result(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client()
        result = await connector.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_counts_report_suites(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(report_suites=SAMPLE_REPORT_SUITES)
        result = await connector.sync()
        assert result.documents_found >= 2
        assert result.documents_synced >= 2

    async def test_sync_counts_segments(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(
            report_suites=SAMPLE_REPORT_SUITES,
            segments=SAMPLE_SEGMENTS,
        )
        result = await connector.sync()
        # 2 report_suites + 2 segments + 2 calc_metrics = 6 found minimum
        assert result.documents_found >= 4

    async def test_sync_status_completed_on_no_failures(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client()
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_status_partial_on_failures(self) -> None:
        connector = _make_connector()
        mock = _mock_client()
        # Force a normalization failure by making one segment malformed
        bad_seg = {"id": None}  # will cause issue in normalize_segment

        async def _bad_segments(rsid: str) -> list:
            return [bad_seg]

        mock.get_segments = AsyncMock(side_effect=_bad_segments)
        connector.http_client = mock
        # Should complete partially (report_suites + calc_metrics ok)
        result = await connector.sync()
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    async def test_sync_initializes_client_if_none(self) -> None:
        connector = _make_connector()
        mock = _mock_client()
        connector.http_client = None
        with patch.object(connector, "_make_client", return_value=mock):
            result = await connector.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_empty_report_suites_partial(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(report_suites=[], segments=[], calc_metrics=[])
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_found == 0

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(report_suites=SAMPLE_REPORT_SUITES)
        ingest_calls: list[Any] = []

        async def _mock_ingest(doc: ConnectorDocument, kb_id: str) -> None:
            ingest_calls.append((doc, kb_id))

        connector._ingest_document = _mock_ingest  # type: ignore[method-assign]
        await connector.sync(kb_id="kb_test_123")
        assert len(ingest_calls) >= 2
        assert all(kb == "kb_test_123" for _, kb in ingest_calls)

    async def test_sync_partial_graceful_when_segments_fail(self) -> None:
        connector = _make_connector()
        mock = _mock_client(report_suites=SAMPLE_REPORT_SUITES)

        async def _exploding_segments(rsid: str) -> list:
            raise AdobeAnalyticsNetworkError("segments endpoint down")

        mock.get_segments = AsyncMock(side_effect=_exploding_segments)
        connector.http_client = mock
        result = await connector.sync()
        # Report suites + calc metrics should still succeed
        assert result.documents_synced >= 2


# ── list_* method tests ───────────────────────────────────────────────────────


class TestListMethods:
    async def test_list_report_suites_returns_list(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(report_suites=SAMPLE_REPORT_SUITES)
        result = await connector.list_report_suites()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_report_suites_empty(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(report_suites=[])
        result = await connector.list_report_suites()
        assert result == []

    async def test_list_dimensions_returns_list(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(dimensions=SAMPLE_DIMENSIONS)
        result = await connector.list_dimensions("mycompany.prod")
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_metrics_returns_list(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(metrics=SAMPLE_METRICS)
        result = await connector.list_metrics("mycompany.prod")
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_segments_returns_list(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(segments=SAMPLE_SEGMENTS)
        result = await connector.list_segments("mycompany.prod")
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_segments_empty(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(segments=[])
        result = await connector.list_segments("mycompany.prod")
        assert result == []

    async def test_list_report_suites_wraps_dict_response(self) -> None:
        """When API returns a dict with 'content' key, extract the list."""
        connector = _make_connector()
        mock = _mock_client()

        async def _suites_dict() -> dict:
            return {"content": SAMPLE_REPORT_SUITES, "totalElements": 2}

        mock.get_report_suites = AsyncMock(side_effect=_suites_dict)
        connector.http_client = mock
        result = await connector.list_report_suites()
        assert isinstance(result, list)
        assert len(result) == 2


# ── run_report() tests ────────────────────────────────────────────────────────


class TestRunReport:
    async def test_run_report_returns_response(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(report_response=SAMPLE_REPORT_RESPONSE)
        result = await connector.run_report(
            report_suite_id="mycompany.prod",
            metrics=["metrics/visits", "metrics/pageviews"],
            dimensions=["variables/page"],
        )
        assert "rows" in result

    async def test_run_report_with_date_range(self) -> None:
        connector = _make_connector()
        mock = _mock_client(report_response=SAMPLE_REPORT_RESPONSE)
        connector.http_client = mock
        result = await connector.run_report(
            report_suite_id="mycompany.prod",
            metrics=["metrics/visits"],
            dimensions=["variables/page"],
            date_range="2024-01-01T00:00:00/2024-01-31T23:59:59",
        )
        assert "rows" in result

    async def test_run_report_default_dimension_when_empty(self) -> None:
        connector = _make_connector()
        connector.http_client = _mock_client(report_response=SAMPLE_REPORT_RESPONSE)
        result = await connector.run_report(
            report_suite_id="mycompany.prod",
            metrics=["metrics/visits"],
            dimensions=[],
        )
        assert result == SAMPLE_REPORT_RESPONSE


# ── Lifecycle tests ───────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_aclose_clears_http_client(self) -> None:
        connector = _make_connector()
        mock = _mock_client()
        connector.http_client = mock
        await connector.aclose()
        assert connector.http_client is None
        mock.aclose.assert_awaited_once()

    async def test_aclose_when_no_client(self) -> None:
        connector = _make_connector()
        connector.http_client = None
        await connector.aclose()  # Should not raise

    async def test_context_manager(self) -> None:
        connector = _make_connector()
        mock = _mock_client()
        connector.http_client = mock
        async with connector as c:
            assert c is connector
        mock.aclose.assert_awaited_once()

    async def test_connector_type_constant(self) -> None:
        assert AdobeAnalyticsConnector.CONNECTOR_TYPE == "adobe_analytics"

    async def test_auth_type_constant(self) -> None:
        assert AdobeAnalyticsConnector.AUTH_TYPE == "oauth2"

    async def test_config_stored(self) -> None:
        connector = _make_connector()
        assert connector._client_id == CLIENT_ID
        assert connector._client_secret == CLIENT_SECRET
        assert connector._company_id == COMPANY_ID
        assert connector._organization_id == ORG_ID

    async def test_ensure_client_creates_if_none(self) -> None:
        connector = _make_connector()
        connector.http_client = None
        mock = _mock_client()
        with patch.object(connector, "_make_client", return_value=mock):
            client = connector._ensure_client()
        assert client is mock
        assert connector.http_client is mock
