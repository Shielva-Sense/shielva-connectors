"""
Comprehensive unit test suite for the Zoho CRM connector.
60+ tests — no live network calls.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

# Make root importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent))

from exceptions import (
    ZohoCRMAuthError,
    ZohoCRMError,
    ZohoCRMNetworkError,
    ZohoCRMNotFoundError,
    ZohoCRMRateLimitError,
    ZohoCRMServerError,
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
from helpers.utils import CircuitBreaker, normalize_record, with_retry, _stable_id
from connector import ZohoCRMConnector
from client.http_client import _build_base_url, _build_auth_url


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_connector(**config_overrides: object) -> ZohoCRMConnector:
    config: dict = {
        "client_id": "1000.TESTCLIENTID",
        "client_secret": "test_secret",
        "access_token": "1000.token_abc123",
        "refresh_token": "1000.refresh_xyz789",
        "dc": "com",
        "redirect_uri": "https://app.shielva.ai/oauth/callback/zoho_crm",
    }
    config.update(config_overrides)
    return ZohoCRMConnector(
        tenant_id="tenant-test",
        connector_id="connector-test",
        config=config,
    )


def _zoho_page(records: list, more_records: bool = False, page: int = 1) -> dict:
    return {
        "data": records,
        "info": {
            "page": page,
            "count": len(records),
            "more_records": more_records,
            "per_page": 200,
        },
    }


def _lead_record(record_id: str = "3692340000001234567") -> dict:
    return {
        "id": record_id,
        "First_Name": "Jane",
        "Last_Name": "Smith",
        "Company": "Acme Corp",
        "Email": "jane@acme.com",
        "Phone": "+1-555-0100",
        "Lead_Status": "Not Contacted",
        "Lead_Source": "Web",
        "Created_Time": "2024-01-15T10:00:00+00:00",
    }


def _contact_record(record_id: str = "3692340000002345678") -> dict:
    return {
        "id": record_id,
        "First_Name": "Bob",
        "Last_Name": "Jones",
        "Account_Name": {"id": "99999", "name": "BigCorp"},
        "Email": "bob@bigcorp.com",
        "Phone": "+1-555-0200",
        "Title": "VP Sales",
        "Created_Time": "2024-02-01T09:00:00+00:00",
    }


def _deal_record(record_id: str = "3692340000003456789") -> dict:
    return {
        "id": record_id,
        "Deal_Name": "Big Deal Q1",
        "Stage": "Negotiation/Review",
        "Amount": 75000.0,
        "Closing_Date": "2024-03-31",
        "Account_Name": {"id": "99999", "name": "MegaCorp"},
        "Probability": 80,
        "Created_Time": "2024-01-10T08:00:00+00:00",
    }


def _account_record(record_id: str = "3692340000004567890") -> dict:
    return {
        "id": record_id,
        "Account_Name": "TechCorp Inc",
        "Phone": "+1-555-0300",
        "Website": "https://techcorp.example.com",
        "Industry": "Technology",
        "Created_Time": "2024-01-01T00:00:00+00:00",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Class attributes
# ─────────────────────────────────────────────────────────────────────────────

class TestClassAttributes:
    def test_connector_type(self) -> None:
        assert ZohoCRMConnector.CONNECTOR_TYPE == "zoho_crm"

    def test_auth_type(self) -> None:
        assert ZohoCRMConnector.AUTH_TYPE == "oauth2"

    def test_instance_has_connector_type(self) -> None:
        c = _make_connector()
        assert c.CONNECTOR_TYPE == "zoho_crm"

    def test_instance_has_auth_type(self) -> None:
        c = _make_connector()
        assert c.AUTH_TYPE == "oauth2"

    def test_data_center_default_com(self) -> None:
        c = ZohoCRMConnector(config={
            "client_id": "cid", "client_secret": "cs", "access_token": "tok"
        })
        assert c._data_center == "com"

    def test_data_center_eu_via_dc_key(self) -> None:
        c = _make_connector(dc="eu")
        assert c._data_center == "eu"

    def test_data_center_in_via_dc_key(self) -> None:
        c = _make_connector(dc="in")
        assert c._data_center == "in"

    def test_data_center_legacy_key(self) -> None:
        # legacy "data_center" key still accepted for backwards compat
        c = ZohoCRMConnector(config={
            "client_id": "cid", "client_secret": "cs",
            "access_token": "tok", "data_center": "eu",
        })
        assert c._data_center == "eu"

    def test_tenant_and_connector_id_stored(self) -> None:
        c = ZohoCRMConnector(
            tenant_id="t1", connector_id="c1",
            config={"client_id": "cid", "client_secret": "cs", "access_token": "tok"},
        )
        assert c.tenant_id == "t1"
        assert c.connector_id == "c1"


# ─────────────────────────────────────────────────────────────────────────────
# 2. DC helper methods
# ─────────────────────────────────────────────────────────────────────────────

class TestDCHelpers:
    def test_get_dc_com(self) -> None:
        c = _make_connector(dc="com")
        assert c._get_dc() == "com"

    def test_get_dc_eu(self) -> None:
        c = _make_connector(dc="eu")
        assert c._get_dc() == "eu"

    def test_accounts_url_com(self) -> None:
        c = _make_connector(dc="com")
        assert c._accounts_url() == "https://accounts.zoho.com"

    def test_accounts_url_eu(self) -> None:
        c = _make_connector(dc="eu")
        assert c._accounts_url() == "https://accounts.zoho.eu"

    def test_accounts_url_in(self) -> None:
        c = _make_connector(dc="in")
        assert c._accounts_url() == "https://accounts.zoho.in"

    def test_api_url_com(self) -> None:
        c = _make_connector(dc="com")
        assert c._api_url() == "https://www.zohoapis.com/crm/v2"

    def test_api_url_eu(self) -> None:
        c = _make_connector(dc="eu")
        assert c._api_url() == "https://www.zohoapis.eu/crm/v2"

    def test_api_url_in(self) -> None:
        c = _make_connector(dc="in")
        assert c._api_url() == "https://www.zohoapis.in/crm/v2"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Exception hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptions:
    def test_zoho_error_attrs(self) -> None:
        exc = ZohoCRMError("oops", status_code=400, code="BAD")
        assert exc.message == "oops"
        assert exc.status_code == 400
        assert exc.code == "BAD"
        assert str(exc) == "oops"

    def test_zoho_error_defaults(self) -> None:
        exc = ZohoCRMError("msg")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_auth_error_inherits(self) -> None:
        exc = ZohoCRMAuthError("auth fail", 401, "INVALID_TOKEN")
        assert isinstance(exc, ZohoCRMError)
        assert exc.status_code == 401

    def test_rate_limit_error_attrs(self) -> None:
        exc = ZohoCRMRateLimitError("too many", retry_after=5.5)
        assert isinstance(exc, ZohoCRMError)
        assert exc.retry_after == 5.5
        assert exc.status_code == 429
        assert exc.code == "rate_limit"

    def test_rate_limit_default_retry_after(self) -> None:
        exc = ZohoCRMRateLimitError("too many")
        assert exc.retry_after == 0.0

    def test_not_found_message(self) -> None:
        exc = ZohoCRMNotFoundError("Lead", "123")
        assert isinstance(exc, ZohoCRMError)
        assert "Lead" in str(exc)
        assert "123" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "NOT_FOUND"

    def test_network_error_inherits(self) -> None:
        exc = ZohoCRMNetworkError("timeout")
        assert isinstance(exc, ZohoCRMError)

    def test_server_error_inherits(self) -> None:
        exc = ZohoCRMServerError("500 oops", 500)
        assert isinstance(exc, ZohoCRMError)
        assert exc.status_code == 500


# ─────────────────────────────────────────────────────────────────────────────
# 4. Models
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
            source_id="abc123",
            title="Zoho Lead: Jane Smith",
            content="content here",
            connector_id="conn1",
            tenant_id="tenant1",
            source_url="https://crm.zoho.com/crm/org/tab/Leads/123",
            metadata={"module": "Leads"},
        )
        assert doc.source_id == "abc123"
        assert doc.source_url == "https://crm.zoho.com/crm/org/tab/Leads/123"
        assert doc.metadata["module"] == "Leads"

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
# 5. URL builders
# ─────────────────────────────────────────────────────────────────────────────

class TestURLBuilders:
    def test_base_url_com(self) -> None:
        url = _build_base_url("com")
        assert url == "https://www.zohoapis.com/crm/v2"

    def test_base_url_eu(self) -> None:
        url = _build_base_url("eu")
        assert url == "https://www.zohoapis.eu/crm/v2"

    def test_base_url_in(self) -> None:
        url = _build_base_url("in")
        assert url == "https://www.zohoapis.in/crm/v2"

    def test_base_url_com_au(self) -> None:
        url = _build_base_url("com.au")
        assert url == "https://www.zohoapis.com.au/crm/v2"

    def test_base_url_empty_defaults_com(self) -> None:
        url = _build_base_url("")
        assert "zohoapis.com" in url

    def test_auth_url_com(self) -> None:
        url = _build_auth_url("com")
        assert url == "https://accounts.zoho.com/oauth/v2/"

    def test_auth_url_eu(self) -> None:
        url = _build_auth_url("eu")
        assert url == "https://accounts.zoho.eu/oauth/v2/"

    def test_auth_url_in(self) -> None:
        url = _build_auth_url("in")
        assert url == "https://accounts.zoho.in/oauth/v2/"


# ─────────────────────────────────────────────────────────────────────────────
# 6. normalize_record
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeRecord:
    def test_stable_id_deterministic(self) -> None:
        doc1 = normalize_record("Leads", _lead_record(), "c", "t")
        doc2 = normalize_record("Leads", _lead_record(), "c", "t")
        assert doc1.source_id == doc2.source_id

    def test_stable_id_length_16(self) -> None:
        doc = normalize_record("Leads", _lead_record(), "c", "t")
        assert len(doc.source_id) == 16

    def test_stable_id_helper(self) -> None:
        result = _stable_id("Leads", "123")
        assert len(result) == 16
        assert result == _stable_id("Leads", "123")

    def test_stable_id_differs_by_module(self) -> None:
        id1 = _stable_id("Leads", "123")
        id2 = _stable_id("Contacts", "123")
        assert id1 != id2

    def test_lead_title(self) -> None:
        doc = normalize_record("Leads", _lead_record(), "c", "t")
        assert "Jane Smith" in doc.title
        assert "Acme Corp" in doc.title

    def test_contact_title(self) -> None:
        doc = normalize_record("Contacts", _contact_record(), "c", "t")
        assert "Bob Jones" in doc.title
        assert "BigCorp" in doc.title

    def test_deal_title(self) -> None:
        doc = normalize_record("Deals", _deal_record(), "c", "t")
        assert "Big Deal Q1" in doc.title
        assert "Negotiation" in doc.title

    def test_account_title(self) -> None:
        record = {"id": "A001", "Account_Name": "TechCorp"}
        doc = normalize_record("Accounts", record, "c", "t")
        assert "TechCorp" in doc.title

    def test_generic_module_title(self) -> None:
        record = {"id": "X001", "Name": "Test Record"}
        doc = normalize_record("Tasks", record, "c", "t")
        assert "Test Record" in doc.title
        assert "Tasks" in doc.title

    def test_content_contains_record_id(self) -> None:
        doc = normalize_record("Leads", _lead_record("LEAD001"), "c", "t")
        assert "LEAD001" in doc.content

    def test_content_contains_module(self) -> None:
        doc = normalize_record("Leads", _lead_record(), "c", "t")
        assert "Leads" in doc.content

    def test_content_includes_scalar_fields(self) -> None:
        doc = normalize_record("Leads", _lead_record(), "c", "t")
        assert "jane@acme.com" in doc.content

    def test_content_includes_dict_name_fields(self) -> None:
        doc = normalize_record("Contacts", _contact_record(), "c", "t")
        assert "BigCorp" in doc.content

    def test_metadata_module(self) -> None:
        doc = normalize_record("Leads", _lead_record(), "c", "t")
        assert doc.metadata["module"] == "Leads"

    def test_metadata_zoho_record_id(self) -> None:
        doc = normalize_record("Leads", _lead_record("ZCRM001"), "c", "t")
        assert doc.metadata["zoho_record_id"] == "ZCRM001"

    def test_metadata_data_center(self) -> None:
        doc = normalize_record("Leads", _lead_record(), "c", "t", data_center="eu")
        assert doc.metadata["data_center"] == "eu"

    def test_source_url_contains_module(self) -> None:
        doc = normalize_record("Leads", _lead_record("REC123"), "c", "t")
        assert "Leads" in doc.source_url
        assert "REC123" in doc.source_url

    def test_source_url_uses_data_center(self) -> None:
        doc = normalize_record("Leads", _lead_record(), "c", "t", data_center="eu")
        assert "zoho.eu" in doc.source_url

    def test_connector_and_tenant_ids(self) -> None:
        doc = normalize_record("Leads", _lead_record(), "my-conn", "my-tenant")
        assert doc.connector_id == "my-conn"
        assert doc.tenant_id == "my-tenant"

    def test_empty_record_id_gives_empty_source_id(self) -> None:
        doc = normalize_record("Leads", {}, "c", "t")
        assert doc.source_id == ""

    def test_empty_record_id_gives_empty_source_url(self) -> None:
        doc = normalize_record("Leads", {}, "c", "t")
        assert doc.source_url == ""


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
        cb.on_failure()
        cb.on_failure()
        assert not cb.is_open


# ─────────────────────────────────────────────────────────────────────────────
# 8. with_retry
# ─────────────────────────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_success_on_first_try(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_zoho_error(self) -> None:
        fn = AsyncMock(side_effect=[
            ZohoCRMError("temp", 503),
            ZohoCRMError("temp", 503),
            {"ok": True},
        ])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 3

    async def test_raises_immediately_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=ZohoCRMAuthError("invalid token", 401))
        with pytest.raises(ZohoCRMAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 1

    async def test_respects_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=ZohoCRMError("fail", 503))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ZohoCRMError):
                await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    async def test_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(side_effect=[
            ZohoCRMRateLimitError("rate", retry_after=2.5),
            {"ok": True},
        ])
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        sleep_mock.assert_called_once_with(2.5)

    async def test_success_after_one_failure(self) -> None:
        fn = AsyncMock(side_effect=[ZohoCRMNetworkError("net"), {"data": 1}])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"data": 1}


# ─────────────────────────────────────────────────────────────────────────────
# 9. authorize()
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthorize:
    async def test_returns_string(self) -> None:
        c = _make_connector()
        url = await c.authorize()
        assert isinstance(url, str)
        assert url.startswith("https://")

    async def test_contains_client_id(self) -> None:
        c = _make_connector()
        url = await c.authorize()
        assert "1000.TESTCLIENTID" in url

    async def test_contains_scope(self) -> None:
        c = _make_connector()
        url = await c.authorize()
        # scope contains contacts.READ
        assert "ZohoCRM.modules.contacts.READ" in url

    async def test_uses_correct_data_center_eu(self) -> None:
        c = _make_connector(dc="eu")
        url = await c.authorize()
        assert "accounts.zoho.eu" in url

    async def test_uses_correct_data_center_com(self) -> None:
        c = _make_connector(dc="com")
        url = await c.authorize()
        assert "accounts.zoho.com" in url

    async def test_contains_redirect_uri_when_set(self) -> None:
        c = _make_connector(redirect_uri="https://app.shielva.ai/oauth/callback/zoho_crm")
        url = await c.authorize()
        assert "redirect_uri" in url

    async def test_contains_response_type_code(self) -> None:
        c = _make_connector()
        url = await c.authorize()
        assert "response_type=code" in url

    async def test_no_redirect_uri_when_empty(self) -> None:
        c = _make_connector(redirect_uri="")
        url = await c.authorize()
        assert "redirect_uri" not in url

    async def test_uses_accounts_url_path(self) -> None:
        c = _make_connector(dc="in")
        url = await c.authorize()
        assert "accounts.zoho.in/oauth/v2/auth" in url


# ─────────────────────────────────────────────────────────────────────────────
# 10. install()
# ─────────────────────────────────────────────────────────────────────────────

class TestInstall:
    async def test_missing_client_id(self) -> None:
        c = _make_connector(client_id="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_missing_client_secret(self) -> None:
        c = _make_connector(client_secret="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_missing_access_token(self) -> None:
        c = _make_connector(access_token="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_success(self) -> None:
        c = _make_connector()
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get_org = AsyncMock(
                return_value={"data": [{"id": "org1", "company_name": "Test Org"}]}
            )
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_auth_error_returns_invalid_credentials(self) -> None:
        c = _make_connector()
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get_org = AsyncMock(
                side_effect=ZohoCRMAuthError("bad token", 401)
            )
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_generic_exception_returns_failed(self) -> None:
        c = _make_connector()
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get_org = AsyncMock(side_effect=Exception("unexpected"))
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 11. health_check()
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthCheck:
    async def test_missing_access_token(self) -> None:
        c = _make_connector(access_token="")
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_success_healthy(self) -> None:
        c = _make_connector()
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get_org = AsyncMock(return_value={"data": [{}]})
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "reachable" in result.message

    async def test_auth_error_returns_offline_invalid(self) -> None:
        c = _make_connector()
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get_org = AsyncMock(
                side_effect=ZohoCRMAuthError("bad", 401)
            )
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_network_error_circuit_not_open_degraded(self) -> None:
        c = _make_connector()
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get_org = AsyncMock(
                side_effect=ZohoCRMNetworkError("timeout")
            )
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.health_check()
        # 1 failure < 5 threshold → DEGRADED
        assert result.health == ConnectorHealth.DEGRADED

    async def test_network_error_circuit_open_offline(self) -> None:
        c = _make_connector()
        for _ in range(5):
            c._circuit_breaker.on_failure()
        assert c._circuit_breaker.is_open

        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get_org = AsyncMock(
                side_effect=ZohoCRMNetworkError("timeout")
            )
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE


# ─────────────────────────────────────────────────────────────────────────────
# 12. sync()
# ─────────────────────────────────────────────────────────────────────────────

class TestSync:
    async def test_empty_all_modules(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(return_value=_zoho_page([]))
        c.http_client = mock_http
        result = await c.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.documents_failed == 0

    async def test_leads_only(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(side_effect=[
            _zoho_page([_lead_record()]),   # Leads
            _zoho_page([]),                 # Contacts
            _zoho_page([]),                 # Accounts
            _zoho_page([]),                 # Deals
        ])
        c.http_client = mock_http
        result = await c.sync()
        assert result.documents_found == 1
        assert result.documents_synced == 1
        assert result.status == SyncStatus.COMPLETED

    async def test_all_four_modules(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(side_effect=[
            _zoho_page([_lead_record()]),
            _zoho_page([_contact_record()]),
            _zoho_page([_account_record()]),
            _zoho_page([_deal_record()]),
        ])
        c.http_client = mock_http
        result = await c.sync()
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert result.status == SyncStatus.COMPLETED

    async def test_pagination_follows_more_records(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(side_effect=[
            _zoho_page([_lead_record("L001")], more_records=True, page=1),
            _zoho_page([_lead_record("L002")], more_records=False, page=2),
            _zoho_page([]),  # Contacts
            _zoho_page([]),  # Accounts
            _zoho_page([]),  # Deals
        ])
        c.http_client = mock_http
        result = await c.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_kb_id_calls_ingest_document(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(side_effect=[
            _zoho_page([_lead_record()]),
            _zoho_page([]),
            _zoho_page([]),
            _zoho_page([]),
        ])
        c.http_client = mock_http
        ingest_mock = AsyncMock()
        c._ingest_document = ingest_mock  # type: ignore[method-assign]
        await c.sync(kb_id="kb-123")
        ingest_mock.assert_called_once()
        assert ingest_mock.call_args[0][1] == "kb-123"

    async def test_error_on_one_module_partial_status(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(side_effect=[
            ZohoCRMError("server error", 503),
            ZohoCRMError("server error", 503),
            ZohoCRMError("server error", 503),
            _zoho_page([]),   # Contacts
            _zoho_page([]),   # Accounts
            _zoho_page([]),   # Deals
        ])
        c.http_client = mock_http
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await c.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed >= 1

    async def test_normalize_error_increments_failed(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(side_effect=[
            _zoho_page([_lead_record()]),
            _zoho_page([]),
            _zoho_page([]),
            _zoho_page([]),
        ])
        c.http_client = mock_http
        with patch("connector.normalize_record", side_effect=Exception("normalize failed")):
            result = await c.sync()
        assert result.documents_failed >= 1

    async def test_sync_calls_list_records_for_each_module(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(return_value=_zoho_page([]))
        c.http_client = mock_http
        await c.sync()
        # 4 modules × 1 page each = 4 calls
        assert mock_http.list_records.call_count == 4

    async def test_sync_creates_client_if_none(self) -> None:
        c = _make_connector()
        assert c.http_client is None
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.list_records = AsyncMock(return_value=_zoho_page([]))
            MockClient.return_value = mock_instance
            await c.sync()
        assert c.http_client is not None


# ─────────────────────────────────────────────────────────────────────────────
# 13. list_contacts / list_leads / list_accounts / list_deals
# ─────────────────────────────────────────────────────────────────────────────

class TestTypedListMethods:
    async def test_list_contacts_returns_list(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_contacts = AsyncMock(return_value=_zoho_page([_contact_record()]))
        c.http_client = mock_http
        result = await c.list_contacts()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "3692340000002345678"

    async def test_list_contacts_default_pagination(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_contacts = AsyncMock(return_value=_zoho_page([]))
        c.http_client = mock_http
        await c.list_contacts()
        mock_http.get_contacts.assert_called_once_with(1, 200)

    async def test_list_contacts_custom_pagination(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_contacts = AsyncMock(return_value=_zoho_page([]))
        c.http_client = mock_http
        await c.list_contacts(page=3, per_page=50)
        mock_http.get_contacts.assert_called_once_with(3, 50)

    async def test_list_leads_returns_list(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_leads = AsyncMock(return_value=_zoho_page([_lead_record()]))
        c.http_client = mock_http
        result = await c.list_leads()
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_leads_default_pagination(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_leads = AsyncMock(return_value=_zoho_page([]))
        c.http_client = mock_http
        await c.list_leads()
        mock_http.get_leads.assert_called_once_with(1, 200)

    async def test_list_accounts_returns_list(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_accounts = AsyncMock(return_value=_zoho_page([_account_record()]))
        c.http_client = mock_http
        result = await c.list_accounts()
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_accounts_default_pagination(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_accounts = AsyncMock(return_value=_zoho_page([]))
        c.http_client = mock_http
        await c.list_accounts()
        mock_http.get_accounts.assert_called_once_with(1, 200)

    async def test_list_deals_returns_list(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_deals = AsyncMock(return_value=_zoho_page([_deal_record()]))
        c.http_client = mock_http
        result = await c.list_deals()
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_deals_default_pagination(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_deals = AsyncMock(return_value=_zoho_page([]))
        c.http_client = mock_http
        await c.list_deals()
        mock_http.get_deals.assert_called_once_with(1, 200)

    async def test_list_contacts_empty_page(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_contacts = AsyncMock(return_value=_zoho_page([]))
        c.http_client = mock_http
        result = await c.list_contacts()
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# 14. get_contact()
# ─────────────────────────────────────────────────────────────────────────────

class TestGetContact:
    async def test_delegates_to_http_client(self) -> None:
        c = _make_connector()
        expected = {"data": [_contact_record("C001")]}
        mock_http = AsyncMock()
        mock_http.get_contact = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.get_contact("C001")
        assert result == expected
        mock_http.get_contact.assert_called_once_with("C001")

    async def test_creates_client_if_none(self) -> None:
        c = _make_connector()
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get_contact = AsyncMock(return_value={"data": [{}]})
            MockClient.return_value = mock_instance
            await c.get_contact("C001")
        assert c.http_client is not None

    async def test_not_found_raises(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.get_contact = AsyncMock(
            side_effect=ZohoCRMNotFoundError("Contact", "MISSING")
        )
        c.http_client = mock_http
        with pytest.raises(ZohoCRMNotFoundError):
            await with_retry(mock_http.get_contact, "MISSING", max_attempts=1, base_delay=0)


# ─────────────────────────────────────────────────────────────────────────────
# 15. list_records() / get_record() / search_records()
# ─────────────────────────────────────────────────────────────────────────────

class TestGenericRecordMethods:
    async def test_list_records_returns_result(self) -> None:
        c = _make_connector()
        expected = _zoho_page([_lead_record()])
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.list_records("Leads")
        assert result == expected

    async def test_list_records_passes_page_and_per_page(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(return_value=_zoho_page([]))
        c.http_client = mock_http
        await c.list_records("Contacts", page=2, per_page=50)
        mock_http.list_records.assert_called_once_with("Contacts", 2, 50)

    async def test_list_records_creates_client_if_none(self) -> None:
        c = _make_connector()
        assert c.http_client is None
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.list_records = AsyncMock(return_value=_zoho_page([]))
            MockClient.return_value = mock_instance
            await c.list_records("Accounts")
        assert c.http_client is not None

    async def test_list_records_any_module_works(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.list_records = AsyncMock(return_value=_zoho_page([]))
        c.http_client = mock_http
        result = await c.list_records("Tasks")
        assert "data" in result

    async def test_get_record_delegates_to_http_client(self) -> None:
        c = _make_connector()
        expected = {"data": [_lead_record("LEAD001")]}
        mock_http = AsyncMock()
        mock_http.get_record = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.get_record("Leads", "LEAD001")
        assert result == expected
        mock_http.get_record.assert_called_once_with("Leads", "LEAD001")

    async def test_get_record_creates_client_if_none(self) -> None:
        c = _make_connector()
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get_record = AsyncMock(return_value={"data": [{}]})
            MockClient.return_value = mock_instance
            await c.get_record("Contacts", "C001")
        assert c.http_client is not None

    async def test_search_records_delegates_to_http_client(self) -> None:
        c = _make_connector()
        expected = {"data": [_contact_record()]}
        mock_http = AsyncMock()
        mock_http.search_records = AsyncMock(return_value=expected)
        c.http_client = mock_http
        result = await c.search_records("Contacts", "(Last_Name:equals:Jones)")
        assert result == expected
        mock_http.search_records.assert_called_once_with("Contacts", "(Last_Name:equals:Jones)")

    async def test_search_records_creates_client_if_none(self) -> None:
        c = _make_connector()
        with patch("connector.ZohoCRMHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.search_records = AsyncMock(return_value={"data": []})
            MockClient.return_value = mock_instance
            await c.search_records("Leads", "(Email:equals:test@example.com)")
        assert c.http_client is not None


# ─────────────────────────────────────────────────────────────────────────────
# 16. aclose()
# ─────────────────────────────────────────────────────────────────────────────

class TestAclose:
    async def test_aclose_calls_http_client_aclose(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        mock_http.aclose = AsyncMock()
        c.http_client = mock_http
        await c.aclose()
        mock_http.aclose.assert_called_once()
        assert c.http_client is None

    async def test_aclose_safe_when_no_client(self) -> None:
        c = _make_connector()
        assert c.http_client is None
        await c.aclose()  # must not raise

    async def test_aclose_sets_http_client_none(self) -> None:
        c = _make_connector()
        c.http_client = AsyncMock()
        await c.aclose()
        assert c.http_client is None

    async def test_double_aclose_safe(self) -> None:
        c = _make_connector()
        c.http_client = AsyncMock()
        await c.aclose()
        await c.aclose()  # second call must not raise


# ─────────────────────────────────────────────────────────────────────────────
# 17. Context manager
# ─────────────────────────────────────────────────────────────────────────────

class TestContextManager:
    async def test_async_context_manager(self) -> None:
        c = _make_connector()
        mock_http = AsyncMock()
        c.http_client = mock_http
        async with c as ctx:
            assert ctx is c
        mock_http.aclose.assert_called_once()
