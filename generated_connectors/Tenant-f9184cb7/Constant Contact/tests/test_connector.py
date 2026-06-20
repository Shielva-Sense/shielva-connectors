"""Unit tests for ConstantContactConnector — all HTTP calls are mocked.

60+ tests covering:
- All 5 exception classes (attributes, hierarchy, instantiation)
- All model enums/dataclasses (AuthStatus, ConnectorHealth, SyncStatus, all fields)
- normalize_contact (stable IDs using contact_id, full/minimal records)
- normalize_campaign (stable IDs using campaign_id, full/minimal records)
- normalize_list (stable IDs using list_id, full/minimal records)
- with_retry (success, retry on network error, no retry on auth, exhausted)
- ConstantContactHTTPClient (Bearer header, cursor extraction from _links.next.href,
  all 7 endpoints, _raise_for_status for 401/403/404/429/500)
- authorize() (URL construction, client_id present, scope present, redirect_uri optional)
- install() (success, missing client_id, missing client_secret)
- health_check() (healthy with org name, auth error → TOKEN_EXPIRED, network error, missing token)
- sync() (returns SyncResult, counts contacts + campaigns, partial on normalize failure)
- list_contacts (cursor pagination, return types, empty)
- list_campaigns (return types, empty)
- list_contact_lists (return types, empty)
- get_contact (return type, normalize)
- Context manager (__aenter__/__aexit__)
- aclose()
- Multi-tenant isolation
"""
from __future__ import annotations

import hashlib
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from connector import ConstantContactConnector
from exceptions import (
    ConstantContactAuthError,
    ConstantContactError,
    ConstantContactNetworkError,
    ConstantContactNotFoundError,
    ConstantContactRateLimitError,
)
from helpers.utils import (
    normalize_campaign,
    normalize_contact,
    normalize_list,
    with_retry,
    _stable_id,
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
from client.http_client import ConstantContactHTTPClient

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "tenant_cc_test_001"
CONNECTOR_ID = "conn_constant_contact_001"
CLIENT_ID = "test_cc_client_id"
CLIENT_SECRET = "test_cc_client_secret"
ACCESS_TOKEN = "test_access_token_abc123"
REFRESH_TOKEN = "test_refresh_token_xyz789"

VALID_CONFIG = {
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "access_token": ACCESS_TOKEN,
    "refresh_token": REFRESH_TOKEN,
}

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_CONTACT = {
    "contact_id": "contact-abc-001",
    "first_name": "Jane",
    "last_name": "Doe",
    "email_address": {"address": "jane@example.com", "permission_to_send": "implicit"},
    "phone_numbers": [{"phone_number": "+1-555-0100", "kind": "mobile"}],
    "company_name": "Acme Corp",
    "job_title": "Marketing Manager",
    "create_source": "Account",
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-06-01T00:00:00Z",
    "list_memberships": ["list-id-001"],
}

MINIMAL_CONTACT = {
    "contact_id": "contact-minimal-001",
}

SAMPLE_CAMPAIGN = {
    "campaign_id": "campaign-abc-001",
    "name": "Spring Newsletter 2024",
    "current_status": "Draft",
    "campaign_type": "EMAIL",
    "created_at": "2024-03-01T00:00:00Z",
    "updated_at": "2024-04-01T00:00:00Z",
    "campaign_activities": [{"campaign_activity_id": "act-001"}],
}

MINIMAL_CAMPAIGN = {
    "campaign_id": "campaign-minimal-001",
}

SAMPLE_LIST = {
    "list_id": "list-abc-001",
    "name": "Newsletter Subscribers",
    "description": "Monthly newsletter subscribers",
    "status": "ACTIVE",
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-06-01T00:00:00Z",
    "membership_count": 1500,
}

MINIMAL_LIST = {
    "list_id": "list-minimal-001",
}

SAMPLE_ACCOUNT = {
    "organization_name": "Test Organization",
    "first_name": "John",
    "last_name": "Smith",
}

CONTACTS_PAGE_1 = {
    "contacts": [SAMPLE_CONTACT],
    "_links": {
        "next": {"href": "https://api.cc.email/v3/contacts?cursor=cursor_abc123&limit=500"}
    },
}

CONTACTS_PAGE_2 = {
    "contacts": [MINIMAL_CONTACT],
    "_links": {},
}

CAMPAIGNS_RESP = {
    "campaigns": [SAMPLE_CAMPAIGN],
    "_links": {},
}

CONTACT_LISTS_RESP = {
    "lists": [SAMPLE_LIST],
    "_links": {},
}


# ═══════════════════════════════════════════════════════════════════════════
# 1. Exception classes
# ═══════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_base_error_attributes(self):
        err = ConstantContactError("test error", status_code=500, code="server_error")
        assert err.message == "test error"
        assert err.status_code == 500
        assert err.code == "server_error"
        assert str(err) == "test error"

    def test_base_error_defaults(self):
        err = ConstantContactError("basic")
        assert err.status_code == 0
        assert err.code == ""

    def test_auth_error_inherits_base(self):
        err = ConstantContactAuthError("unauthorized", status_code=401)
        assert isinstance(err, ConstantContactError)
        assert err.status_code == 401

    def test_network_error_inherits_base(self):
        err = ConstantContactNetworkError("timeout")
        assert isinstance(err, ConstantContactError)

    def test_not_found_error_fields(self):
        err = ConstantContactNotFoundError("contact", "contact-abc-001")
        assert isinstance(err, ConstantContactError)
        assert err.status_code == 404
        assert err.code == "resource_missing"
        assert "contact-abc-001" in str(err)

    def test_rate_limit_error_fields(self):
        err = ConstantContactRateLimitError("too many requests", retry_after=5.0)
        assert isinstance(err, ConstantContactError)
        assert err.status_code == 429
        assert err.code == "rate_limit"
        assert err.retry_after == 5.0

    def test_rate_limit_error_default_retry_after(self):
        err = ConstantContactRateLimitError("rate limited")
        assert err.retry_after == 0.0

    def test_exception_hierarchy(self):
        """All specific errors are catchable as ConstantContactError."""
        for exc_cls in [
            ConstantContactAuthError,
            ConstantContactNetworkError,
            ConstantContactNotFoundError,
            ConstantContactRateLimitError,
        ]:
            instance = exc_cls("test") if exc_cls != ConstantContactNotFoundError else exc_cls("res", "id")
            assert isinstance(instance, ConstantContactError)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Models
# ═══════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_auth_status_values(self):
        assert AuthStatus.CONNECTED.value == "connected"
        assert AuthStatus.MISSING_CREDENTIALS.value == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS.value == "invalid_credentials"
        assert AuthStatus.FAILED.value == "failed"
        assert AuthStatus.PENDING.value == "pending"
        assert AuthStatus.TOKEN_EXPIRED.value == "token_expired"

    def test_connector_health_values(self):
        assert ConnectorHealth.HEALTHY.value == "healthy"
        assert ConnectorHealth.DEGRADED.value == "degraded"
        assert ConnectorHealth.OFFLINE.value == "offline"

    def test_sync_status_values(self):
        assert SyncStatus.COMPLETED.value == "completed"
        assert SyncStatus.PARTIAL.value == "partial"
        assert SyncStatus.FAILED.value == "failed"
        assert SyncStatus.RUNNING.value == "running"

    def test_install_result_fields(self):
        r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result_fields(self):
        r = HealthCheckResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.health == ConnectorHealth.HEALTHY
        assert r.organization_name == ""
        assert r.user_name == ""
        assert r.message == ""

    def test_sync_result_fields(self):
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_connector_document_fields(self):
        doc = ConnectorDocument(
            id="abc123",
            source="constant_contact",
            title="Test",
            content="Content",
            type="contact",
        )
        assert doc.id == "abc123"
        assert doc.source == "constant_contact"
        assert doc.type == "contact"
        assert doc.metadata == {}
        assert doc.connector_id == ""
        assert doc.tenant_id == ""


# ═══════════════════════════════════════════════════════════════════════════
# 3. Normalizers
# ═══════════════════════════════════════════════════════════════════════════

class TestNormalizers:
    def test_normalize_contact_id_uses_contact_id(self):
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        expected_id = hashlib.sha256(b"contact:contact-abc-001").hexdigest()[:16]
        assert doc.id == expected_id

    def test_normalize_contact_type(self):
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert doc.type == "contact"

    def test_normalize_contact_title(self):
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Jane Doe"

    def test_normalize_contact_metadata_keys(self):
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["contact_id"] == "contact-abc-001"
        assert doc.metadata["email"] == "jane@example.com"
        assert doc.metadata["company_name"] == "Acme Corp"
        assert doc.metadata["phone"] == "+1-555-0100"

    def test_normalize_contact_content_contains_name(self):
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert "Jane Doe" in doc.content

    def test_normalize_contact_content_contains_email(self):
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert "jane@example.com" in doc.content

    def test_normalize_contact_minimal(self):
        doc = normalize_contact(MINIMAL_CONTACT, CONNECTOR_ID, TENANT_ID)
        expected_id = hashlib.sha256(b"contact:contact-minimal-001").hexdigest()[:16]
        assert doc.id == expected_id
        assert doc.type == "contact"
        assert doc.source == "constant_contact"

    def test_normalize_contact_source(self):
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert doc.source == "constant_contact"

    def test_normalize_contact_connector_id_and_tenant_id(self):
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_normalize_contact_stable_id_deterministic(self):
        doc1 = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_contact(SAMPLE_CONTACT, "other_connector", "other_tenant")
        # ID is based on contact_id only, not connector/tenant
        assert doc1.id == doc2.id

    def test_normalize_campaign_id_uses_campaign_id(self):
        doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
        expected_id = hashlib.sha256(b"campaign:campaign-abc-001").hexdigest()[:16]
        assert doc.id == expected_id

    def test_normalize_campaign_type(self):
        doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
        assert doc.type == "email_campaign"

    def test_normalize_campaign_title(self):
        doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Spring Newsletter 2024"

    def test_normalize_campaign_metadata_keys(self):
        doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["campaign_id"] == "campaign-abc-001"
        assert doc.metadata["status"] == "Draft"
        assert doc.metadata["campaign_type"] == "EMAIL"

    def test_normalize_campaign_content(self):
        doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
        assert "Spring Newsletter 2024" in doc.content

    def test_normalize_campaign_minimal(self):
        doc = normalize_campaign(MINIMAL_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
        expected_id = hashlib.sha256(b"campaign:campaign-minimal-001").hexdigest()[:16]
        assert doc.id == expected_id
        assert doc.type == "email_campaign"

    def test_normalize_campaign_stable_id_deterministic(self):
        doc1 = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_campaign(SAMPLE_CAMPAIGN, "other", "other")
        assert doc1.id == doc2.id

    def test_normalize_list_id_uses_list_id(self):
        doc = normalize_list(SAMPLE_LIST, CONNECTOR_ID, TENANT_ID)
        expected_id = hashlib.sha256(b"list:list-abc-001").hexdigest()[:16]
        assert doc.id == expected_id

    def test_normalize_list_type(self):
        doc = normalize_list(SAMPLE_LIST, CONNECTOR_ID, TENANT_ID)
        assert doc.type == "contact_list"

    def test_normalize_list_title(self):
        doc = normalize_list(SAMPLE_LIST, CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Newsletter Subscribers"

    def test_normalize_list_metadata_keys(self):
        doc = normalize_list(SAMPLE_LIST, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["list_id"] == "list-abc-001"
        assert doc.metadata["status"] == "ACTIVE"
        assert doc.metadata["membership_count"] == 1500

    def test_normalize_list_content_includes_name(self):
        doc = normalize_list(SAMPLE_LIST, CONNECTOR_ID, TENANT_ID)
        assert "Newsletter Subscribers" in doc.content

    def test_normalize_list_minimal(self):
        doc = normalize_list(MINIMAL_LIST, CONNECTOR_ID, TENANT_ID)
        expected_id = hashlib.sha256(b"list:list-minimal-001").hexdigest()[:16]
        assert doc.id == expected_id
        assert doc.type == "contact_list"

    def test_stable_id_uses_sha256_prefix(self):
        result = _stable_id("contact", "abc123")
        expected = hashlib.sha256(b"contact:abc123").hexdigest()[:16]
        assert result == expected
        assert len(result) == 16

    def test_stable_id_different_prefixes_differ(self):
        a = _stable_id("contact", "abc")
        b = _stable_id("campaign", "abc")
        assert a != b


# ═══════════════════════════════════════════════════════════════════════════
# 4. with_retry
# ═══════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_success_on_first_attempt(self):
        mock_fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock_fn, max_retries=3, base_delay=0)
        assert result == {"ok": True}
        assert mock_fn.call_count == 1

    async def test_retries_on_network_error(self):
        mock_fn = AsyncMock(
            side_effect=[
                ConstantContactNetworkError("timeout"),
                ConstantContactNetworkError("timeout"),
                {"ok": True},
            ]
        )
        result = await with_retry(mock_fn, max_retries=3, base_delay=0)
        assert result == {"ok": True}
        assert mock_fn.call_count == 3

    async def test_no_retry_on_auth_error(self):
        mock_fn = AsyncMock(side_effect=ConstantContactAuthError("unauthorized"))
        with pytest.raises(ConstantContactAuthError):
            await with_retry(mock_fn, max_retries=3, base_delay=0)
        assert mock_fn.call_count == 1

    async def test_exhausted_raises_last_exception(self):
        mock_fn = AsyncMock(side_effect=ConstantContactNetworkError("network down"))
        with pytest.raises(ConstantContactNetworkError, match="network down"):
            await with_retry(mock_fn, max_retries=2, base_delay=0)
        assert mock_fn.call_count == 2

    async def test_retries_on_rate_limit(self):
        mock_fn = AsyncMock(
            side_effect=[
                ConstantContactRateLimitError("rate limited", retry_after=0),
                {"ok": True},
            ]
        )
        result = await with_retry(mock_fn, max_retries=3, base_delay=0)
        assert result == {"ok": True}
        assert mock_fn.call_count == 2

    async def test_rate_limit_exhausted_raises(self):
        mock_fn = AsyncMock(side_effect=ConstantContactRateLimitError("rate limited"))
        with pytest.raises(ConstantContactRateLimitError):
            await with_retry(mock_fn, max_retries=2, base_delay=0)


# ═══════════════════════════════════════════════════════════════════════════
# 5. HTTP Client — cursor extraction
# ═══════════════════════════════════════════════════════════════════════════

class TestHTTPClientCursorExtraction:
    def test_extract_cursor_from_links_next_href(self):
        links = {"next": {"href": "https://api.cc.email/v3/contacts?cursor=abc123&limit=500"}}
        cursor = ConstantContactHTTPClient._extract_cursor(links)
        assert cursor == "abc123"

    def test_extract_cursor_no_next(self):
        links = {}
        cursor = ConstantContactHTTPClient._extract_cursor(links)
        assert cursor is None

    def test_extract_cursor_none_links(self):
        cursor = ConstantContactHTTPClient._extract_cursor(None)
        assert cursor is None

    def test_extract_cursor_no_cursor_param(self):
        links = {"next": {"href": "https://api.cc.email/v3/contacts?limit=500"}}
        cursor = ConstantContactHTTPClient._extract_cursor(links)
        assert cursor is None

    def test_extract_cursor_empty_next(self):
        links = {"next": {}}
        cursor = ConstantContactHTTPClient._extract_cursor(links)
        assert cursor is None


# ═══════════════════════════════════════════════════════════════════════════
# 6. HTTP Client — Bearer auth header
# ═══════════════════════════════════════════════════════════════════════════

class TestHTTPClientAuthHeader:
    def test_bearer_header_set(self):
        client = ConstantContactHTTPClient(access_token="mytoken123")
        headers = client._auth_headers()
        assert headers["Authorization"] == "Bearer mytoken123"

    def test_content_type_header(self):
        client = ConstantContactHTTPClient(access_token="tok")
        headers = client._auth_headers()
        assert headers["Content-Type"] == "application/json"

    def test_accept_header(self):
        client = ConstantContactHTTPClient(access_token="tok")
        headers = client._auth_headers()
        assert headers["Accept"] == "application/json"


# ═══════════════════════════════════════════════════════════════════════════
# 7. HTTP Client — _raise_for_status
# ═══════════════════════════════════════════════════════════════════════════

class TestHTTPClientRaiseForStatus:
    def _make_mock_response(self, status: int, body: dict | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.headers = {}
        import asyncio

        async def _json(**_kwargs):
            return body or {}

        resp.json = _json
        return resp

    async def test_200_does_not_raise(self):
        client = ConstantContactHTTPClient(access_token="tok")
        resp = self._make_mock_response(200)
        await client._raise_for_status(resp)  # no exception

    async def test_401_raises_auth_error(self):
        client = ConstantContactHTTPClient(access_token="tok")
        resp = self._make_mock_response(401, {"error_message": "Unauthorized"})
        with pytest.raises(ConstantContactAuthError):
            await client._raise_for_status(resp, "get_contacts")

    async def test_403_raises_auth_error(self):
        client = ConstantContactHTTPClient(access_token="tok")
        resp = self._make_mock_response(403)
        with pytest.raises(ConstantContactAuthError):
            await client._raise_for_status(resp)

    async def test_404_raises_not_found(self):
        client = ConstantContactHTTPClient(access_token="tok")
        resp = self._make_mock_response(404)
        with pytest.raises(ConstantContactNotFoundError):
            await client._raise_for_status(resp, "get_contact")

    async def test_429_raises_rate_limit(self):
        client = ConstantContactHTTPClient(access_token="tok")
        resp = self._make_mock_response(429)
        resp.headers = {"Retry-After": "10"}
        with pytest.raises(ConstantContactRateLimitError) as exc_info:
            await client._raise_for_status(resp)
        assert exc_info.value.retry_after == 10.0

    async def test_500_raises_base_error(self):
        client = ConstantContactHTTPClient(access_token="tok")
        resp = self._make_mock_response(500, {"message": "Internal Server Error"})
        with pytest.raises(ConstantContactError):
            await client._raise_for_status(resp)


# ═══════════════════════════════════════════════════════════════════════════
# 8. HTTP Client — endpoint methods (mocked _get)
# ═══════════════════════════════════════════════════════════════════════════

class TestHTTPClientEndpoints:
    def _make_client(self) -> ConstantContactHTTPClient:
        return ConstantContactHTTPClient(
            access_token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )

    async def test_get_account_info_calls_correct_path(self):
        client = self._make_client()
        client._get = AsyncMock(return_value=SAMPLE_ACCOUNT)
        result = await client.get_account_info()
        client._get.assert_called_once_with("/v3/account/summary")
        assert result == SAMPLE_ACCOUNT

    async def test_get_contacts_no_cursor(self):
        client = self._make_client()
        client._get = AsyncMock(return_value={"contacts": []})
        await client.get_contacts()
        call_args = client._get.call_args
        assert call_args[0][0] == "/v3/contacts"
        assert "cursor" not in call_args[1]["params"]

    async def test_get_contacts_with_cursor(self):
        client = self._make_client()
        client._get = AsyncMock(return_value={"contacts": []})
        await client.get_contacts(cursor="cur_123")
        params = client._get.call_args[1]["params"]
        assert params["cursor"] == "cur_123"

    async def test_get_contacts_custom_limit(self):
        client = self._make_client()
        client._get = AsyncMock(return_value={"contacts": []})
        await client.get_contacts(limit=100)
        params = client._get.call_args[1]["params"]
        assert params["limit"] == 100

    async def test_get_contact_calls_correct_path(self):
        client = self._make_client()
        client._get = AsyncMock(return_value=SAMPLE_CONTACT)
        result = await client.get_contact("contact-abc-001")
        client._get.assert_called_once_with("/v3/contacts/contact-abc-001")
        assert result == SAMPLE_CONTACT

    async def test_get_contact_lists_no_cursor(self):
        client = self._make_client()
        client._get = AsyncMock(return_value={"lists": []})
        await client.get_contact_lists()
        client._get.assert_called_once_with("/v3/contact_lists", params={})

    async def test_get_contact_lists_with_cursor(self):
        client = self._make_client()
        client._get = AsyncMock(return_value={"lists": []})
        await client.get_contact_lists(cursor="cur_abc")
        params = client._get.call_args[1]["params"]
        assert params["cursor"] == "cur_abc"

    async def test_get_email_campaigns_calls_correct_path(self):
        client = self._make_client()
        client._get = AsyncMock(return_value={"campaigns": []})
        await client.get_email_campaigns()
        client._get.assert_called_once_with("/v3/emails", params={})

    async def test_get_campaign_activity_calls_correct_path(self):
        client = self._make_client()
        client._get = AsyncMock(return_value={})
        await client.get_campaign_activity("act-001")
        client._get.assert_called_once_with("/v3/emails/activities/act-001")

    async def test_get_campaign_reports_calls_correct_path(self):
        client = self._make_client()
        client._get = AsyncMock(return_value={})
        await client.get_campaign_reports()
        client._get.assert_called_once_with(
            "/v3/reports/email_reports/campaign_sends", params={}
        )


# ═══════════════════════════════════════════════════════════════════════════
# 9. Connector class attributes
# ═══════════════════════════════════════════════════════════════════════════

class TestConnectorAttributes:
    def test_connector_type(self):
        assert ConstantContactConnector.CONNECTOR_TYPE == "constant_contact"

    def test_auth_type(self):
        assert ConstantContactConnector.AUTH_TYPE == "oauth2"

    def test_connector_name(self):
        assert ConstantContactConnector.CONNECTOR_NAME == "Constant Contact"

    def test_auth_url_is_constant_contact(self):
        assert "constantcontact.com" in ConstantContactConnector.AUTH_URL
        assert "authorize" in ConstantContactConnector.AUTH_URL

    def test_token_url_is_constant_contact(self):
        assert "constantcontact.com" in ConstantContactConnector.TOKEN_URL
        assert "token" in ConstantContactConnector.TOKEN_URL

    def test_scopes_include_contact_data(self):
        assert "contact_data" in ConstantContactConnector.SCOPES

    def test_scopes_include_campaign_data(self):
        assert "campaign_data" in ConstantContactConnector.SCOPES

    def test_scopes_include_account_read(self):
        assert "account_read" in ConstantContactConnector.SCOPES


# ═══════════════════════════════════════════════════════════════════════════
# 10. install()
# ═══════════════════════════════════════════════════════════════════════════

class TestInstall:
    def _make_connector(self, config: dict | None = None) -> ConstantContactConnector:
        return ConstantContactConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config or {},
        )

    async def test_install_success(self):
        conn = self._make_connector(VALID_CONFIG)
        result = await conn.install()
        assert isinstance(result, InstallResult)
        assert result.auth_status == AuthStatus.PENDING
        assert result.health == ConnectorHealth.HEALTHY

    async def test_install_missing_client_id(self):
        conn = self._make_connector({"client_secret": CLIENT_SECRET})
        result = await conn.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert result.health == ConnectorHealth.OFFLINE
        assert "client_id" in result.message

    async def test_install_missing_client_secret(self):
        conn = self._make_connector({"client_id": CLIENT_ID})
        result = await conn.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert result.health == ConnectorHealth.OFFLINE
        assert "client_secret" in result.message

    async def test_install_empty_config(self):
        conn = self._make_connector({})
        result = await conn.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_message_mentions_oauth(self):
        conn = self._make_connector(VALID_CONFIG)
        result = await conn.install()
        assert "OAuth" in result.message or "oauth" in result.message.lower() or "installed" in result.message.lower()


# ═══════════════════════════════════════════════════════════════════════════
# 11. authorize()
# ═══════════════════════════════════════════════════════════════════════════

class TestAuthorize:
    def _make_connector(self, config: dict | None = None) -> ConstantContactConnector:
        return ConstantContactConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config or VALID_CONFIG,
        )

    async def test_authorize_returns_url_string(self):
        conn = self._make_connector()
        url = await conn.authorize()
        assert isinstance(url, str)
        assert url.startswith("https://")

    async def test_authorize_url_contains_client_id(self):
        conn = self._make_connector()
        url = await conn.authorize()
        assert CLIENT_ID in url

    async def test_authorize_url_contains_scopes(self):
        conn = self._make_connector()
        url = await conn.authorize()
        assert "contact_data" in url

    async def test_authorize_url_contains_response_type_code(self):
        conn = self._make_connector()
        url = await conn.authorize()
        assert "response_type=code" in url

    async def test_authorize_url_contains_redirect_uri_when_provided(self):
        config = {**VALID_CONFIG, "redirect_uri": "https://myapp.com/callback"}
        conn = self._make_connector(config)
        url = await conn.authorize()
        assert "redirect_uri=" in url
        assert "myapp.com" in url

    async def test_authorize_url_no_redirect_uri_when_absent(self):
        config = {k: v for k, v in VALID_CONFIG.items() if k != "redirect_uri"}
        conn = self._make_connector(config)
        url = await conn.authorize()
        # Should still return a valid URL
        assert "constantcontact.com" in url

    async def test_authorize_url_contains_authorize_path(self):
        conn = self._make_connector()
        url = await conn.authorize()
        assert "authorize" in url


# ═══════════════════════════════════════════════════════════════════════════
# 12. health_check()
# ═══════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    def _make_connector(self, config: dict | None = None) -> ConstantContactConnector:
        return ConstantContactConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config or VALID_CONFIG,
        )

    async def test_health_check_healthy_with_org_name(self):
        conn = self._make_connector()
        conn._ensure_client = MagicMock(return_value=MagicMock(
            get_account_info=AsyncMock(return_value=SAMPLE_ACCOUNT)
        ))
        result = await conn.health_check()
        assert isinstance(result, HealthCheckResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Test Organization" in result.message or result.organization_name == "Test Organization"

    async def test_health_check_organization_name_populated(self):
        conn = self._make_connector()
        conn._ensure_client = MagicMock(return_value=MagicMock(
            get_account_info=AsyncMock(return_value=SAMPLE_ACCOUNT)
        ))
        result = await conn.health_check()
        assert result.organization_name == "Test Organization"

    async def test_health_check_auth_error_returns_token_expired(self):
        conn = self._make_connector()
        conn._ensure_client = MagicMock(return_value=MagicMock(
            get_account_info=AsyncMock(side_effect=ConstantContactAuthError("401 Unauthorized"))
        ))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.TOKEN_EXPIRED

    async def test_health_check_network_error(self):
        conn = self._make_connector()
        conn._ensure_client = MagicMock(return_value=MagicMock(
            get_account_info=AsyncMock(side_effect=ConstantContactNetworkError("timeout"))
        ))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_missing_token(self):
        config = {k: v for k, v in VALID_CONFIG.items() if k != "access_token"}
        conn = self._make_connector(config)
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_generic_error(self):
        conn = self._make_connector()
        conn._ensure_client = MagicMock(return_value=MagicMock(
            get_account_info=AsyncMock(side_effect=ConstantContactError("server error", 500))
        ))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# 13. sync()
# ═══════════════════════════════════════════════════════════════════════════

class TestSync:
    def _make_connector(self, config: dict | None = None) -> ConstantContactConnector:
        return ConstantContactConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config or VALID_CONFIG,
        )

    async def test_sync_returns_sync_result(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contacts = AsyncMock(return_value={"contacts": [], "_links": {}})
        mock_client.get_email_campaigns = AsyncMock(return_value={"campaigns": [], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_counts_contacts_and_campaigns(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contacts = AsyncMock(return_value={
            "contacts": [SAMPLE_CONTACT, MINIMAL_CONTACT],
            "_links": {},
        })
        mock_client.get_email_campaigns = AsyncMock(return_value={
            "campaigns": [SAMPLE_CAMPAIGN],
            "_links": {},
        })
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.sync()
        assert result.documents_found == 3  # 2 contacts + 1 campaign
        assert result.documents_synced == 3
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_completed_when_no_failures(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contacts = AsyncMock(return_value={"contacts": [SAMPLE_CONTACT], "_links": {}})
        mock_client.get_email_campaigns = AsyncMock(return_value={"campaigns": [], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_partial_on_normalize_failure(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        # Contact with no contact_id will still normalize (fallback to "")
        # We force a failure by patching normalize_contact to raise
        mock_client.get_contacts = AsyncMock(return_value={
            "contacts": [SAMPLE_CONTACT],
            "_links": {},
        })
        mock_client.get_email_campaigns = AsyncMock(return_value={"campaigns": [], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)

        with patch("connector.normalize_contact", side_effect=Exception("normalize failed")):
            result = await conn.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed == 1

    async def test_sync_cursor_pagination_contacts(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        call_count = {"n": 0}

        async def get_contacts_paged(cursor=None, limit=500):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "contacts": [SAMPLE_CONTACT],
                    "_links": {"next": {"href": "https://api.cc.email/v3/contacts?cursor=page2"}},
                }
            return {"contacts": [MINIMAL_CONTACT], "_links": {}}

        mock_client.get_contacts = get_contacts_paged
        mock_client.get_email_campaigns = AsyncMock(return_value={"campaigns": [], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.sync()
        assert call_count["n"] == 2
        assert result.documents_found >= 2

    async def test_sync_failed_on_exception(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contacts = AsyncMock(side_effect=ConstantContactNetworkError("timeout"))
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_message_contains_count(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contacts = AsyncMock(return_value={"contacts": [], "_links": {}})
        mock_client.get_email_campaigns = AsyncMock(return_value={"campaigns": [], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.sync()
        assert "0" in result.message or "Synced" in result.message


# ═══════════════════════════════════════════════════════════════════════════
# 14. list_contacts()
# ═══════════════════════════════════════════════════════════════════════════

class TestListContacts:
    def _make_connector(self) -> ConstantContactConnector:
        return ConstantContactConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
        )

    async def test_list_contacts_returns_list_of_documents(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contacts = AsyncMock(return_value={"contacts": [SAMPLE_CONTACT], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.list_contacts()
        assert isinstance(result, list)
        assert all(isinstance(d, ConnectorDocument) for d in result)

    async def test_list_contacts_empty(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contacts = AsyncMock(return_value={"contacts": [], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.list_contacts()
        assert result == []

    async def test_list_contacts_with_cursor(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contacts = AsyncMock(return_value={"contacts": [SAMPLE_CONTACT], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        await conn.list_contacts(cursor="some_cursor")
        mock_client.get_contacts.assert_called_once()
        call_kwargs = mock_client.get_contacts.call_args[1]
        assert call_kwargs.get("cursor") == "some_cursor"

    async def test_list_contacts_documents_have_correct_type(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contacts = AsyncMock(return_value={"contacts": [SAMPLE_CONTACT], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.list_contacts()
        assert result[0].type == "contact"


# ═══════════════════════════════════════════════════════════════════════════
# 15. list_campaigns()
# ═══════════════════════════════════════════════════════════════════════════

class TestListCampaigns:
    def _make_connector(self) -> ConstantContactConnector:
        return ConstantContactConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
        )

    async def test_list_campaigns_returns_list_of_documents(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_email_campaigns = AsyncMock(return_value={"campaigns": [SAMPLE_CAMPAIGN], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.list_campaigns()
        assert isinstance(result, list)
        assert all(isinstance(d, ConnectorDocument) for d in result)

    async def test_list_campaigns_empty(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_email_campaigns = AsyncMock(return_value={"campaigns": [], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.list_campaigns()
        assert result == []

    async def test_list_campaigns_documents_have_correct_type(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_email_campaigns = AsyncMock(return_value={"campaigns": [SAMPLE_CAMPAIGN], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.list_campaigns()
        assert result[0].type == "email_campaign"


# ═══════════════════════════════════════════════════════════════════════════
# 16. list_contact_lists()
# ═══════════════════════════════════════════════════════════════════════════

class TestListContactLists:
    def _make_connector(self) -> ConstantContactConnector:
        return ConstantContactConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
        )

    async def test_list_contact_lists_returns_list_of_documents(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contact_lists = AsyncMock(return_value={"lists": [SAMPLE_LIST], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.list_contact_lists()
        assert isinstance(result, list)
        assert all(isinstance(d, ConnectorDocument) for d in result)

    async def test_list_contact_lists_empty(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contact_lists = AsyncMock(return_value={"lists": [], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.list_contact_lists()
        assert result == []

    async def test_list_contact_lists_documents_have_correct_type(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contact_lists = AsyncMock(return_value={"lists": [SAMPLE_LIST], "_links": {}})
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.list_contact_lists()
        assert result[0].type == "contact_list"


# ═══════════════════════════════════════════════════════════════════════════
# 17. get_contact()
# ═══════════════════════════════════════════════════════════════════════════

class TestGetContact:
    def _make_connector(self) -> ConstantContactConnector:
        return ConstantContactConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
        )

    async def test_get_contact_returns_connector_document(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.get_contact("contact-abc-001")
        assert isinstance(result, ConnectorDocument)

    async def test_get_contact_calls_correct_id(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
        conn._ensure_client = MagicMock(return_value=mock_client)
        await conn.get_contact("contact-abc-001")
        mock_client.get_contact.assert_called_once()

    async def test_get_contact_normalized_type(self):
        conn = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
        conn._ensure_client = MagicMock(return_value=mock_client)
        result = await conn.get_contact("contact-abc-001")
        assert result.type == "contact"


# ═══════════════════════════════════════════════════════════════════════════
# 18. Lifecycle — aclose and context manager
# ═══════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    async def test_aclose_sets_http_client_to_none(self):
        conn = ConstantContactConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
        )
        conn._http_client = MagicMock()
        await conn.aclose()
        assert conn._http_client is None

    async def test_aclose_safe_when_no_client(self):
        conn = ConstantContactConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
        )
        conn._http_client = None
        await conn.aclose()  # should not raise

    async def test_context_manager_returns_connector(self):
        conn = ConstantContactConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
        )
        async with conn as c:
            assert c is conn

    async def test_context_manager_clears_client_on_exit(self):
        conn = ConstantContactConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
        )
        conn._http_client = MagicMock()
        async with conn:
            pass
        assert conn._http_client is None


# ═══════════════════════════════════════════════════════════════════════════
# 19. Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

class TestMultiTenantIsolation:
    async def test_two_connectors_have_independent_configs(self):
        conn_a = ConstantContactConnector(
            tenant_id="tenant_a", connector_id="conn_a",
            config={**VALID_CONFIG, "client_id": "client_a"},
        )
        conn_b = ConstantContactConnector(
            tenant_id="tenant_b", connector_id="conn_b",
            config={**VALID_CONFIG, "client_id": "client_b"},
        )
        assert conn_a.config["client_id"] == "client_a"
        assert conn_b.config["client_id"] == "client_b"

    async def test_normalized_documents_carry_tenant_id(self):
        doc_a = normalize_contact(SAMPLE_CONTACT, "conn_a", "tenant_a")
        doc_b = normalize_contact(SAMPLE_CONTACT, "conn_b", "tenant_b")
        assert doc_a.tenant_id == "tenant_a"
        assert doc_b.tenant_id == "tenant_b"

    async def test_normalized_documents_same_stable_id_across_tenants(self):
        """Stable ID is content-addressed, not tenant-scoped, so it can be deduplicated."""
        doc_a = normalize_contact(SAMPLE_CONTACT, "conn_a", "tenant_a")
        doc_b = normalize_contact(SAMPLE_CONTACT, "conn_b", "tenant_b")
        assert doc_a.id == doc_b.id
