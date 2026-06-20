"""Unit tests for NetSuiteConnector — all HTTP calls and OAuth signing are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import NetSuiteConnector
from exceptions import (
    NetSuiteAuthError,
    NetSuiteError,
    NetSuiteNetworkError,
    NetSuiteNotFoundError,
    NetSuiteRateLimitError,
    NetSuiteServerError,
)
from helpers.utils import (
    _stable_id,
    normalize_customer,
    normalize_invoice,
    normalize_item,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Constants ──────────────────────────────────────────────────────────────────

TENANT_ID = "tenant_test_ns"
CONNECTOR_ID = "conn_ns_test"
ACCOUNT_ID = "1234567"
CONSUMER_KEY = "consumer_key_abc123"
CONSUMER_SECRET = "consumer_secret_xyz789"
TOKEN_KEY = "token_key_def456"
TOKEN_SECRET = "token_secret_ghi012"

VALID_CONFIG = {
    "account_id": ACCOUNT_ID,
    "consumer_key": CONSUMER_KEY,
    "consumer_secret": CONSUMER_SECRET,
    "token_key": TOKEN_KEY,
    "token_secret": TOKEN_SECRET,
}

# ── Sample NetSuite REST payloads ──────────────────────────────────────────────

SAMPLE_CUSTOMER: dict = {
    "id": "42",
    "entityId": "CUST-42",
    "companyName": "Acme Corporation",
    "email": "billing@acme.com",
    "phone": "555-1234",
    "balance": 2500.00,
    "isInactive": False,
    "subsidiary": {"id": "1", "refName": "Parent Company"},
}

SAMPLE_INVOICE: dict = {
    "id": "100",
    "tranId": "INV-1001",
    "entity": {"id": "42", "refName": "Acme Corporation"},
    "tranDate": "2026-01-15",
    "dueDate": "2026-02-15",
    "total": 5000.00,
    "amountRemaining": 5000.00,
    "status": {"id": "A", "refName": "Open"},
    "subsidiary": {"id": "1", "refName": "Parent Company"},
}

SAMPLE_ITEM: dict = {
    "id": "200",
    "itemId": "ITEM-200",
    "displayName": "Professional Services",
    "description": "Consulting services billed per hour",
    "salesPrice": 150.00,
    "itemType": {"id": "SvcResale", "refName": "Service"},
    "isInactive": False,
}

NS_LIST_CUSTOMERS: dict = {
    "links": [],
    "count": 1,
    "hasMore": False,
    "offset": 0,
    "totalResults": 1,
    "items": [SAMPLE_CUSTOMER],
}

NS_LIST_INVOICES: dict = {
    "links": [],
    "count": 1,
    "hasMore": False,
    "offset": 0,
    "totalResults": 1,
    "items": [SAMPLE_INVOICE],
}

NS_LIST_ITEMS: dict = {
    "links": [],
    "count": 1,
    "hasMore": False,
    "offset": 0,
    "totalResults": 1,
    "items": [SAMPLE_ITEM],
}

NS_EMPTY_LIST: dict = {
    "links": [],
    "count": 0,
    "hasMore": False,
    "offset": 0,
    "totalResults": 0,
    "items": [],
}

NS_SUITEQL_RESULT: dict = {
    "links": [],
    "count": 2,
    "hasMore": False,
    "offset": 0,
    "totalResults": 2,
    "items": [
        {"id": "42", "companyname": "Acme Corporation"},
        {"id": "43", "companyname": "Globex Inc"},
    ],
}


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> NetSuiteConnector:
    """Connector with all credentials; http_client is a MagicMock."""
    c = NetSuiteConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=VALID_CONFIG,
    )
    c.http_client = MagicMock()
    return c


@pytest.fixture()
def no_creds() -> NetSuiteConnector:
    """Connector with no credentials at all."""
    return NetSuiteConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)


@pytest.fixture()
def partial_creds() -> NetSuiteConnector:
    """Connector missing token_secret."""
    return NetSuiteConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "account_id": ACCOUNT_ID,
            "consumer_key": CONSUMER_KEY,
            "consumer_secret": CONSUMER_SECRET,
            "token_key": TOKEN_KEY,
            # token_secret is missing
        },
    )


# ── install() ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_missing_all_credentials(no_creds: NetSuiteConnector) -> None:
    """install() with no credentials returns MISSING_CREDENTIALS."""
    result = await no_creds.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "account_id" in result.message


@pytest.mark.asyncio
async def test_install_partial_credentials(partial_creds: NetSuiteConnector) -> None:
    """install() with missing token_secret returns MISSING_CREDENTIALS."""
    result = await partial_creds.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "token_secret" in result.message


@pytest.mark.asyncio
async def test_install_success() -> None:
    """install() with valid credentials and a successful API call returns HEALTHY/CONNECTED."""
    connector = NetSuiteConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
    )
    with patch("connector.NetSuiteHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_customers = AsyncMock(return_value=NS_LIST_CUSTOMERS)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert ACCOUNT_ID in result.message


@pytest.mark.asyncio
async def test_install_auth_error() -> None:
    """install() with invalid credentials returns INVALID_CREDENTIALS."""
    connector = NetSuiteConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
    )
    with patch("connector.NetSuiteHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_customers = AsyncMock(
            side_effect=NetSuiteAuthError("Invalid consumer key", 401)
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_general_exception() -> None:
    """Any non-auth exception during install returns FAILED."""
    connector = NetSuiteConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG
    )
    with patch("connector.NetSuiteHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_customers = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_connector_id_from_account_id() -> None:
    """install() uses account_id as connector_id fallback when connector_id is empty."""
    connector = NetSuiteConnector(tenant_id=TENANT_ID, config=VALID_CONFIG)
    with patch("connector.NetSuiteHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_customers = AsyncMock(return_value=NS_LIST_CUSTOMERS)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.connector_id == ACCOUNT_ID


# ── health_check() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(authed: NetSuiteConnector) -> None:
    """health_check() returns HEALTHY/CONNECTED when the API responds."""
    with patch("connector.NetSuiteHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_customers = AsyncMock(return_value=NS_LIST_CUSTOMERS)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance  # type: ignore[method-assign]
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message.lower()


@pytest.mark.asyncio
async def test_health_check_missing_credentials(no_creds: NetSuiteConnector) -> None:
    """health_check() without credentials returns MISSING_CREDENTIALS."""
    result = await no_creds.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: NetSuiteConnector) -> None:
    """health_check() on 401 returns OFFLINE/INVALID_CREDENTIALS."""
    with patch("connector.NetSuiteHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_customers = AsyncMock(
            side_effect=NetSuiteAuthError("Token rejected", 401)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance  # type: ignore[method-assign]
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: NetSuiteConnector) -> None:
    """health_check() on network failure returns DEGRADED/FAILED."""
    with patch("connector.NetSuiteHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_customers = AsyncMock(
            side_effect=NetSuiteNetworkError("Connection timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance  # type: ignore[method-assign]
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(authed: NetSuiteConnector) -> None:
    """health_check() on unexpected error returns DEGRADED."""
    with patch("connector.NetSuiteHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_customers = AsyncMock(side_effect=Exception("unknown"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance  # type: ignore[method-assign]
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ── sync() ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_missing_credentials(no_creds: NetSuiteConnector) -> None:
    """sync() without credentials returns FAILED immediately."""
    result = await no_creds.sync()
    assert result.status == SyncStatus.FAILED
    assert "Missing required fields" in result.message


@pytest.mark.asyncio
async def test_sync_empty_results(authed: NetSuiteConnector) -> None:
    """sync() with empty API responses completes with zero documents."""
    authed.http_client.list_customers = AsyncMock(return_value=NS_EMPTY_LIST)
    authed.http_client.list_invoices = AsyncMock(return_value=NS_EMPTY_LIST)
    authed.http_client.list_items = AsyncMock(return_value=NS_EMPTY_LIST)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_data(authed: NetSuiteConnector) -> None:
    """sync() correctly counts documents across all three entity types."""
    authed.http_client.list_customers = AsyncMock(return_value=NS_LIST_CUSTOMERS)
    authed.http_client.list_invoices = AsyncMock(return_value=NS_LIST_INVOICES)
    authed.http_client.list_items = AsyncMock(return_value=NS_LIST_ITEMS)
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_customer_api_failure(authed: NetSuiteConnector) -> None:
    """If customer fetch fails, sync returns FAILED before touching invoices."""
    authed.http_client.list_customers = AsyncMock(
        side_effect=NetSuiteError("NS down", 503)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "Customer sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_invoice_api_failure(authed: NetSuiteConnector) -> None:
    """If invoice fetch fails after customers succeed, sync returns PARTIAL."""
    authed.http_client.list_customers = AsyncMock(return_value=NS_LIST_CUSTOMERS)
    authed.http_client.list_invoices = AsyncMock(
        side_effect=NetSuiteError("NS down", 503)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1  # customers succeeded


@pytest.mark.asyncio
async def test_sync_item_api_failure(authed: NetSuiteConnector) -> None:
    """If item fetch fails after customers and invoices succeed, sync returns PARTIAL."""
    authed.http_client.list_customers = AsyncMock(return_value=NS_LIST_CUSTOMERS)
    authed.http_client.list_invoices = AsyncMock(return_value=NS_LIST_INVOICES)
    authed.http_client.list_items = AsyncMock(
        side_effect=NetSuiteError("NS down", 503)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_partial_normalize_failure(authed: NetSuiteConnector) -> None:
    """A normalization exception for one doc increments documents_failed."""
    authed.http_client.list_customers = AsyncMock(
        return_value={"items": [{}, SAMPLE_CUSTOMER]}
    )
    authed.http_client.list_invoices = AsyncMock(return_value=NS_EMPTY_LIST)
    authed.http_client.list_items = AsyncMock(return_value=NS_EMPTY_LIST)

    with patch(
        "connector.normalize_customer",
        side_effect=[
            Exception("bad"),
            normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID),
        ],
    ):
        result = await authed.sync(full=True)

    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_multiple_entities_count(authed: NetSuiteConnector) -> None:
    """sync() correctly aggregates documents_found across entity types."""
    multi_customers = {
        "items": [SAMPLE_CUSTOMER, {**SAMPLE_CUSTOMER, "id": "43"}],
        "count": 2,
    }
    authed.http_client.list_customers = AsyncMock(return_value=multi_customers)
    authed.http_client.list_invoices = AsyncMock(return_value=NS_LIST_INVOICES)
    authed.http_client.list_items = AsyncMock(return_value=NS_LIST_ITEMS)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 4  # 2 customers + 1 invoice + 1 item
    assert result.documents_synced == 4


# ── list_customers() / get_customer() ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_customers(authed: NetSuiteConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value=NS_LIST_CUSTOMERS)
    result = await authed.list_customers(limit=50, offset=0)
    assert "items" in result
    authed.http_client.list_customers.assert_called_once_with(50, 0)


@pytest.mark.asyncio
async def test_list_customers_empty(authed: NetSuiteConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value=NS_EMPTY_LIST)
    result = await authed.list_customers()
    assert result["items"] == []
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_get_customer(authed: NetSuiteConnector) -> None:
    authed.http_client.get_customer = AsyncMock(return_value=SAMPLE_CUSTOMER)
    result = await authed.get_customer("42")
    assert result["id"] == "42"
    authed.http_client.get_customer.assert_called_once_with("42")


@pytest.mark.asyncio
async def test_get_customer_not_found(authed: NetSuiteConnector) -> None:
    authed.http_client.get_customer = AsyncMock(
        side_effect=NetSuiteNotFoundError("customer", "999")
    )
    with pytest.raises(NetSuiteNotFoundError):
        await authed.get_customer("999")


# ── list_invoices() / get_invoice() ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_invoices(authed: NetSuiteConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(return_value=NS_LIST_INVOICES)
    result = await authed.list_invoices(limit=100, offset=0)
    assert "items" in result
    authed.http_client.list_invoices.assert_called_once_with(100, 0)


@pytest.mark.asyncio
async def test_get_invoice(authed: NetSuiteConnector) -> None:
    authed.http_client.get_invoice = AsyncMock(return_value=SAMPLE_INVOICE)
    result = await authed.get_invoice("100")
    assert result["tranId"] == "INV-1001"
    authed.http_client.get_invoice.assert_called_once_with("100")


@pytest.mark.asyncio
async def test_get_invoice_not_found(authed: NetSuiteConnector) -> None:
    authed.http_client.get_invoice = AsyncMock(
        side_effect=NetSuiteNotFoundError("invoice", "999")
    )
    with pytest.raises(NetSuiteNotFoundError):
        await authed.get_invoice("999")


# ── list_items() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_items(authed: NetSuiteConnector) -> None:
    authed.http_client.list_items = AsyncMock(return_value=NS_LIST_ITEMS)
    result = await authed.list_items(limit=100, offset=0)
    assert "items" in result
    authed.http_client.list_items.assert_called_once_with(100, 0)


@pytest.mark.asyncio
async def test_list_items_with_offset(authed: NetSuiteConnector) -> None:
    authed.http_client.list_items = AsyncMock(return_value=NS_EMPTY_LIST)
    result = await authed.list_items(limit=50, offset=100)
    authed.http_client.list_items.assert_called_once_with(50, 100)
    assert result["items"] == []


# ── suiteql() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suiteql(authed: NetSuiteConnector) -> None:
    authed.http_client.suiteql = AsyncMock(return_value=NS_SUITEQL_RESULT)
    query = "SELECT id, companyName FROM customer WHERE isInactive = 'F'"
    result = await authed.suiteql(query, limit=100, offset=0)
    assert "items" in result
    assert result["count"] == 2
    authed.http_client.suiteql.assert_called_once_with(query, 100, 0)


@pytest.mark.asyncio
async def test_suiteql_default_limit(authed: NetSuiteConnector) -> None:
    authed.http_client.suiteql = AsyncMock(return_value=NS_SUITEQL_RESULT)
    await authed.suiteql("SELECT id FROM customer")
    authed.http_client.suiteql.assert_called_once_with(
        "SELECT id FROM customer", 1000, 0
    )


@pytest.mark.asyncio
async def test_suiteql_auth_error(authed: NetSuiteConnector) -> None:
    authed.http_client.suiteql = AsyncMock(
        side_effect=NetSuiteAuthError("Invalid signature", 401)
    )
    with pytest.raises(NetSuiteAuthError):
        await authed.suiteql("SELECT id FROM customer")


# ── normalize_customer() ───────────────────────────────────────────────────────


def test_normalize_customer_full() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("customer", "42")
    assert "Acme Corporation" in doc.title
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["email"] == "billing@acme.com"
    assert doc.metadata["balance"] == 2500.00
    assert doc.metadata["is_inactive"] is False
    assert doc.metadata["subsidiary"] == "Parent Company"
    assert doc.metadata["entity_type"] == "customer"


def test_normalize_customer_minimal() -> None:
    """normalize_customer handles a minimal record with only an id."""
    doc = normalize_customer({"id": "99"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("customer", "99")
    assert "99" in doc.title or "Customer" in doc.title
    assert doc.metadata["email"] == ""
    assert doc.metadata["phone"] == ""


def test_normalize_customer_inactive() -> None:
    raw = {**SAMPLE_CUSTOMER, "isInactive": True}
    doc = normalize_customer(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["is_inactive"] is True


def test_normalize_customer_no_email_no_phone() -> None:
    raw = {"id": "77", "companyName": "Silent Corp"}
    doc = normalize_customer(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == ""
    assert doc.metadata["phone"] == ""
    assert "Silent Corp" in doc.title


def test_normalize_customer_zero_balance() -> None:
    raw = {**SAMPLE_CUSTOMER, "balance": 0.0}
    doc = normalize_customer(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["balance"] == 0.0


def test_normalize_customer_no_subsidiary() -> None:
    raw = {"id": "88", "companyName": "Solo Co"}
    doc = normalize_customer(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["subsidiary"] == ""


def test_normalize_customer_entity_id_fallback() -> None:
    """normalize_customer uses entityId when companyName is absent."""
    raw = {"id": "55", "entityId": "ENT-55"}
    doc = normalize_customer(raw, CONNECTOR_ID, TENANT_ID)
    assert "ENT-55" in doc.title


# ── normalize_invoice() ────────────────────────────────────────────────────────


def test_normalize_invoice_full() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("invoice", "100")
    assert "INV-1001" in doc.title
    assert doc.metadata["total"] == 5000.00
    assert doc.metadata["customer_name"] == "Acme Corporation"
    assert doc.metadata["due_date"] == "2026-02-15"
    assert doc.metadata["status"] == "Open"
    assert doc.metadata["entity_type"] == "invoice"


def test_normalize_invoice_minimal() -> None:
    doc = normalize_invoice({"id": "5"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("invoice", "5")
    assert doc.metadata["total"] == 0
    assert doc.metadata["customer_name"] == ""
    assert doc.metadata["due_date"] == ""


def test_normalize_invoice_no_dates() -> None:
    raw = {"id": "201", "tranId": "INV-X"}
    doc = normalize_invoice(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["tran_date"] == ""
    assert doc.metadata["due_date"] == ""


def test_normalize_invoice_no_entity() -> None:
    raw = {"id": "202", "tranId": "INV-Y", "total": 0.0}
    doc = normalize_invoice(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["customer_name"] == ""


def test_normalize_invoice_status_refname() -> None:
    raw = {**SAMPLE_INVOICE, "status": {"id": "B", "refName": "Paid in Full"}}
    doc = normalize_invoice(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["status"] == "Paid in Full"


def test_normalize_invoice_subsidiary() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["subsidiary"] == "Parent Company"


# ── normalize_item() ───────────────────────────────────────────────────────────


def test_normalize_item_full() -> None:
    doc = normalize_item(SAMPLE_ITEM, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("item", "200")
    assert "Professional Services" in doc.title
    assert doc.metadata["sales_price"] == 150.00
    assert doc.metadata["item_type"] == "Service"
    assert doc.metadata["is_inactive"] is False
    assert doc.metadata["entity_type"] == "item"


def test_normalize_item_minimal() -> None:
    doc = normalize_item({"id": "10"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("item", "10")
    assert doc.metadata["sales_price"] == 0
    assert doc.metadata["description"] == ""


def test_normalize_item_inactive() -> None:
    raw = {**SAMPLE_ITEM, "isInactive": True}
    doc = normalize_item(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["is_inactive"] is True


def test_normalize_item_no_description() -> None:
    raw = {"id": "11", "displayName": "Widget"}
    doc = normalize_item(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["description"] == ""


# ── _stable_id ─────────────────────────────────────────────────────────────────


def test_stable_id_deterministic() -> None:
    id1 = _stable_id("customer", "42")
    id2 = _stable_id("customer", "42")
    assert id1 == id2
    assert len(id1) == 16


def test_stable_id_distinct_types() -> None:
    cust_id = _stable_id("customer", "1")
    inv_id = _stable_id("invoice", "1")
    item_id = _stable_id("item", "1")
    assert cust_id != inv_id
    assert inv_id != item_id
    assert cust_id != item_id


def test_stable_id_format() -> None:
    sid = _stable_id("invoice", "100")
    assert all(c in "0123456789abcdef" for c in sid)


# ── aclose / context manager ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client(no_creds: NetSuiteConnector) -> None:
    """aclose() with no http_client doesn't raise."""
    await no_creds.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_client(authed: NetSuiteConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    async with NetSuiteConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=VALID_CONFIG,
    ) as connector:
        assert isinstance(connector, NetSuiteConnector)


# ── CONNECTOR_TYPE constant ────────────────────────────────────────────────────


def test_connector_type() -> None:
    connector = NetSuiteConnector()
    assert connector.CONNECTOR_TYPE == "netsuite"


def test_auth_type() -> None:
    connector = NetSuiteConnector()
    assert connector.AUTH_TYPE == "oauth2"


# ── with_retry behaviour ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    """Auth errors surface immediately — no retry."""
    from helpers.utils import with_retry

    call_count = 0

    async def failing_fn() -> dict:
        nonlocal call_count
        call_count += 1
        raise NetSuiteAuthError("Bad signature", 401)

    with pytest.raises(NetSuiteAuthError):
        await with_retry(failing_fn)
    assert call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_network_error() -> None:
    """Network errors are retried up to max_attempts."""
    from helpers.utils import with_retry

    call_count = 0

    async def flaky_fn() -> dict:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise NetSuiteNetworkError("timeout")
        return {"ok": True}

    result = await with_retry(flaky_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert call_count == 3


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    """Raises the last exception when all attempts fail."""
    from helpers.utils import with_retry

    async def always_fails() -> dict:
        raise NetSuiteNetworkError("always down")

    with pytest.raises(NetSuiteNetworkError):
        await with_retry(always_fails, max_attempts=2, base_delay=0)


@pytest.mark.asyncio
async def test_with_retry_rate_limit_exhaustion() -> None:
    """Rate limit error is retried and re-raised after all attempts exhausted."""
    from helpers.utils import with_retry

    call_count = 0

    async def always_rate_limited() -> dict:
        nonlocal call_count
        call_count += 1
        raise NetSuiteRateLimitError("rate limited", retry_after=0.0)

    with pytest.raises(NetSuiteRateLimitError):
        await with_retry(always_rate_limited, max_attempts=2, base_delay=0)
    assert call_count == 2


@pytest.mark.asyncio
async def test_with_retry_rate_limit_succeeds_on_retry() -> None:
    """Rate limit on first attempt, success on second."""
    from helpers.utils import with_retry

    call_count = 0

    async def rate_limit_once() -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise NetSuiteRateLimitError("rate limited", retry_after=0.0)
        return {"ok": True}

    result = await with_retry(rate_limit_once, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert call_count == 2


@pytest.mark.asyncio
async def test_with_retry_network_error_exhaustion() -> None:
    """Network errors exhaust all attempts and re-raise."""
    from helpers.utils import with_retry

    call_count = 0

    async def always_network_error() -> dict:
        nonlocal call_count
        call_count += 1
        raise NetSuiteNetworkError("connection refused")

    with pytest.raises(NetSuiteNetworkError):
        await with_retry(always_network_error, max_attempts=3, base_delay=0)
    assert call_count == 3


# ── HTTP client error mapping ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_401() -> None:
    from client.http_client import NetSuiteHTTPClient

    client = NetSuiteHTTPClient(
        account_id=ACCOUNT_ID,
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        token_key=TOKEN_KEY,
        token_secret=TOKEN_SECRET,
    )
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"
    mock_response.content = b"Unauthorized"
    mock_response.json.return_value = {}
    mock_response.headers = {}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(NetSuiteAuthError) as exc_info:
            await client._request("GET", "https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/customer")
    assert exc_info.value.status_code == 401
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_403() -> None:
    from client.http_client import NetSuiteHTTPClient

    client = NetSuiteHTTPClient(
        account_id=ACCOUNT_ID,
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        token_key=TOKEN_KEY,
        token_secret=TOKEN_SECRET,
    )
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Forbidden"
    mock_response.content = b"Forbidden"
    mock_response.json.return_value = {}
    mock_response.headers = {}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(NetSuiteAuthError) as exc_info:
            await client._request("GET", "https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/customer")
    assert exc_info.value.status_code == 403
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_not_found_on_404() -> None:
    from client.http_client import NetSuiteHTTPClient

    client = NetSuiteHTTPClient(
        account_id=ACCOUNT_ID,
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        token_key=TOKEN_KEY,
        token_secret=TOKEN_SECRET,
    )
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_response.content = b"Not Found"
    mock_response.json.return_value = {}
    mock_response.headers = {}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(NetSuiteNotFoundError):
            await client._request("GET", "https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/customer/999")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_rate_limit_on_429() -> None:
    from client.http_client import NetSuiteHTTPClient

    client = NetSuiteHTTPClient(
        account_id=ACCOUNT_ID,
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        token_key=TOKEN_KEY,
        token_secret=TOKEN_SECRET,
    )
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = "Too Many Requests"
    mock_response.content = b"Too Many Requests"
    mock_response.json.return_value = {}
    mock_response.headers = {"Retry-After": "10"}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(NetSuiteRateLimitError) as exc_info:
            await client._request("GET", "https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/customer")
    assert exc_info.value.retry_after == 10.0
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_server_error_on_500() -> None:
    from client.http_client import NetSuiteHTTPClient

    client = NetSuiteHTTPClient(
        account_id=ACCOUNT_ID,
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        token_key=TOKEN_KEY,
        token_secret=TOKEN_SECRET,
    )
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_response.content = b"Internal Server Error"
    mock_response.json.return_value = {}
    mock_response.headers = {}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(NetSuiteServerError) as exc_info:
            await client._request("GET", "https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/customer")
    assert exc_info.value.status_code == 500
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_error_details_parsed() -> None:
    """o:errorDetails are parsed into the exception message."""
    from client.http_client import NetSuiteHTTPClient

    client = NetSuiteHTTPClient(
        account_id=ACCOUNT_ID,
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        token_key=TOKEN_KEY,
        token_secret=TOKEN_SECRET,
    )
    error_body = {
        "o:errorDetails": [
            {
                "detail": "Invalid consumer key",
                "o:errorCode": "INVALID_CONSUMER_KEY",
            }
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = ""
    mock_response.content = b'{"o:errorDetails": []}'
    mock_response.json.return_value = error_body
    mock_response.headers = {}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(NetSuiteAuthError) as exc_info:
            await client._request("GET", "https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/customer")
    assert "Invalid consumer key" in str(exc_info.value)
    await client.aclose()


# ── _build_base_url ────────────────────────────────────────────────────────────


def test_build_base_url_simple_account_id() -> None:
    from client.http_client import _build_base_url
    url = _build_base_url("1234567")
    assert url == "https://1234567.suitetalk.api.netsuite.com/services/rest"


def test_build_base_url_sandbox_account_id() -> None:
    from client.http_client import _build_base_url
    url = _build_base_url("1234567_SB1")
    assert url == "https://1234567-sb1.suitetalk.api.netsuite.com/services/rest"


def test_build_base_url_lowercase() -> None:
    from client.http_client import _build_base_url
    url = _build_base_url("TSTDRV1234567")
    assert "tstdrv1234567" in url


# ── OAuth 1.0a header generation (mocked signing) ─────────────────────────────


def test_oauth1_header_contains_required_params() -> None:
    """_build_oauth1_header returns a header with all required OAuth 1.0a params."""
    from client.http_client import _build_oauth1_header

    header = _build_oauth1_header(
        method="GET",
        url="https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/customer",
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        token_key=TOKEN_KEY,
        token_secret=TOKEN_SECRET,
    )
    assert "OAuth realm=" in header
    assert "oauth_consumer_key=" in header
    assert "oauth_token=" in header
    assert "oauth_signature_method=" in header
    assert "HMAC-SHA256" in header
    assert "oauth_timestamp=" in header
    assert "oauth_nonce=" in header
    assert "oauth_version=" in header
    assert "oauth_signature=" in header


def test_oauth1_header_nonce_unique() -> None:
    """Two calls to _build_oauth1_header produce different nonces."""
    from client.http_client import _build_oauth1_header

    url = "https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/customer"
    h1 = _build_oauth1_header("GET", url, CONSUMER_KEY, CONSUMER_SECRET, TOKEN_KEY, TOKEN_SECRET)
    h2 = _build_oauth1_header("GET", url, CONSUMER_KEY, CONSUMER_SECRET, TOKEN_KEY, TOKEN_SECRET)
    assert h1 != h2


def test_connector_missing_one_field_fails() -> None:
    """Missing any single required field causes MISSING_CREDENTIALS."""
    for field_name in ("account_id", "consumer_key", "consumer_secret", "token_key", "token_secret"):
        config = {**VALID_CONFIG}
        del config[field_name]
        c = NetSuiteConnector(config=config)
        assert field_name in c._missing_fields()
