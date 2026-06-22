"""Unit tests for XeroConnector — all Xero HTTP calls are mocked."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import XeroConnector
from exceptions import XeroAuthError, XeroNetworkError, XeroNotFoundError, XeroRateLimitError
from helpers.utils import normalize_account, normalize_contact, normalize_invoice, _stable_id
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_xero_test_001"
CLIENT_ID = "test-client-id-abc123"
CLIENT_SECRET = "test-client-secret-xyz789"
ACCESS_TOKEN = "fake-access-token-001"
XERO_TENANT_ID = "xero-tenant-uuid-001"

BASE_CONFIG = {
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "access_token": ACCESS_TOKEN,
    "xero_tenant_id": XERO_TENANT_ID,
    "redirect_uri": "https://app.shielva.ai/connectors/xero/callback",
}

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_INVOICE: dict = {
    "InvoiceID": "inv-uuid-001",
    "InvoiceNumber": "INV-0001",
    "Status": "AUTHORISED",
    "Contact": {"Name": "Acme Corp"},
    "Total": 1500.00,
    "CurrencyCode": "USD",
    "DateString": "2024-01-15",
    "DueDateString": "2024-02-15",
    "LineItems": [
        {
            "Description": "Consulting Services",
            "Quantity": 10.0,
            "UnitAmount": 150.0,
            "LineAmount": 1500.0,
        }
    ],
}

SAMPLE_CONTACT: dict = {
    "ContactID": "con-uuid-001",
    "Name": "Acme Corp",
    "EmailAddress": "billing@acme.com",
    "ContactStatus": "ACTIVE",
    "Phones": [
        {"PhoneType": "DEFAULT", "PhoneNumber": "+1-555-0100"},
    ],
    "Addresses": [
        {"AddressType": "STREET", "City": "New York", "Country": "US"},
    ],
}

SAMPLE_ACCOUNT: dict = {
    "AccountID": "acc-uuid-001",
    "Code": "200",
    "Name": "Sales",
    "Type": "REVENUE",
    "Status": "ACTIVE",
    "CurrencyCode": "USD",
    "Description": "Revenue from product sales",
}

SAMPLE_ORG: dict = {
    "Organisations": [
        {"OrganisationID": "org-uuid-001", "Name": "Shielva Demo Ltd"},
    ]
}

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> XeroConnector:
    c = XeroConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={**BASE_CONFIG},
    )
    c.http_client = MagicMock()
    return c


@pytest.fixture()
def no_token() -> XeroConnector:
    return XeroConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={**BASE_CONFIG})
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert result.connector_id == CONNECTOR_ID
    assert "OAuth2" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    c = XeroConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"client_secret": CLIENT_SECRET},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    c = XeroConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"client_id": CLIENT_ID},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_both_credentials() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════


def test_authorize_returns_url() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={**BASE_CONFIG})
    url = c.authorize()
    assert url.startswith("https://login.xero.com/identity/connect/authorize")
    assert "client_id=" in url
    assert "response_type=code" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "offline_access" in url


def test_authorize_stores_pkce_verifier() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={**BASE_CONFIG})
    c.authorize()
    assert "_pkce_code_verifier" in c.config
    assert len(c.config["_pkce_code_verifier"]) > 32


def test_authorize_missing_client_id_raises() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    with pytest.raises(XeroAuthError, match="client_id"):
        c.authorize()


def test_authorize_includes_scopes() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={**BASE_CONFIG})
    url = c.authorize()
    assert "accounting.transactions" in url
    assert "accounting.contacts" in url


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(authed: XeroConnector) -> None:
    with patch("connector.XeroHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_organisation = AsyncMock(return_value=SAMPLE_ORG)
        instance.aclose = AsyncMock()
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Shielva Demo Ltd" in result.message
    assert result.organisation_name == "Shielva Demo Ltd"


@pytest.mark.asyncio
async def test_health_check_no_token(no_token: XeroConnector) -> None:
    result = await no_token.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: XeroConnector) -> None:
    with patch("connector.XeroHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_organisation = AsyncMock(
            side_effect=XeroAuthError("Token expired", 401)
        )
        instance.aclose = AsyncMock()
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.EXPIRED


@pytest.mark.asyncio
async def test_health_check_network_error(authed: XeroConnector) -> None:
    with patch("connector.XeroHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_organisation = AsyncMock(
            side_effect=XeroNetworkError("Connection refused")
        )
        instance.aclose = AsyncMock()
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_empty_org(authed: XeroConnector) -> None:
    with patch("connector.XeroHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_organisation = AsyncMock(return_value={"Organisations": []})
        instance.aclose = AsyncMock()
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.organisation_name == ""


# ═══════════════════════════════════════════════════════════════════════════
# list_invoices() / get_invoice()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_invoices(authed: XeroConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(
        return_value={"Invoices": [SAMPLE_INVOICE]}
    )
    result = await authed.list_invoices()
    assert result["Invoices"][0]["InvoiceID"] == "inv-uuid-001"
    authed.http_client.list_invoices.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_invoices_with_modified_after(authed: XeroConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(return_value={"Invoices": []})
    await authed.list_invoices(modified_after="Mon, 01 Jan 2024 00:00:00 GMT")
    authed.http_client.list_invoices.assert_awaited_once_with(
        modified_after="Mon, 01 Jan 2024 00:00:00 GMT"
    )


@pytest.mark.asyncio
async def test_get_invoice(authed: XeroConnector) -> None:
    authed.http_client.get_invoice = AsyncMock(
        return_value={"Invoices": [SAMPLE_INVOICE]}
    )
    result = await authed.get_invoice("inv-uuid-001")
    authed.http_client.get_invoice.assert_awaited_once_with("inv-uuid-001")
    assert "Invoices" in result


@pytest.mark.asyncio
async def test_get_invoice_not_found(authed: XeroConnector) -> None:
    authed.http_client.get_invoice = AsyncMock(
        side_effect=XeroNotFoundError("Invoice", "nonexistent-id")
    )
    with pytest.raises(XeroNotFoundError):
        await authed.get_invoice("nonexistent-id")


# ═══════════════════════════════════════════════════════════════════════════
# list_contacts() / get_contact()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_contacts(authed: XeroConnector) -> None:
    authed.http_client.list_contacts = AsyncMock(
        return_value={"Contacts": [SAMPLE_CONTACT]}
    )
    result = await authed.list_contacts()
    assert result["Contacts"][0]["ContactID"] == "con-uuid-001"


@pytest.mark.asyncio
async def test_list_contacts_with_modified_after(authed: XeroConnector) -> None:
    authed.http_client.list_contacts = AsyncMock(return_value={"Contacts": []})
    await authed.list_contacts(modified_after="Mon, 01 Jan 2024 00:00:00 GMT")
    authed.http_client.list_contacts.assert_awaited_once_with(
        modified_after="Mon, 01 Jan 2024 00:00:00 GMT"
    )


@pytest.mark.asyncio
async def test_get_contact(authed: XeroConnector) -> None:
    authed.http_client.get_contact = AsyncMock(
        return_value={"Contacts": [SAMPLE_CONTACT]}
    )
    result = await authed.get_contact("con-uuid-001")
    authed.http_client.get_contact.assert_awaited_once_with("con-uuid-001")
    assert "Contacts" in result


@pytest.mark.asyncio
async def test_get_contact_not_found(authed: XeroConnector) -> None:
    authed.http_client.get_contact = AsyncMock(
        side_effect=XeroNotFoundError("Contact", "bad-id")
    )
    with pytest.raises(XeroNotFoundError):
        await authed.get_contact("bad-id")


# ═══════════════════════════════════════════════════════════════════════════
# list_accounts()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_accounts(authed: XeroConnector) -> None:
    authed.http_client.list_accounts = AsyncMock(
        return_value={"Accounts": [SAMPLE_ACCOUNT]}
    )
    result = await authed.list_accounts()
    assert result["Accounts"][0]["AccountID"] == "acc-uuid-001"
    authed.http_client.list_accounts.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_full(authed: XeroConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(return_value={"Invoices": [SAMPLE_INVOICE]})
    authed.http_client.list_contacts = AsyncMock(return_value={"Contacts": [SAMPLE_CONTACT]})
    authed.http_client.list_accounts = AsyncMock(return_value={"Accounts": [SAMPLE_ACCOUNT]})

    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_incremental(authed: XeroConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(return_value={"Invoices": [SAMPLE_INVOICE]})
    authed.http_client.list_contacts = AsyncMock(return_value={"Contacts": [SAMPLE_CONTACT]})
    authed.http_client.list_accounts = AsyncMock(return_value={"Accounts": []})

    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = await authed.sync(full=False, since=since)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    # Accounts are skipped in incremental mode when since is provided and full=False
    authed.http_client.list_accounts.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_empty_results(authed: XeroConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(return_value={"Invoices": []})
    authed.http_client.list_contacts = AsyncMock(return_value={"Contacts": []})
    authed.http_client.list_accounts = AsyncMock(return_value={"Accounts": []})

    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_invoice_failure(authed: XeroConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(
        side_effect=XeroAuthError("Unauthorized", 401)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "Invoice sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_contact_failure(authed: XeroConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(return_value={"Invoices": []})
    authed.http_client.list_contacts = AsyncMock(
        side_effect=XeroNetworkError("Timeout")
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert "Contact sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_pagination(authed: XeroConnector) -> None:
    """Verify that pagination continues until a partial page is returned."""
    full_page = [SAMPLE_INVOICE] * 100
    partial_page = [SAMPLE_INVOICE] * 5

    authed.http_client.list_invoices = AsyncMock(
        side_effect=[
            {"Invoices": full_page},
            {"Invoices": partial_page},
        ]
    )
    authed.http_client.list_contacts = AsyncMock(return_value={"Contacts": []})
    authed.http_client.list_accounts = AsyncMock(return_value={"Accounts": []})

    result = await authed.sync(full=True)
    assert result.documents_found >= 105
    assert authed.http_client.list_invoices.await_count == 2


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(authed: XeroConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(return_value={"Invoices": [SAMPLE_INVOICE]})
    authed.http_client.list_contacts = AsyncMock(return_value={"Contacts": []})
    authed.http_client.list_accounts = AsyncMock(return_value={"Accounts": []})

    ingest_calls = []

    async def mock_ingest(doc, kb_id):
        ingest_calls.append((doc, kb_id))

    authed._ingest_document = mock_ingest
    await authed.sync(full=True, kb_id="kb-test-001")
    assert len(ingest_calls) == 1
    assert ingest_calls[0][1] == "kb-test-001"


# ═══════════════════════════════════════════════════════════════════════════
# normalize_invoice()
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_invoice_fields() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("invoice", "inv-uuid-001")
    assert "INV-0001" in doc.title
    assert "Acme Corp" in doc.title
    assert "AUTHORISED" in doc.content
    assert "1500" in doc.content
    assert "Consulting Services" in doc.content
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "inv-uuid-001" in doc.source_url


def test_normalize_invoice_missing_optional_fields() -> None:
    raw = {"InvoiceID": "inv-minimal-001", "Status": "DRAFT"}
    doc = normalize_invoice(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("invoice", "inv-minimal-001")
    assert "DRAFT" in doc.content


def test_normalize_invoice_metadata() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["entity_type"] == "invoice"
    assert doc.metadata["xero_id"] == "inv-uuid-001"
    assert doc.metadata["status"] == "AUTHORISED"
    assert doc.metadata["currency"] == "USD"


# ═══════════════════════════════════════════════════════════════════════════
# normalize_contact()
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_contact_fields() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("contact", "con-uuid-001")
    assert "Acme Corp" in doc.title
    assert "billing@acme.com" in doc.content
    assert "ACTIVE" in doc.content
    assert "+1-555-0100" in doc.content
    assert "New York" in doc.content
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "con-uuid-001" in doc.source_url


def test_normalize_contact_no_phones() -> None:
    raw = {**SAMPLE_CONTACT, "Phones": []}
    doc = normalize_contact(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["phone"] == ""


def test_normalize_contact_metadata() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["entity_type"] == "contact"
    assert doc.metadata["name"] == "Acme Corp"
    assert doc.metadata["email"] == "billing@acme.com"


# ═══════════════════════════════════════════════════════════════════════════
# normalize_account()
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_account_fields() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("account", "acc-uuid-001")
    assert "200" in doc.title
    assert "Sales" in doc.title
    assert "REVENUE" in doc.content
    assert "Revenue from product sales" in doc.content
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_account_metadata() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["entity_type"] == "account"
    assert doc.metadata["code"] == "200"
    assert doc.metadata["type"] == "REVENUE"


# ═══════════════════════════════════════════════════════════════════════════
# _stable_id()
# ═══════════════════════════════════════════════════════════════════════════


def test_stable_id_length() -> None:
    sid = _stable_id("invoice", "some-uuid-123")
    assert len(sid) == 16


def test_stable_id_deterministic() -> None:
    a = _stable_id("contact", "con-abc-001")
    b = _stable_id("contact", "con-abc-001")
    assert a == b


def test_stable_id_different_types() -> None:
    inv = _stable_id("invoice", "shared-uuid")
    con = _stable_id("contact", "shared-uuid")
    assert inv != con


# ═══════════════════════════════════════════════════════════════════════════
# Exception classes
# ═══════════════════════════════════════════════════════════════════════════


def test_xero_error_base() -> None:
    from exceptions import XeroError
    exc = XeroError("base error", status_code=500, code="server_error")
    assert str(exc) == "base error"
    assert exc.status_code == 500
    assert exc.code == "server_error"


def test_xero_auth_error() -> None:
    exc = XeroAuthError("Unauthorized", 401, "unauthorized")
    assert exc.status_code == 401
    assert isinstance(exc, XeroAuthError)


def test_xero_rate_limit_error_default_retry() -> None:
    exc = XeroRateLimitError("Too many requests")
    assert exc.retry_after == 60.0
    assert exc.status_code == 429


def test_xero_rate_limit_error_custom_retry() -> None:
    exc = XeroRateLimitError("Too many requests", retry_after=30.0)
    assert exc.retry_after == 30.0


def test_xero_not_found_error() -> None:
    exc = XeroNotFoundError("Invoice", "inv-999")
    assert "inv-999" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "not_found"


def test_xero_network_error() -> None:
    exc = XeroNetworkError("Timeout")
    assert str(exc) == "Timeout"


# ═══════════════════════════════════════════════════════════════════════════
# XeroConnector defaults / lifecycle
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_defaults() -> None:
    c = XeroConnector()
    assert c.CONNECTOR_TYPE == "xero"
    assert c.AUTH_TYPE == "oauth2"
    assert c._tenant_id == ""
    assert c.connector_id == ""
    assert c.http_client is None


def test_connector_config_extraction() -> None:
    c = XeroConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={**BASE_CONFIG},
    )
    assert c._client_id == CLIENT_ID
    assert c._client_secret == CLIENT_SECRET
    assert c._access_token == ACCESS_TOKEN
    assert c._xero_tenant_id == XERO_TENANT_ID


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    await c.aclose()  # should not raise


@pytest.mark.asyncio
async def test_aclose_closes_http_client() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={**BASE_CONFIG})
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    await c.aclose()
    mock_client.aclose.assert_awaited_once()
    assert c.http_client is None


@pytest.mark.asyncio
async def test_async_context_manager() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={**BASE_CONFIG})
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    async with c as ctx:
        assert ctx is c
    mock_client.aclose.assert_awaited_once()


def test_ensure_client_creates_client() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={**BASE_CONFIG})
    assert c.http_client is None
    client = c._ensure_client()
    assert client is not None
    assert c.http_client is client


def test_ensure_client_returns_existing() -> None:
    c = XeroConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={**BASE_CONFIG})
    mock = MagicMock()
    c.http_client = mock
    result = c._ensure_client()
    assert result is mock


# ═══════════════════════════════════════════════════════════════════════════
# _stable_id() — additional coverage
# ═══════════════════════════════════════════════════════════════════════════


def test_stable_id_hex_only() -> None:
    """Output must be a valid lowercase hex string (digits 0-9 and a-f only)."""
    sid = _stable_id("invoice", "any-id-99")
    assert all(c in "0123456789abcdef" for c in sid), f"Non-hex chars found in {sid!r}"


def test_stable_id_different_ids_differ() -> None:
    """Two different xero_ids with the same entity_type must produce different stable IDs."""
    a = _stable_id("invoice", "inv-001")
    b = _stable_id("invoice", "inv-002")
    assert a != b


# ═══════════════════════════════════════════════════════════════════════════
# normalize_account() — additional coverage
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_account_source_url_empty() -> None:
    """Accounts have no dedicated deep-link; source_url should be empty."""
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == ""


def test_normalize_account_minimal_fields() -> None:
    """normalize_account must not raise when optional fields are absent."""
    raw = {"AccountID": "acc-minimal-001", "Type": "EXPENSE"}
    doc = normalize_account(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("account", "acc-minimal-001")
    assert "EXPENSE" in doc.content


# ═══════════════════════════════════════════════════════════════════════════
# with_retry() — exhaustion and auth-error short-circuit
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    """with_retry must re-raise after max_attempts without sleeping indefinitely."""
    from helpers.utils import with_retry
    calls: list[int] = []

    async def flaky() -> None:
        calls.append(1)
        raise XeroNetworkError("always fails")

    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(XeroNetworkError):
            await with_retry(flaky, max_attempts=3)

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_with_retry_skips_retry_on_auth_error() -> None:
    """XeroAuthError must propagate immediately — no retry attempts."""
    from helpers.utils import with_retry
    calls: list[int] = []

    async def auth_fail() -> None:
        calls.append(1)
        raise XeroAuthError("Token invalid", 401)

    with pytest.raises(XeroAuthError):
        await with_retry(auth_fail, max_attempts=3)

    assert len(calls) == 1, "Auth error must not be retried"


# ═══════════════════════════════════════════════════════════════════════════
# list_accounts() — network error propagation
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_accounts_network_error(authed: XeroConnector) -> None:
    authed.http_client.list_accounts = AsyncMock(
        side_effect=XeroNetworkError("Connection refused")
    )
    with pytest.raises(XeroNetworkError):
        await authed.list_accounts()


# ═══════════════════════════════════════════════════════════════════════════
# sync() — partial failure: accounts fail, invoices+contacts succeed
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_account_failure_partial(authed: XeroConnector) -> None:
    """When accounts fail but invoices+contacts succeed, status is PARTIAL."""
    authed.http_client.list_invoices = AsyncMock(return_value={"Invoices": [SAMPLE_INVOICE]})
    authed.http_client.list_contacts = AsyncMock(return_value={"Contacts": [SAMPLE_CONTACT]})
    authed.http_client.list_accounts = AsyncMock(
        side_effect=XeroNetworkError("Accounts endpoint down")
    )

    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert "Account sync failed" in result.message
    # Invoices and contacts were synced before the failure
    assert result.documents_synced >= 2


# ═══════════════════════════════════════════════════════════════════════════
# XeroHTTPClient._raise_for_status — 429 and 500 via _request
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_raises_rate_limit_on_429() -> None:
    """A 429 response from the HTTP layer must raise XeroRateLimitError."""
    from client.http_client import XeroHTTPClient

    client = XeroHTTPClient(access_token=ACCESS_TOKEN, xero_tenant_id=XERO_TENANT_ID)
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {"Retry-After": "45"}
    mock_response.json.return_value = {"Detail": "Rate limit exceeded"}
    mock_response.text = "Rate limit exceeded"

    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(XeroRateLimitError) as exc_info:
            await client._request("GET", "/Invoices")

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 45.0
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_xero_error_on_500() -> None:
    """A 500 response must raise a generic XeroError with the correct status code."""
    from client.http_client import XeroHTTPClient
    from exceptions import XeroError as _XeroError

    client = XeroHTTPClient(access_token=ACCESS_TOKEN, xero_tenant_id=XERO_TENANT_ID)
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.headers = {}
    mock_response.json.return_value = {"Message": "Internal Server Error"}
    mock_response.text = "Internal Server Error"

    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(_XeroError) as exc_info:
            await client._request("GET", "/Invoices")

    assert exc_info.value.status_code == 500
    await client.aclose()


# ═══════════════════════════════════════════════════════════════════════════
# XeroConnector.get_connections()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_connections_returns_tenants(authed: XeroConnector) -> None:
    """get_connections() should return a dict with a 'connections' list."""
    connections_payload = [
        {"tenantId": XERO_TENANT_ID, "tenantName": "Acme Ltd", "tenantType": "ORGANISATION"},
    ]
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = connections_payload

    import httpx
    with patch("connector.httpx.AsyncClient") as MockHttpx:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_ctx.get = AsyncMock(return_value=mock_response)
        MockHttpx.return_value = mock_ctx

        result = await authed.get_connections()

    assert "connections" in result
    assert len(result["connections"]) == 1
    assert result["connections"][0]["tenantId"] == XERO_TENANT_ID


@pytest.mark.asyncio
async def test_get_connections_no_token_raises(no_token: XeroConnector) -> None:
    """get_connections() must raise XeroAuthError when there is no access token."""
    with pytest.raises(XeroAuthError, match="No access token"):
        await no_token.get_connections()


@pytest.mark.asyncio
async def test_get_connections_auth_error_propagates(authed: XeroConnector) -> None:
    """A 401 from /connections must raise XeroAuthError."""
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.json.return_value = {"Detail": "Invalid token"}
    mock_response.text = "Invalid token"

    with patch("connector.httpx.AsyncClient") as MockHttpx:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_ctx.get = AsyncMock(return_value=mock_response)
        MockHttpx.return_value = mock_ctx

        with pytest.raises(XeroAuthError):
            await authed.get_connections()


@pytest.mark.asyncio
async def test_get_connections_empty_list(authed: XeroConnector) -> None:
    """get_connections() with an empty list is valid — no tenants connected yet."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = []

    with patch("connector.httpx.AsyncClient") as MockHttpx:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_ctx.get = AsyncMock(return_value=mock_response)
        MockHttpx.return_value = mock_ctx

        result = await authed.get_connections()

    assert result == {"connections": []}
