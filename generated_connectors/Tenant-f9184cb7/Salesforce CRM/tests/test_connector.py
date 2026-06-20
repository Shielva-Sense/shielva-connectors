"""
Comprehensive unit test suite for the Salesforce CRM connector.
60+ tests — no live network calls.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Make root importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent))

from exceptions import (
    SalesforceAuthError,
    SalesforceError,
    SalesforceNetworkError,
    SalesforceNotFoundError,
    SalesforceRateLimitError,
    SalesforceServerError,
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
from helpers.normalizer import normalize_contact, normalize_lead, normalize_opportunity
from helpers.utils import CircuitBreaker, with_retry
from connector import SalesforceConnector


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_connector(**config_overrides: object) -> SalesforceConnector:
    config = {
        "client_id": "CLIENT_ID",
        "client_secret": "CLIENT_SECRET",
        "instance_url": "https://test.salesforce.com",
        "access_token": "ACCESS_TOKEN",
        "refresh_token": "REFRESH_TOKEN",
    }
    config.update(config_overrides)
    return SalesforceConnector(
        tenant_id="tenant-test",
        connector_id="connector-test",
        config=config,
    )


def _page(records: list, done: bool = True, next_url: str | None = None) -> dict:
    return {
        "records": records,
        "done": done,
        "nextRecordsUrl": next_url,
        "totalSize": len(records),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Class attributes
# ─────────────────────────────────────────────────────────────────────────────

class TestClassAttributes:
    def test_connector_type(self) -> None:
        assert SalesforceConnector.CONNECTOR_TYPE == "salesforce"

    def test_auth_type(self) -> None:
        assert SalesforceConnector.AUTH_TYPE == "oauth2"

    def test_instance_has_connector_type(self) -> None:
        c = _make_connector()
        assert c.CONNECTOR_TYPE == "salesforce"

    def test_instance_has_auth_type(self) -> None:
        c = _make_connector()
        assert c.AUTH_TYPE == "oauth2"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Exception hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptions:
    def test_salesforce_error_attrs(self) -> None:
        exc = SalesforceError("oops", status_code=400, code="BAD")
        assert exc.message == "oops"
        assert exc.status_code == 400
        assert exc.code == "BAD"
        assert str(exc) == "oops"

    def test_salesforce_error_defaults(self) -> None:
        exc = SalesforceError("msg")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_salesforce_auth_error_inherits(self) -> None:
        exc = SalesforceAuthError("auth fail", 401, "INVALID_SESSION_ID")
        assert isinstance(exc, SalesforceError)
        assert exc.status_code == 401

    def test_salesforce_rate_limit_error_attrs(self) -> None:
        exc = SalesforceRateLimitError("too many", retry_after=5.5)
        assert isinstance(exc, SalesforceError)
        assert exc.retry_after == 5.5
        assert exc.status_code == 429
        assert exc.code == "rate_limit"

    def test_salesforce_rate_limit_default_retry_after(self) -> None:
        exc = SalesforceRateLimitError("too many")
        assert exc.retry_after == 0.0

    def test_salesforce_not_found_message(self) -> None:
        exc = SalesforceNotFoundError("Lead", "001abc")
        assert isinstance(exc, SalesforceError)
        assert "Lead" in str(exc)
        assert "001abc" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "NOT_FOUND"

    def test_salesforce_network_error_inherits(self) -> None:
        exc = SalesforceNetworkError("timeout")
        assert isinstance(exc, SalesforceError)

    def test_salesforce_server_error_inherits(self) -> None:
        exc = SalesforceServerError("500 oops", 500)
        assert isinstance(exc, SalesforceError)
        assert exc.status_code == 500


# ─────────────────────────────────────────────────────────────────────────────
# 3. Models
# ─────────────────────────────────────────────────────────────────────────────

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

    def test_install_result_fields(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="cid",
            message="ok",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED
        assert r.connector_id == "cid"
        assert r.message == "ok"

    def test_install_result_defaults(self) -> None:
        r = InstallResult(health=ConnectorHealth.OFFLINE, auth_status=AuthStatus.FAILED)
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result_fields(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.DEGRADED,
            auth_status=AuthStatus.FAILED,
            message="net error",
        )
        assert r.health == ConnectorHealth.DEGRADED
        assert r.message == "net error"

    def test_sync_result_fields(self) -> None:
        r = SyncResult(
            status=SyncStatus.PARTIAL,
            documents_found=10,
            documents_synced=8,
            documents_failed=2,
        )
        assert r.documents_found == 10
        assert r.documents_synced == 8
        assert r.documents_failed == 2

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc",
            title="My Lead",
            content="content here",
            connector_id="conn1",
            tenant_id="tenant1",
            source_url="https://example.com",
            metadata={"object_type": "Lead"},
        )
        assert doc.source_id == "abc"
        assert doc.source_url == "https://example.com"
        assert doc.metadata["object_type"] == "Lead"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="x",
            title="T",
            content="C",
            connector_id="c",
            tenant_id="t",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}


# ─────────────────────────────────────────────────────────────────────────────
# 4. normalize_lead
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeLead:
    def _full_record(self) -> dict:
        return {
            "Id": "00Q1a000001bCDEF",
            "FirstName": "Jane",
            "LastName": "Smith",
            "Company": "Acme Corp",
            "Email": "jane@acme.com",
            "Phone": "+1-555-0100",
            "Status": "Open - Not Contacted",
            "LeadSource": "Web",
            "CreatedDate": "2024-01-15T10:00:00Z",
        }

    def test_source_id(self) -> None:
        doc = normalize_lead(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert doc.source_id == "00Q1a000001bCDEF"

    def test_title_contains_name_and_company(self) -> None:
        doc = normalize_lead(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert "Jane Smith" in doc.title
        assert "Acme Corp" in doc.title

    def test_content_has_all_fields(self) -> None:
        doc = normalize_lead(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert "jane@acme.com" in doc.content
        assert "+1-555-0100" in doc.content
        assert "Open - Not Contacted" in doc.content
        assert "Web" in doc.content
        assert "2024-01-15T10:00:00Z" in doc.content

    def test_metadata_object_type(self) -> None:
        doc = normalize_lead(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert doc.metadata["object_type"] == "Lead"

    def test_source_url_contains_instance_url_and_record_id(self) -> None:
        doc = normalize_lead(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert "https://org.sf.com" in doc.source_url
        assert "00Q1a000001bCDEF" in doc.source_url
        assert "/lightning/r/Lead/" in doc.source_url

    def test_minimal_record_only_id(self) -> None:
        doc = normalize_lead({"Id": "ID001"}, "conn", "ten", "https://org.sf.com")
        assert doc.source_id == "ID001"
        assert "ID001" in doc.source_url

    def test_no_instance_url_fallback(self) -> None:
        doc = normalize_lead(self._full_record(), "conn", "ten")
        # Falls back to SF_INSTANCE_BASE when no instance_url
        assert doc.source_url != ""
        assert "00Q1a000001bCDEF" in doc.source_url

    def test_connector_id_and_tenant_id(self) -> None:
        doc = normalize_lead(self._full_record(), "my-conn", "my-tenant", "https://org.sf.com")
        assert doc.connector_id == "my-conn"
        assert doc.tenant_id == "my-tenant"


# ─────────────────────────────────────────────────────────────────────────────
# 5. normalize_contact
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeContact:
    def _full_record(self) -> dict:
        return {
            "Id": "0031a000001AAAAA",
            "FirstName": "Bob",
            "LastName": "Jones",
            "Account": {"Name": "BigCorp"},
            "Email": "bob@bigcorp.com",
            "Phone": "+1-555-0200",
            "Title": "VP Sales",
            "CreatedDate": "2024-02-01T09:00:00Z",
        }

    def test_source_id(self) -> None:
        doc = normalize_contact(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert doc.source_id == "0031a000001AAAAA"

    def test_title_contains_name(self) -> None:
        doc = normalize_contact(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert "Bob Jones" in doc.title

    def test_nested_account_name_in_content(self) -> None:
        doc = normalize_contact(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert "BigCorp" in doc.content

    def test_metadata_object_type_contact(self) -> None:
        doc = normalize_contact(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert doc.metadata["object_type"] == "Contact"

    def test_source_url(self) -> None:
        doc = normalize_contact(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert "0031a000001AAAAA" in doc.source_url
        assert "/lightning/r/Contact/" in doc.source_url

    def test_minimal_record(self) -> None:
        doc = normalize_contact({"Id": "C001"}, "conn", "ten")
        assert doc.source_id == "C001"

    def test_missing_account_handled(self) -> None:
        record = dict(self._full_record())
        record["Account"] = None
        doc = normalize_contact(record, "conn", "ten")
        assert doc.source_id == "0031a000001AAAAA"


# ─────────────────────────────────────────────────────────────────────────────
# 6. normalize_opportunity
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeOpportunity:
    def _full_record(self) -> dict:
        return {
            "Id": "0061a000001BBBBB",
            "Name": "Big Deal Q1",
            "StageName": "Negotiation",
            "Amount": 75000.0,
            "CloseDate": "2024-03-31",
            "Account": {"Name": "MegaCorp"},
            "Probability": 80,
            "CreatedDate": "2024-01-10T08:00:00Z",
        }

    def test_source_id(self) -> None:
        doc = normalize_opportunity(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert doc.source_id == "0061a000001BBBBB"

    def test_title_contains_name_and_stage(self) -> None:
        doc = normalize_opportunity(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert "Big Deal Q1" in doc.title
        assert "Negotiation" in doc.title

    def test_amount_in_content(self) -> None:
        doc = normalize_opportunity(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert "75000" in doc.content

    def test_probability_in_content(self) -> None:
        doc = normalize_opportunity(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert "80" in doc.content

    def test_metadata_object_type(self) -> None:
        doc = normalize_opportunity(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert doc.metadata["object_type"] == "Opportunity"

    def test_source_url(self) -> None:
        doc = normalize_opportunity(self._full_record(), "conn", "ten", "https://org.sf.com")
        assert "0061a000001BBBBB" in doc.source_url
        assert "/lightning/r/Opportunity/" in doc.source_url

    def test_minimal_record(self) -> None:
        doc = normalize_opportunity({"Id": "O001"}, "conn", "ten")
        assert doc.source_id == "O001"

    def test_zero_amount(self) -> None:
        record = dict(self._full_record())
        record["Amount"] = 0
        doc = normalize_opportunity(record, "conn", "ten")
        assert "Amount" in doc.content


# ─────────────────────────────────────────────────────────────────────────────
# 7. CircuitBreaker
# ─────────────────────────────────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        assert cb.state == "closed"
        assert not cb.is_open

    def test_opens_after_threshold_failures(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.on_failure()
        cb.on_failure()
        assert not cb.is_open
        cb.on_failure()
        assert cb.is_open
        assert cb.state == "open"

    def test_on_success_closes(self) -> None:
        cb = CircuitBreaker(failure_threshold=2)
        cb.on_failure()
        cb.on_failure()
        assert cb.is_open
        cb.on_success()
        assert not cb.is_open
        assert cb.state == "closed"

    def test_is_open_property(self) -> None:
        cb = CircuitBreaker(failure_threshold=1)
        assert cb.is_open is False
        cb.on_failure()
        assert cb.is_open is True

    def test_half_open_after_recovery_timeout(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.0)
        cb.on_failure()
        # recovery_timeout_s=0 means it transitions to half-open immediately
        assert cb.state in ("open", "half-open")

    def test_multiple_successes_stay_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=5)
        cb.on_success()
        cb.on_success()
        assert cb.state == "closed"

    def test_reset_on_success(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.on_failure()
        cb.on_failure()
        cb.on_success()
        # failures reset — need 3 more to open
        cb.on_failure()
        cb.on_failure()
        assert not cb.is_open


# ─────────────────────────────────────────────────────────────────────────────
# 8. with_retry
# ─────────────────────────────────────────────────────────────────────────────

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_try(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_salesforce_error(self) -> None:
        fn = AsyncMock(side_effect=[
            SalesforceError("temp", 503),
            SalesforceError("temp", 503),
            {"ok": True},
        ])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_raises_immediately_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=SalesforceAuthError("invalid token", 401))
        with pytest.raises(SalesforceAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_respects_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=SalesforceError("fail", 503))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(SalesforceError):
                await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(side_effect=[
            SalesforceRateLimitError("rate", retry_after=2.5),
            {"ok": True},
        ])
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        sleep_mock.assert_called_once_with(2.5)

    @pytest.mark.asyncio
    async def test_success_after_one_failure(self) -> None:
        fn = AsyncMock(side_effect=[SalesforceNetworkError("net"), {"data": 1}])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"data": 1}


# ─────────────────────────────────────────────────────────────────────────────
# 9. install()
# ─────────────────────────────────────────────────────────────────────────────

class TestInstall:
    @pytest.mark.asyncio
    async def test_missing_access_token(self) -> None:
        c = _make_connector(access_token="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_missing_instance_url(self) -> None:
        c = _make_connector(instance_url="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        c = _make_connector()
        with patch("connector.SalesforceHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.ping = AsyncMock(return_value={})
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_auth_error_returns_invalid_credentials(self) -> None:
        c = _make_connector()
        with patch("connector.SalesforceHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.ping = AsyncMock(side_effect=SalesforceAuthError("bad token", 401))
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_generic_exception_returns_failed(self) -> None:
        c = _make_connector()
        with patch("connector.SalesforceHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.ping = AsyncMock(side_effect=Exception("unexpected"))
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 10. health_check()
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_missing_credentials(self) -> None:
        c = _make_connector(access_token="")
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_success_healthy(self) -> None:
        c = _make_connector()
        with patch("connector.SalesforceHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.ping = AsyncMock(return_value={})
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "reachable" in result.message

    @pytest.mark.asyncio
    async def test_auth_error_returns_offline_invalid(self) -> None:
        c = _make_connector()
        with patch("connector.SalesforceHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.ping = AsyncMock(side_effect=SalesforceAuthError("bad", 401))
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_network_error_circuit_not_open_degraded(self) -> None:
        c = _make_connector()
        with patch("connector.SalesforceHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.ping = AsyncMock(side_effect=SalesforceNetworkError("timeout"))
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.health_check()
        # circuit not yet open (1 failure < 5 threshold) → DEGRADED
        assert result.health == ConnectorHealth.DEGRADED

    @pytest.mark.asyncio
    async def test_network_error_circuit_open_offline(self) -> None:
        c = _make_connector()
        # Force circuit open
        for _ in range(5):
            c._circuit_breaker.on_failure()
        assert c._circuit_breaker.is_open

        with patch("connector.SalesforceHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.ping = AsyncMock(side_effect=SalesforceNetworkError("timeout"))
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE


# ─────────────────────────────────────────────────────────────────────────────
# 11. sync()
# ─────────────────────────────────────────────────────────────────────────────

class TestSync:
    def _mock_http(self, empty: bool = False) -> AsyncMock:
        mock = AsyncMock()
        if empty:
            mock.query = AsyncMock(return_value=_page([]))
        return mock

    @pytest.mark.asyncio
    async def test_empty_all_objects(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value=_page([]))
        c.http_client = mock_http
        result = await c.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.documents_failed == 0

    @pytest.mark.asyncio
    async def test_leads_only(self) -> None:
        c = _make_connector()
        lead_record = {"Id": "L001", "FirstName": "A", "LastName": "B", "Company": "X"}
        # First call: leads (1 record), second: contacts (0), third: opportunities (0)
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(side_effect=[
            _page([lead_record]),
            _page([]),
            _page([]),
        ])
        c.http_client = mock_http
        result = await c.sync()
        assert result.documents_found == 1
        assert result.documents_synced == 1
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_all_three_objects(self) -> None:
        c = _make_connector()
        lead = {"Id": "L001", "LastName": "Smith"}
        contact = {"Id": "C001", "LastName": "Jones"}
        opp = {"Id": "O001", "Name": "Deal"}
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(side_effect=[
            _page([lead]),
            _page([contact]),
            _page([opp]),
        ])
        c.http_client = mock_http
        result = await c.sync()
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_full_true_uses_non_since_soql(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value=_page([]))
        c.http_client = mock_http
        await c.sync(full=True)
        # Should have been called 3 times (leads, contacts, opps), no since clause
        assert mock_http.query.call_count == 3
        for args, _ in mock_http.query.call_args_list:
            soql = args[0]
            assert "WHERE CreatedDate" not in soql

    @pytest.mark.asyncio
    async def test_since_datetime_uses_since_soql(self) -> None:
        c = _make_connector()
        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value=_page([]))
        c.http_client = mock_http
        await c.sync(full=False, since=since)
        for args, _ in mock_http.query.call_args_list:
            soql = args[0]
            assert "CreatedDate" in soql

    @pytest.mark.asyncio
    async def test_kb_id_calls_ingest_document(self) -> None:
        c = _make_connector()
        lead = {"Id": "L001", "LastName": "Smith"}
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(side_effect=[
            _page([lead]),
            _page([]),
            _page([]),
        ])
        c.http_client = mock_http
        ingest_mock = AsyncMock()
        c._ingest_document = ingest_mock  # type: ignore[method-assign]
        await c.sync(kb_id="kb-123")
        ingest_mock.assert_called_once()
        args = ingest_mock.call_args[0]
        assert args[1] == "kb-123"

    @pytest.mark.asyncio
    async def test_error_on_one_object_partial_status(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        # with_retry retries up to 3 times on SalesforceError, so supply 3 failures
        # then one empty page for contacts and one for opportunities
        mock_http.query = AsyncMock(side_effect=[
            SalesforceError("server error", 503),
            SalesforceError("server error", 503),
            SalesforceError("server error", 503),
            _page([]),  # contacts
            _page([]),  # opportunities
        ])
        c.http_client = mock_http
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await c.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed >= 1

    @pytest.mark.asyncio
    async def test_normalize_error_increments_failed(self) -> None:
        c = _make_connector()
        # A record with no Id will still normalize but let's patch the normalizer
        lead = {"Id": "L001", "LastName": "Smith"}
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(side_effect=[
            _page([lead]),
            _page([]),
            _page([]),
        ])
        c.http_client = mock_http
        with patch("connector.normalize_lead", side_effect=Exception("normalize failed")):
            result = await c.sync()
        assert result.documents_failed >= 1

    @pytest.mark.asyncio
    async def test_pagination_follows_next_url(self) -> None:
        c = _make_connector()
        page1 = {
            "records": [{"Id": "L001", "LastName": "A"}],
            "done": False,
            "nextRecordsUrl": "/services/data/v57.0/query/01g000000001-2000",
            "totalSize": 2,
        }
        page2 = _page([{"Id": "L002", "LastName": "B"}])
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(side_effect=[page1, _page([]), _page([])])
        mock_http.query_more = AsyncMock(return_value=page2)
        c.http_client = mock_http
        result = await c.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2


# ─────────────────────────────────────────────────────────────────────────────
# 12. list_leads / list_contacts / list_opportunities / list_accounts
# ─────────────────────────────────────────────────────────────────────────────

class TestListMethods:
    @pytest.mark.asyncio
    async def test_list_leads_correct_soql(self) -> None:
        c = _make_connector()
        expected = {"records": [], "totalSize": 0, "done": True}
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.list_leads(limit=50)
        assert result == expected
        soql = mock_http.query.call_args[0][0]
        assert "FROM Lead" in soql
        assert "LIMIT 50" in soql

    @pytest.mark.asyncio
    async def test_list_contacts_correct_soql(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value={"records": []})
        c.http_client = mock_http
        await c.list_contacts(limit=25)
        soql = mock_http.query.call_args[0][0]
        assert "FROM Contact" in soql
        assert "LIMIT 25" in soql

    @pytest.mark.asyncio
    async def test_list_opportunities_correct_soql(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value={"records": []})
        c.http_client = mock_http
        await c.list_opportunities(limit=10)
        soql = mock_http.query.call_args[0][0]
        assert "FROM Opportunity" in soql
        assert "LIMIT 10" in soql

    @pytest.mark.asyncio
    async def test_list_accounts_correct_soql(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value={"records": []})
        c.http_client = mock_http
        await c.list_accounts(limit=200)
        soql = mock_http.query.call_args[0][0]
        assert "FROM Account" in soql
        assert "LIMIT 200" in soql

    @pytest.mark.asyncio
    async def test_list_leads_returns_result(self) -> None:
        c = _make_connector()
        expected = {"records": [{"Id": "L1"}], "totalSize": 1, "done": True}
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.list_leads()
        assert result == expected


# ─────────────────────────────────────────────────────────────────────────────
# 13. query()
# ─────────────────────────────────────────────────────────────────────────────

class TestQuery:
    @pytest.mark.asyncio
    async def test_delegates_to_http_client_query(self) -> None:
        c = _make_connector()
        expected = {"records": [{"Id": "X"}], "done": True}
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.query("SELECT Id FROM Lead LIMIT 1")
        assert result == expected
        mock_http.query.assert_called_once_with("SELECT Id FROM Lead LIMIT 1")

    @pytest.mark.asyncio
    async def test_creates_client_if_none(self) -> None:
        c = _make_connector()
        assert c.http_client is None
        with patch("connector.SalesforceHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.query = AsyncMock(return_value={"records": []})
            MockClient.return_value = mock_instance
            await c.query("SELECT Id FROM Lead")
        assert c.http_client is not None


# ─────────────────────────────────────────────────────────────────────────────
# 14. list_objects() / get_object()
# ─────────────────────────────────────────────────────────────────────────────

class TestObjectMethods:
    @pytest.mark.asyncio
    async def test_list_objects_calls_list_sobjects(self) -> None:
        c = _make_connector()
        expected = {"sobjects": [{"name": "Lead"}]}
        mock_http = AsyncMock()
        mock_http.list_sobjects = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.list_objects()
        assert result == expected
        mock_http.list_sobjects.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_object_calls_get_sobject(self) -> None:
        c = _make_connector()
        expected = {"Id": "L001", "FirstName": "Jane"}
        mock_http = AsyncMock()
        mock_http.get_sobject = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.get_object("Lead", "L001")
        assert result == expected
        mock_http.get_sobject.assert_called_once_with("Lead", "L001")

    @pytest.mark.asyncio
    async def test_get_object_returns_result(self) -> None:
        c = _make_connector()
        expected = {"Id": "C999"}
        mock_http = AsyncMock()
        mock_http.get_sobject = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.get_object("Contact", "C999")
        assert result["Id"] == "C999"


# ─────────────────────────────────────────────────────────────────────────────
# 15. aclose()
# ─────────────────────────────────────────────────────────────────────────────

class TestAclose:
    @pytest.mark.asyncio
    async def test_aclose_calls_http_client_aclose(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.aclose = AsyncMock()
        c.http_client = mock_http
        await c.aclose()
        mock_http.aclose.assert_called_once()
        assert c.http_client is None

    @pytest.mark.asyncio
    async def test_aclose_safe_when_no_client(self) -> None:
        c = _make_connector()
        assert c.http_client is None
        await c.aclose()  # must not raise

    @pytest.mark.asyncio
    async def test_aclose_sets_http_client_none(self) -> None:
        c = _make_connector()
        c.http_client = AsyncMock()
        await c.aclose()
        assert c.http_client is None

    @pytest.mark.asyncio
    async def test_double_aclose_safe(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        c.http_client = mock_http
        await c.aclose()
        await c.aclose()  # second call must not raise


# ─────────────────────────────────────────────────────────────────────────────
# 16. Context manager
# ─────────────────────────────────────────────────────────────────────────────

class TestContextManager:
    @pytest.mark.asyncio
    async def test_async_context_manager(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        c.http_client = mock_http
        async with c as ctx:
            assert ctx is c
        mock_http.aclose.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 17. _auth_base_url()
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthBaseUrl:
    def test_production_base_url(self) -> None:
        c = _make_connector(sandbox=False)
        assert c._auth_base_url() == "https://login.salesforce.com"

    def test_sandbox_base_url(self) -> None:
        c = _make_connector(sandbox=True)
        assert c._auth_base_url() == "https://test.salesforce.com"

    def test_no_sandbox_key_defaults_to_production(self) -> None:
        c = SalesforceConnector(tenant_id="t", connector_id="c", config={})
        assert c._auth_base_url() == "https://login.salesforce.com"


# ─────────────────────────────────────────────────────────────────────────────
# 18. authorize()
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthorize:
    @pytest.mark.asyncio
    async def test_production_auth_url(self) -> None:
        c = _make_connector(sandbox=False)
        result = await c.authorize()
        assert "auth_url" in result
        assert "login.salesforce.com" in result["auth_url"]
        assert "services/oauth2/authorize" in result["auth_url"]

    @pytest.mark.asyncio
    async def test_sandbox_auth_url(self) -> None:
        c = _make_connector(sandbox=True)
        result = await c.authorize()
        assert "auth_url" in result
        assert "test.salesforce.com" in result["auth_url"]

    @pytest.mark.asyncio
    async def test_auth_url_contains_client_id(self) -> None:
        c = _make_connector(sandbox=False)
        result = await c.authorize()
        assert "CLIENT_ID" in result["auth_url"]

    @pytest.mark.asyncio
    async def test_auth_url_contains_scope(self) -> None:
        c = _make_connector(sandbox=False)
        result = await c.authorize()
        assert "scope" in result["auth_url"]

    @pytest.mark.asyncio
    async def test_custom_redirect_uri_used(self) -> None:
        c = _make_connector(redirect_uri="https://myapp.example.com/callback")
        result = await c.authorize()
        assert "myapp.example.com" in result["auth_url"]
        assert result["redirect_uri"] == "https://myapp.example.com/callback"


# ─────────────────────────────────────────────────────────────────────────────
# 19. install() — client_id/client_secret validation
# ─────────────────────────────────────────────────────────────────────────────

class TestInstallValidation:
    @pytest.mark.asyncio
    async def test_missing_client_id_returns_offline(self) -> None:
        c = SalesforceConnector(
            tenant_id="t", connector_id="c",
            config={"client_secret": "SECRET"},
        )
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in result.message

    @pytest.mark.asyncio
    async def test_missing_client_secret_returns_offline(self) -> None:
        c = SalesforceConnector(
            tenant_id="t", connector_id="c",
            config={"client_id": "ID"},
        )
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_secret" in result.message

    @pytest.mark.asyncio
    async def test_client_id_and_secret_without_token_offline(self) -> None:
        # Has credentials but no access_token/instance_url — offline (awaiting OAuth)
        c = SalesforceConnector(
            tenant_id="t", connector_id="c",
            config={"client_id": "ID", "client_secret": "SECRET"},
        )
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ─────────────────────────────────────────────────────────────────────────────
# 20. list_cases()
# ─────────────────────────────────────────────────────────────────────────────

class TestListCases:
    @pytest.mark.asyncio
    async def test_list_cases_correct_soql(self) -> None:
        c = _make_connector()
        expected = {"records": [], "totalSize": 0, "done": True}
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.list_cases(limit=50)
        assert result == expected
        soql = mock_http.query.call_args[0][0]
        assert "FROM Case" in soql
        assert "LIMIT 50" in soql

    @pytest.mark.asyncio
    async def test_list_cases_returns_result(self) -> None:
        c = _make_connector()
        expected = {"records": [{"Id": "CS001", "Subject": "Login issue"}], "totalSize": 1, "done": True}
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.list_cases()
        assert result == expected

    @pytest.mark.asyncio
    async def test_list_cases_default_limit(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.query = AsyncMock(return_value={"records": []})
        c.http_client = mock_http
        await c.list_cases()
        soql = mock_http.query.call_args[0][0]
        assert "LIMIT 100" in soql


# ─────────────────────────────────────────────────────────────────────────────
# 21. Module-level constants
# ─────────────────────────────────────────────────────────────────────────────

class TestModuleConstants:
    def test_connector_type_constant(self) -> None:
        from connector import CONNECTOR_TYPE
        assert CONNECTOR_TYPE == "salesforce"

    def test_auth_type_constant(self) -> None:
        from connector import AUTH_TYPE
        assert AUTH_TYPE == "oauth2"

    def test_api_version_constant(self) -> None:
        from connector import SALESFORCE_API_VERSION
        assert SALESFORCE_API_VERSION == "v58.0"
