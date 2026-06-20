"""Unit tests for FreshworksCRMConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import FreshworksCRMConnector
from exceptions import (
    FreshworksCRMAuthError,
    FreshworksCRMNetworkError,
    FreshworksCRMNotFoundError,
    FreshworksCRMRateLimitError,
)
from helpers.utils import (
    normalize_account,
    normalize_contact,
    normalize_deal,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_freshworks_crm_test_001"
DOMAIN = "acme"
API_KEY = "test_api_key_freshworks_xyz"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_OWNERS_RESPONSE: dict = {
    "users": [
        {"id": 1, "display_name": "Alice Sales", "email": "alice@acme.com"},
        {"id": 2, "display_name": "Bob Manager", "email": "bob@acme.com"},
    ]
}

SAMPLE_CONTACT: dict = {
    "id": 101,
    "first_name": "Jane",
    "last_name": "Doe",
    "display_name": "Jane Doe",
    "email": "jane.doe@example.com",
    "work_number": "+1-555-1000",
    "mobile_number": "+1-555-2000",
    "job_title": "VP of Engineering",
    "company": {"name": "Example Corp"},
    "owner_id": 1,
    "lead_source_id": 3,
    "linkedin": "https://linkedin.com/in/janedoe",
    "created_at": "2026-01-15T09:00:00Z",
    "updated_at": "2026-06-01T14:00:00Z",
}

SAMPLE_CONTACT_2: dict = {
    "id": 102,
    "first_name": "John",
    "last_name": "Smith",
    "display_name": "John Smith",
    "email": "john.smith@example.com",
    "work_number": "",
    "mobile_number": "+44-7700-900456",
    "job_title": "",
    "company": {"name": ""},
    "owner_id": 2,
    "lead_source_id": None,
    "linkedin": "",
    "created_at": "2026-02-20T11:30:00Z",
    "updated_at": "2026-05-30T10:00:00Z",
}

SAMPLE_CONTACTS_RESPONSE: dict = {
    "contacts": [SAMPLE_CONTACT, SAMPLE_CONTACT_2],
    "meta": {"total_pages": 1, "current_page": 1, "total_count": 2},
}

SAMPLE_DEAL: dict = {
    "id": 201,
    "name": "Acme Enterprise License",
    "amount": 50000.0,
    "deal_stage_id": 5,
    "probability": 75,
    "expected_close": "2026-09-30",
    "owner_id": 1,
    "lead_source_id": 2,
    "sales_account_id": 301,
    "contact_id": 101,
    "created_at": "2026-03-01T10:00:00Z",
    "updated_at": "2026-06-15T12:00:00Z",
}

SAMPLE_DEAL_2: dict = {
    "id": 202,
    "name": "Startup Pilot",
    "amount": 5000.0,
    "deal_stage_id": 2,
    "probability": 30,
    "expected_close": "2026-08-15",
    "owner_id": 2,
    "lead_source_id": None,
    "sales_account_id": None,
    "contact_id": None,
    "created_at": "2026-04-10T08:00:00Z",
    "updated_at": "2026-06-10T09:00:00Z",
}

SAMPLE_DEALS_RESPONSE: dict = {
    "deals": [SAMPLE_DEAL, SAMPLE_DEAL_2],
    "meta": {"total_pages": 1, "current_page": 1, "total_count": 2},
}

SAMPLE_ACCOUNT: dict = {
    "id": 301,
    "name": "Example Corp",
    "website": "https://example.com",
    "phone": "+1-800-555-0000",
    "industry_type_id": 7,
    "business_type_id": 2,
    "number_of_employees": 500,
    "annual_revenue": 10000000.0,
    "owner_id": 1,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z",
}

SAMPLE_ACCOUNT_2: dict = {
    "id": 302,
    "name": "Startup Inc",
    "website": "",
    "phone": "",
    "industry_type_id": None,
    "business_type_id": None,
    "number_of_employees": 12,
    "annual_revenue": None,
    "owner_id": 2,
    "created_at": "2026-02-01T00:00:00Z",
    "updated_at": "2026-05-15T00:00:00Z",
}

SAMPLE_ACCOUNTS_RESPONSE: dict = {
    "sales_accounts": [SAMPLE_ACCOUNT, SAMPLE_ACCOUNT_2],
    "meta": {"total_pages": 1, "current_page": 1, "total_count": 2},
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


def make_connector(domain: str = DOMAIN, api_key: str = API_KEY) -> FreshworksCRMConnector:
    return FreshworksCRMConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"domain": domain, "api_key": api_key},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. INSTALL
# ═══════════════════════════════════════════════════════════════════════════════


class TestInstall:
    @pytest.mark.asyncio
    async def test_install_success(self) -> None:
        connector = make_connector()
        with patch("connector.FreshworksCRMHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.list_owners = AsyncMock(return_value=SAMPLE_OWNERS_RESPONSE)
            instance.aclose = AsyncMock()
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "2 owner" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_domain(self) -> None:
        connector = make_connector(domain="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "domain" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_api_key(self) -> None:
        connector = make_connector(api_key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_both(self) -> None:
        connector = make_connector(domain="", api_key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "domain" in result.message
        assert "api_key" in result.message

    @pytest.mark.asyncio
    async def test_install_auth_error(self) -> None:
        connector = make_connector()
        with patch("connector.FreshworksCRMHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.list_owners = AsyncMock(
                side_effect=FreshworksCRMAuthError("Authentication failed", 401)
            )
            instance.aclose = AsyncMock()
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_network_error(self) -> None:
        connector = make_connector()
        with patch("connector.FreshworksCRMHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.list_owners = AsyncMock(
                side_effect=FreshworksCRMNetworkError("Connection refused")
            )
            instance.aclose = AsyncMock()
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_install_empty_owners(self) -> None:
        connector = make_connector()
        with patch("connector.FreshworksCRMHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.list_owners = AsyncMock(return_value={"users": []})
            instance.aclose = AsyncMock()
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert "0 owner" in result.message

    @pytest.mark.asyncio
    async def test_install_stores_connector_id(self) -> None:
        connector = make_connector()
        with patch("connector.FreshworksCRMHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.list_owners = AsyncMock(return_value=SAMPLE_OWNERS_RESPONSE)
            instance.aclose = AsyncMock()
            result = await connector.install()
        assert result.connector_id == CONNECTOR_ID


# ═══════════════════════════════════════════════════════════════════════════════
# 2. HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_healthy(self) -> None:
        connector = make_connector()
        with patch("connector.FreshworksCRMHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.list_owners = AsyncMock(return_value=SAMPLE_OWNERS_RESPONSE)
            instance.aclose = AsyncMock()
            result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_health_check_missing_creds(self) -> None:
        connector = make_connector(domain="", api_key="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self) -> None:
        connector = make_connector()
        with patch("connector.FreshworksCRMHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.list_owners = AsyncMock(
                side_effect=FreshworksCRMAuthError("Invalid token", 401)
            )
            instance.aclose = AsyncMock()
            result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error(self) -> None:
        connector = make_connector()
        with patch("connector.FreshworksCRMHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.list_owners = AsyncMock(
                side_effect=FreshworksCRMNetworkError("Timeout")
            )
            instance.aclose = AsyncMock()
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_generic_error(self) -> None:
        connector = make_connector()
        with patch("connector.FreshworksCRMHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.list_owners = AsyncMock(side_effect=RuntimeError("unexpected"))
            instance.aclose = AsyncMock()
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 3. LIST / GET CONTACTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestContacts:
    @pytest.mark.asyncio
    async def test_list_contacts_returns_list(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.list_contacts = AsyncMock(
            return_value=SAMPLE_CONTACTS_RESPONSE
        )
        result = await connector.list_contacts(page=1, per_page=100)
        assert len(result) == 2
        assert result[0]["id"] == 101
        assert result[1]["id"] == 102

    @pytest.mark.asyncio
    async def test_list_contacts_page_2(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.list_contacts = AsyncMock(
            return_value={"contacts": [], "meta": {"total_pages": 1, "current_page": 2}}
        )
        result = await connector.list_contacts(page=2)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_contact_unwraps_wrapper(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.get_contact = AsyncMock(
            return_value={"contact": SAMPLE_CONTACT}
        )
        result = await connector.get_contact(101)
        assert result["id"] == 101
        assert result["email"] == "jane.doe@example.com"

    @pytest.mark.asyncio
    async def test_get_contact_no_wrapper(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
        result = await connector.get_contact(101)
        assert result["id"] == 101

    @pytest.mark.asyncio
    async def test_get_contact_not_found(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.get_contact = AsyncMock(
            side_effect=FreshworksCRMNotFoundError("contact", "999")
        )
        with pytest.raises(FreshworksCRMNotFoundError):
            await connector.get_contact(999)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. LIST / GET DEALS
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeals:
    @pytest.mark.asyncio
    async def test_list_deals_returns_list(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.list_deals = AsyncMock(
            return_value=SAMPLE_DEALS_RESPONSE
        )
        result = await connector.list_deals(page=1)
        assert len(result) == 2
        assert result[0]["id"] == 201
        assert result[1]["name"] == "Startup Pilot"

    @pytest.mark.asyncio
    async def test_list_deals_empty(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.list_deals = AsyncMock(
            return_value={"deals": [], "meta": {}}
        )
        result = await connector.list_deals()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_deal_unwraps_wrapper(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.get_deal = AsyncMock(
            return_value={"deal": SAMPLE_DEAL}
        )
        result = await connector.get_deal(201)
        assert result["id"] == 201
        assert result["amount"] == 50000.0

    @pytest.mark.asyncio
    async def test_get_deal_no_wrapper(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.get_deal = AsyncMock(return_value=SAMPLE_DEAL)
        result = await connector.get_deal(201)
        assert result["name"] == "Acme Enterprise License"

    @pytest.mark.asyncio
    async def test_get_deal_not_found(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.get_deal = AsyncMock(
            side_effect=FreshworksCRMNotFoundError("deal", "999")
        )
        with pytest.raises(FreshworksCRMNotFoundError):
            await connector.get_deal(999)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. LIST / GET ACCOUNTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestAccounts:
    @pytest.mark.asyncio
    async def test_list_accounts_returns_list(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.list_accounts = AsyncMock(
            return_value=SAMPLE_ACCOUNTS_RESPONSE
        )
        result = await connector.list_accounts(page=1)
        assert len(result) == 2
        assert result[0]["id"] == 301
        assert result[1]["name"] == "Startup Inc"

    @pytest.mark.asyncio
    async def test_list_accounts_empty(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.list_accounts = AsyncMock(
            return_value={"sales_accounts": [], "meta": {}}
        )
        result = await connector.list_accounts()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_account_unwraps_wrapper(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.get_account = AsyncMock(
            return_value={"sales_account": SAMPLE_ACCOUNT}
        )
        result = await connector.get_account(301)
        assert result["id"] == 301
        assert result["name"] == "Example Corp"

    @pytest.mark.asyncio
    async def test_get_account_no_wrapper(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.get_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
        result = await connector.get_account(301)
        assert result["website"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_get_account_not_found(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        connector._http_client.get_account = AsyncMock(
            side_effect=FreshworksCRMNotFoundError("account", "999")
        )
        with pytest.raises(FreshworksCRMNotFoundError):
            await connector.get_account(999)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. SYNC
# ═══════════════════════════════════════════════════════════════════════════════


class TestSync:
    def _mock_client(self) -> MagicMock:
        client = MagicMock()
        client.list_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_RESPONSE)
        client.list_deals = AsyncMock(return_value=SAMPLE_DEALS_RESPONSE)
        client.list_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)
        return client

    @pytest.mark.asyncio
    async def test_sync_full_completed(self) -> None:
        connector = make_connector()
        connector._http_client = self._mock_client()
        result = await connector.sync(full=True)
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 6  # 2 contacts + 2 deals + 2 accounts
        assert result.documents_synced == 6
        assert result.documents_failed == 0

    @pytest.mark.asyncio
    async def test_sync_no_kb_id_no_ingest(self) -> None:
        connector = make_connector()
        connector._http_client = self._mock_client()
        connector._ingest_document = AsyncMock()
        result = await connector.sync()
        connector._ingest_document.assert_not_called()
        assert result.documents_synced == 6

    @pytest.mark.asyncio
    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        connector = make_connector()
        connector._http_client = self._mock_client()
        connector._ingest_document = AsyncMock()
        result = await connector.sync(kb_id="kb_crm_001")
        assert connector._ingest_document.call_count == 6

    @pytest.mark.asyncio
    async def test_sync_empty_contacts(self) -> None:
        connector = make_connector()
        client = MagicMock()
        client.list_contacts = AsyncMock(return_value={"contacts": [], "meta": {}})
        client.list_deals = AsyncMock(return_value=SAMPLE_DEALS_RESPONSE)
        client.list_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)
        connector._http_client = client
        result = await connector.sync()
        assert result.documents_found == 4  # 0 contacts + 2 deals + 2 accounts
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_sync_contacts_api_error_returns_failed(self) -> None:
        connector = make_connector()
        client = MagicMock()
        client.list_contacts = AsyncMock(
            side_effect=FreshworksCRMNetworkError("5xx error")
        )
        connector._http_client = client
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_sync_deals_api_error_returns_partial(self) -> None:
        connector = make_connector()
        client = MagicMock()
        client.list_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_RESPONSE)
        client.list_deals = AsyncMock(
            side_effect=FreshworksCRMNetworkError("deals API error")
        )
        connector._http_client = client
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_found == 2  # contacts only

    @pytest.mark.asyncio
    async def test_sync_accounts_api_error_returns_partial(self) -> None:
        connector = make_connector()
        client = MagicMock()
        client.list_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_RESPONSE)
        client.list_deals = AsyncMock(return_value=SAMPLE_DEALS_RESPONSE)
        client.list_accounts = AsyncMock(
            side_effect=FreshworksCRMNetworkError("accounts API error")
        )
        connector._http_client = client
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_found == 4  # contacts + deals

    @pytest.mark.asyncio
    async def test_sync_partial_on_normalizer_failure(self) -> None:
        connector = make_connector()
        bad_contact: dict = {"id": None, "first_name": None, "last_name": None}
        client = MagicMock()
        client.list_contacts = AsyncMock(
            return_value={"contacts": [bad_contact], "meta": {"total_pages": 1}}
        )
        client.list_deals = AsyncMock(return_value={"deals": [], "meta": {}})
        client.list_accounts = AsyncMock(return_value={"sales_accounts": [], "meta": {}})
        connector._http_client = client

        # Patch normalize_contact to raise
        with patch("connector.normalize_contact", side_effect=ValueError("bad data")):
            result = await connector.sync()
        assert result.documents_failed == 1
        assert result.status == SyncStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_sync_multi_page_contacts(self) -> None:
        connector = make_connector()
        page1 = {
            "contacts": [SAMPLE_CONTACT],
            "meta": {"total_pages": 2, "current_page": 1},
        }
        page2 = {
            "contacts": [SAMPLE_CONTACT_2],
            "meta": {"total_pages": 2, "current_page": 2},
        }
        client = MagicMock()
        client.list_contacts = AsyncMock(side_effect=[page1, page2])
        client.list_deals = AsyncMock(return_value={"deals": [], "meta": {}})
        client.list_accounts = AsyncMock(return_value={"sales_accounts": [], "meta": {}})
        connector._http_client = client
        result = await connector.sync()
        assert result.documents_found == 2
        assert client.list_contacts.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_initialises_http_client_if_none(self) -> None:
        connector = make_connector()
        assert connector._http_client is None
        with patch("connector.FreshworksCRMHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.list_contacts = AsyncMock(return_value={"contacts": [], "meta": {}})
            instance.list_deals = AsyncMock(return_value={"deals": [], "meta": {}})
            instance.list_accounts = AsyncMock(return_value={"sales_accounts": [], "meta": {}})
            await connector.sync()
        assert MockClient.called


# ═══════════════════════════════════════════════════════════════════════════════
# 7. NORMALIZERS
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeContact:
    def test_basic_fields(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert doc.title == "Contact: Jane Doe"
        assert "Jane Doe" in doc.content
        assert "jane.doe@example.com" in doc.content
        assert "+1-555-1000" in doc.content
        assert "VP of Engineering" in doc.content
        assert "Example Corp" in doc.content

    def test_source_id_is_sha256_prefix(self) -> None:
        import hashlib
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        expected = hashlib.sha256(f"contact:{SAMPLE_CONTACT['id']}".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_source_url_contains_contact_id(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert "101" in doc.source_url
        assert "myfreshworks.com" in doc.source_url

    def test_connector_id_and_tenant_id(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_metadata_fields(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert doc.metadata["contact_id"] == 101
        assert doc.metadata["email"] == "jane.doe@example.com"

    def test_fallback_name_when_display_name_missing(self) -> None:
        contact = dict(SAMPLE_CONTACT)
        contact["display_name"] = ""
        contact["first_name"] = "Alice"
        contact["last_name"] = "Wonder"
        doc = normalize_contact(contact, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert "Alice Wonder" in doc.title

    def test_fallback_name_when_all_name_fields_empty(self) -> None:
        contact = dict(SAMPLE_CONTACT)
        contact["display_name"] = ""
        contact["first_name"] = ""
        contact["last_name"] = ""
        doc = normalize_contact(contact, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert f"Contact #{contact['id']}" in doc.title

    def test_mobile_used_when_work_number_empty(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT_2, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert "+44-7700-900456" in doc.content

    def test_linkedin_included_when_present(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert "linkedin.com/in/janedoe" in doc.content

    def test_stable_id_different_contacts(self) -> None:
        doc1 = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        doc2 = normalize_contact(SAMPLE_CONTACT_2, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert doc1.source_id != doc2.source_id


class TestNormalizeDeal:
    def test_basic_fields(self) -> None:
        doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert doc.title == "Deal: Acme Enterprise License"
        assert "50000" in doc.content
        assert "75" in doc.content  # probability
        assert "2026-09-30" in doc.content

    def test_source_id_is_sha256_prefix(self) -> None:
        import hashlib
        doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID, DOMAIN)
        expected = hashlib.sha256(f"deal:{SAMPLE_DEAL['id']}".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_source_url_contains_deal_id(self) -> None:
        doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert "201" in doc.source_url
        assert "myfreshworks.com" in doc.source_url

    def test_metadata_amount_and_stage(self) -> None:
        doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert doc.metadata["amount"] == 50000.0
        assert doc.metadata["stage_id"] == 5

    def test_fallback_title_when_name_empty(self) -> None:
        deal = dict(SAMPLE_DEAL)
        deal["name"] = ""
        doc = normalize_deal(deal, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert f"Deal #{deal['id']}" in doc.title

    def test_none_amount_not_in_content(self) -> None:
        deal = dict(SAMPLE_DEAL_2)
        deal["amount"] = None
        doc = normalize_deal(deal, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert "Amount:" not in doc.content

    def test_stable_id_different_deals(self) -> None:
        doc1 = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID, DOMAIN)
        doc2 = normalize_deal(SAMPLE_DEAL_2, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert doc1.source_id != doc2.source_id


class TestNormalizeAccount:
    def test_basic_fields(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert doc.title == "Account: Example Corp"
        assert "https://example.com" in doc.content
        assert "+1-800-555-0000" in doc.content
        assert "500" in doc.content  # employees

    def test_source_id_is_sha256_prefix(self) -> None:
        import hashlib
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        expected = hashlib.sha256(f"account:{SAMPLE_ACCOUNT['id']}".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_source_url_contains_account_id(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert "301" in doc.source_url
        assert "myfreshworks.com" in doc.source_url

    def test_annual_revenue_in_content(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert "10000000" in doc.content

    def test_none_revenue_not_in_content(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT_2, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert "Annual Revenue:" not in doc.content

    def test_fallback_name_when_empty(self) -> None:
        account = dict(SAMPLE_ACCOUNT)
        account["name"] = ""
        doc = normalize_account(account, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert f"Account #{account['id']}" in doc.title

    def test_stable_id_different_accounts(self) -> None:
        doc1 = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        doc2 = normalize_account(SAMPLE_ACCOUNT_2, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert doc1.source_id != doc2.source_id

    def test_metadata_includes_key_fields(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID, DOMAIN)
        assert doc.metadata["account_id"] == 301
        assert doc.metadata["website"] == "https://example.com"
        assert doc.metadata["number_of_employees"] == 500


# ═══════════════════════════════════════════════════════════════════════════════
# 8. RETRY HELPER
# ═══════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value="ok")
        result = await with_retry(fn)
        assert result == "ok"
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                FreshworksCRMNetworkError("timeout"),
                FreshworksCRMNetworkError("timeout"),
                "success",
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == "success"
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=FreshworksCRMAuthError("bad key", 401))
        with pytest.raises(FreshworksCRMAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_exhausts_attempts_and_raises(self) -> None:
        fn = AsyncMock(side_effect=FreshworksCRMNetworkError("5xx"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(FreshworksCRMNetworkError):
                await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_uses_retry_after(self) -> None:
        exc = FreshworksCRMRateLimitError("429 rate limit", retry_after=5.0)
        fn = AsyncMock(side_effect=[exc, "done"])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_retry(fn, max_attempts=3, base_delay=1.0)
        assert result == "done"
        mock_sleep.assert_called_once_with(5.0)

    @pytest.mark.asyncio
    async def test_rate_limit_backoff_when_no_retry_after(self) -> None:
        exc = FreshworksCRMRateLimitError("429", retry_after=0)
        fn = AsyncMock(side_effect=[exc, "done"])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_retry(fn, max_attempts=3, base_delay=1.0)
        assert result == "done"
        assert mock_sleep.call_count == 1
        delay = mock_sleep.call_args[0][0]
        assert delay >= 1.0  # at least base_delay


# ═══════════════════════════════════════════════════════════════════════════════
# 9. EXCEPTION HIERARCHY
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_base_error_attributes(self) -> None:
        from exceptions import FreshworksCRMError
        exc = FreshworksCRMError("something failed", status_code=500, code="server_error")
        assert exc.message == "something failed"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_auth_error_is_base(self) -> None:
        from exceptions import FreshworksCRMError
        exc = FreshworksCRMAuthError("bad key", 401)
        assert isinstance(exc, FreshworksCRMError)
        assert exc.status_code == 401

    def test_rate_limit_has_retry_after(self) -> None:
        exc = FreshworksCRMRateLimitError("too fast", retry_after=30.0)
        assert exc.retry_after == 30.0
        assert exc.status_code == 429

    def test_not_found_formats_message(self) -> None:
        exc = FreshworksCRMNotFoundError("deal", "201")
        assert "deal" in str(exc)
        assert "201" in str(exc)
        assert exc.status_code == 404

    def test_network_error_is_base(self) -> None:
        from exceptions import FreshworksCRMError
        exc = FreshworksCRMNetworkError("connection refused")
        assert isinstance(exc, FreshworksCRMError)


# ═══════════════════════════════════════════════════════════════════════════════
# 10. HTTP CLIENT — BASE URL BUILDER
# ═══════════════════════════════════════════════════════════════════════════════


class TestBaseURL:
    def test_bare_subdomain_expands(self) -> None:
        from client.http_client import _base_url
        url = _base_url("acme")
        assert url == "https://acme.myfreshworks.com/crm/sales/api/v2"

    def test_full_host_preserved(self) -> None:
        from client.http_client import _base_url
        url = _base_url("acme.myfreshworks.com")
        assert url == "https://acme.myfreshworks.com/crm/sales/api/v2"

    def test_https_prefix_preserved(self) -> None:
        from client.http_client import _base_url
        url = _base_url("https://acme.myfreshworks.com")
        assert url == "https://acme.myfreshworks.com/crm/sales/api/v2"

    def test_trailing_slash_stripped(self) -> None:
        from client.http_client import _base_url
        url = _base_url("acme/")
        assert not url.endswith("//")


# ═══════════════════════════════════════════════════════════════════════════════
# 11. LIFECYCLE / ASYNC CONTEXT MANAGER
# ═══════════════════════════════════════════════════════════════════════════════


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_clears_http_client(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        connector._http_client = mock_client
        await connector.aclose()
        mock_client.aclose.assert_awaited_once()
        assert connector._http_client is None

    @pytest.mark.asyncio
    async def test_aclose_no_op_when_client_is_none(self) -> None:
        connector = make_connector()
        assert connector._http_client is None
        await connector.aclose()  # should not raise

    @pytest.mark.asyncio
    async def test_async_context_manager(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        connector._http_client = mock_client
        async with connector as ctx:
            assert ctx is connector
        mock_client.aclose.assert_awaited_once()

    def test_connector_type_constant(self) -> None:
        assert FreshworksCRMConnector.CONNECTOR_TYPE == "freshworks_crm"

    def test_auth_type_constant(self) -> None:
        assert FreshworksCRMConnector.AUTH_TYPE == "api_key"

    def test_default_constructor(self) -> None:
        connector = FreshworksCRMConnector()
        assert connector._domain == ""
        assert connector._api_key == ""
        assert connector._tenant_id == ""
