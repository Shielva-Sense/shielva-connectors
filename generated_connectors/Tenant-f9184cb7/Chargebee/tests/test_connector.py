"""Unit tests for ChargebeeConnector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import ChargebeeConnector
from exceptions import (
    ChargebeeAuthError,
    ChargebeeError,
    ChargebeeNetworkError,
    ChargebeeNotFoundError,
    ChargebeeRateLimitError,
)
from helpers.utils import (
    normalize_customer,
    normalize_invoice,
    normalize_subscription,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_chargebee_test_001"
SITE = "acme"
API_KEY = "test_chargebee_api_key_xyz"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_SUBSCRIPTION_RAW: dict = {
    "id": "sub_ABC123",
    "plan_id": "professional-monthly",
    "status": "active",
    "customer_id": "cus_CUST001",
    "currency_code": "USD",
    "mrr": 9900,
    "plan_amount": 9900,
    "billing_period": 1,
    "billing_period_unit": "month",
    "current_term_start": 1717200000,
    "current_term_end": 1719792000,
    "created_at": 1709510400,
    "updated_at": 1717200000,
}

# Chargebee wraps each item in the list response
SAMPLE_SUBSCRIPTION_ITEM: dict = {"subscription": SAMPLE_SUBSCRIPTION_RAW}

SAMPLE_SUBSCRIPTION_RAW_2: dict = {
    "id": "sub_DEF456",
    "plan_id": "starter-monthly",
    "status": "cancelled",
    "customer_id": "cus_CUST002",
    "currency_code": "USD",
    "mrr": 0,
    "plan_amount": 2900,
    "billing_period": 1,
    "billing_period_unit": "month",
    "current_term_start": 1714521600,
    "current_term_end": 1717200000,
    "created_at": 1706745600,
    "updated_at": 1717200000,
}

SAMPLE_SUBSCRIPTION_ITEM_2: dict = {"subscription": SAMPLE_SUBSCRIPTION_RAW_2}

SAMPLE_CUSTOMER_RAW: dict = {
    "id": "cus_CUST001",
    "first_name": "Alice",
    "last_name": "Smith",
    "email": "alice@example.com",
    "company": "Acme Corp",
    "phone": "+1-555-0101",
    "taxability": "taxable",
    "auto_collection": "on",
    "created_at": 1709510400,
    "updated_at": 1717200000,
}

SAMPLE_CUSTOMER_ITEM: dict = {"customer": SAMPLE_CUSTOMER_RAW}

SAMPLE_CUSTOMER_RAW_2: dict = {
    "id": "cus_CUST002",
    "first_name": "Bob",
    "last_name": "Jones",
    "email": "bob@example.com",
    "company": "",
    "phone": "",
    "taxability": "exempt",
    "auto_collection": "off",
    "created_at": 1706745600,
    "updated_at": 1717200000,
}

SAMPLE_CUSTOMER_ITEM_2: dict = {"customer": SAMPLE_CUSTOMER_RAW_2}

SAMPLE_INVOICE_RAW: dict = {
    "id": "inv_INV001",
    "customer_id": "cus_CUST001",
    "subscription_id": "sub_ABC123",
    "status": "paid",
    "total": 9900,
    "amount_due": 0,
    "amount_paid": 9900,
    "currency_code": "USD",
    "date": 1717200000,
    "due_date": 1717286400,
    "paid_at": 1717200001,
}

SAMPLE_INVOICE_ITEM: dict = {"invoice": SAMPLE_INVOICE_RAW}

SAMPLE_INVOICE_RAW_2: dict = {
    "id": "inv_INV002",
    "customer_id": "cus_CUST002",
    "subscription_id": "sub_DEF456",
    "status": "voided",
    "total": 2900,
    "amount_due": 2900,
    "amount_paid": 0,
    "currency_code": "USD",
    "date": 1714521600,
    "due_date": None,
    "paid_at": None,
}

SAMPLE_INVOICE_ITEM_2: dict = {"invoice": SAMPLE_INVOICE_RAW_2}

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_page(items: list, next_offset: str | None = None) -> dict:
    """Build a Chargebee-style list response."""
    result: dict = {"list": items}
    if next_offset:
        result["next_offset"] = next_offset
    return result


# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_connector(site: str = SITE, api_key: str = API_KEY) -> ChargebeeConnector:
    return ChargebeeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"site": site, "api_key": api_key},
    )


@pytest.fixture()
def connector() -> ChargebeeConnector:
    c = make_connector()
    c._http_client = MagicMock()
    return c


# ── install() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = make_connector()
    with patch("connector.ChargebeeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_subscriptions = AsyncMock(
            return_value=make_page([SAMPLE_SUBSCRIPTION_ITEM])
        )
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "acme" in result.message


@pytest.mark.asyncio
async def test_install_missing_site() -> None:
    c = make_connector(site="", api_key=API_KEY)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "site" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = make_connector(site=SITE, api_key="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_missing_both() -> None:
    c = make_connector(site="", api_key="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "site" in result.message
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    c = make_connector()
    with patch("connector.ChargebeeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_subscriptions = AsyncMock(
            side_effect=ChargebeeAuthError("Unauthorized", 401)
        )
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = make_connector()
    with patch("connector.ChargebeeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_subscriptions = AsyncMock(
            side_effect=ChargebeeNetworkError("Connection refused")
        )
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unknown_exception() -> None:
    c = make_connector()
    with patch("connector.ChargebeeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.list_subscriptions = AsyncMock(side_effect=Exception("boom"))
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: ChargebeeConnector) -> None:
    connector._make_client = lambda: MagicMock(
        list_subscriptions=AsyncMock(return_value=make_page([SAMPLE_SUBSCRIPTION_ITEM])),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_key(connector: ChargebeeConnector) -> None:
    connector._make_client = lambda: MagicMock(
        list_subscriptions=AsyncMock(
            side_effect=ChargebeeAuthError("Invalid API key", 401)
        ),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: ChargebeeConnector) -> None:
    connector._make_client = lambda: MagicMock(
        list_subscriptions=AsyncMock(side_effect=ChargebeeNetworkError("timeout")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: ChargebeeConnector) -> None:
    connector._make_client = lambda: MagicMock(
        list_subscriptions=AsyncMock(side_effect=Exception("unexpected")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_missing_creds() -> None:
    c = make_connector(site="", api_key="")
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(return_value=make_page([]))
    connector._http_client.list_customers = AsyncMock(return_value=make_page([]))
    connector._http_client.list_invoices = AsyncMock(return_value=make_page([]))
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_subscriptions_one_page(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(
        side_effect=[
            make_page([SAMPLE_SUBSCRIPTION_ITEM, SAMPLE_SUBSCRIPTION_ITEM_2]),
            make_page([]),
        ]
    )
    connector._http_client.list_customers = AsyncMock(return_value=make_page([]))
    connector._http_client.list_invoices = AsyncMock(return_value=make_page([]))
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_subscriptions_pagination_with_offset(connector: ChargebeeConnector) -> None:
    """next_offset must trigger a second page fetch."""
    connector._http_client.list_subscriptions = AsyncMock(
        side_effect=[
            make_page([SAMPLE_SUBSCRIPTION_ITEM], next_offset="offset_page2"),
            make_page([SAMPLE_SUBSCRIPTION_ITEM_2]),
        ]
    )
    connector._http_client.list_customers = AsyncMock(return_value=make_page([]))
    connector._http_client.list_invoices = AsyncMock(return_value=make_page([]))
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert connector._http_client.list_subscriptions.call_count == 2


@pytest.mark.asyncio
async def test_sync_customers_one_page(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(return_value=make_page([]))
    connector._http_client.list_customers = AsyncMock(
        side_effect=[
            make_page([SAMPLE_CUSTOMER_ITEM, SAMPLE_CUSTOMER_ITEM_2]),
            make_page([]),
        ]
    )
    connector._http_client.list_invoices = AsyncMock(return_value=make_page([]))
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_invoices_one_page(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(return_value=make_page([]))
    connector._http_client.list_customers = AsyncMock(return_value=make_page([]))
    connector._http_client.list_invoices = AsyncMock(
        side_effect=[
            make_page([SAMPLE_INVOICE_ITEM, SAMPLE_INVOICE_ITEM_2]),
            make_page([]),
        ]
    )
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_all_resources(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(
        side_effect=[make_page([SAMPLE_SUBSCRIPTION_ITEM]), make_page([])]
    )
    connector._http_client.list_customers = AsyncMock(
        side_effect=[make_page([SAMPLE_CUSTOMER_ITEM]), make_page([])]
    )
    connector._http_client.list_invoices = AsyncMock(
        side_effect=[make_page([SAMPLE_INVOICE_ITEM]), make_page([])]
    )
    result = await connector.sync(full=True)
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_subscription_api_failure(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(
        side_effect=ChargebeeNetworkError("Server error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_customer_api_failure(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(return_value=make_page([]))
    connector._http_client.list_customers = AsyncMock(
        side_effect=ChargebeeNetworkError("Server error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_invoice_api_failure(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(return_value=make_page([]))
    connector._http_client.list_customers = AsyncMock(return_value=make_page([]))
    connector._http_client.list_invoices = AsyncMock(
        side_effect=ChargebeeNetworkError("Server error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_partial_normalizer_failure(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(
        side_effect=[make_page([SAMPLE_SUBSCRIPTION_ITEM]), make_page([])]
    )
    connector._http_client.list_customers = AsyncMock(return_value=make_page([]))
    connector._http_client.list_invoices = AsyncMock(return_value=make_page([]))
    with patch("connector.normalize_subscription", side_effect=Exception("normalizer failed")):
        result = await connector.sync(full=True)
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_ingest_called_with_kb_id(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(
        side_effect=[make_page([SAMPLE_SUBSCRIPTION_ITEM]), make_page([])]
    )
    connector._http_client.list_customers = AsyncMock(
        side_effect=[make_page([SAMPLE_CUSTOMER_ITEM]), make_page([])]
    )
    connector._http_client.list_invoices = AsyncMock(
        side_effect=[make_page([SAMPLE_INVOICE_ITEM]), make_page([])]
    )
    connector._ingest_document = AsyncMock()
    result = await connector.sync(full=True, kb_id="kb_billing_001")
    assert connector._ingest_document.call_count == 3
    assert result.documents_synced == 3


@pytest.mark.asyncio
async def test_sync_no_ingest_without_kb_id(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(
        side_effect=[make_page([SAMPLE_SUBSCRIPTION_ITEM]), make_page([])]
    )
    connector._http_client.list_customers = AsyncMock(return_value=make_page([]))
    connector._http_client.list_invoices = AsyncMock(return_value=make_page([]))
    connector._ingest_document = AsyncMock()
    await connector.sync(full=True)
    connector._ingest_document.assert_not_called()


# ── list_subscriptions() ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_subscriptions_returns_page(connector: ChargebeeConnector) -> None:
    page = make_page([SAMPLE_SUBSCRIPTION_ITEM, SAMPLE_SUBSCRIPTION_ITEM_2])
    connector._http_client.list_subscriptions = AsyncMock(return_value=page)
    result = await connector.list_subscriptions(limit=100)
    assert len(result["list"]) == 2
    assert result["list"][0]["subscription"]["id"] == "sub_ABC123"


@pytest.mark.asyncio
async def test_list_subscriptions_empty_page(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(return_value=make_page([]))
    result = await connector.list_subscriptions()
    assert result["list"] == []


@pytest.mark.asyncio
async def test_list_subscriptions_passes_offset(connector: ChargebeeConnector) -> None:
    connector._http_client.list_subscriptions = AsyncMock(return_value=make_page([]))
    await connector.list_subscriptions(limit=50, offset="some_offset")
    connector._http_client.list_subscriptions.assert_called_once_with(
        SITE, API_KEY, limit=50, offset="some_offset"
    )


# ── get_subscription() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_subscription_success(connector: ChargebeeConnector) -> None:
    connector._http_client.get_subscription = AsyncMock(
        return_value={"subscription": SAMPLE_SUBSCRIPTION_RAW}
    )
    result = await connector.get_subscription("sub_ABC123")
    assert result["id"] == "sub_ABC123"
    assert result["status"] == "active"


@pytest.mark.asyncio
async def test_get_subscription_not_found(connector: ChargebeeConnector) -> None:
    connector._http_client.get_subscription = AsyncMock(
        side_effect=ChargebeeNotFoundError("subscription", "sub_MISSING")
    )
    with pytest.raises(ChargebeeNotFoundError):
        await connector.get_subscription("sub_MISSING")


@pytest.mark.asyncio
async def test_get_subscription_unwraps_envelope(connector: ChargebeeConnector) -> None:
    """Even if the API returns a bare dict, the connector should handle it."""
    connector._http_client.get_subscription = AsyncMock(
        return_value=SAMPLE_SUBSCRIPTION_RAW
    )
    result = await connector.get_subscription("sub_ABC123")
    # When no "subscription" key, the raw dict is returned as-is
    assert result["id"] == "sub_ABC123"


# ── list_customers() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_customers_returns_page(connector: ChargebeeConnector) -> None:
    page = make_page([SAMPLE_CUSTOMER_ITEM, SAMPLE_CUSTOMER_ITEM_2])
    connector._http_client.list_customers = AsyncMock(return_value=page)
    result = await connector.list_customers(limit=100)
    assert len(result["list"]) == 2
    assert result["list"][0]["customer"]["id"] == "cus_CUST001"


@pytest.mark.asyncio
async def test_list_customers_empty(connector: ChargebeeConnector) -> None:
    connector._http_client.list_customers = AsyncMock(return_value=make_page([]))
    result = await connector.list_customers()
    assert result["list"] == []


# ── get_customer() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_customer_success(connector: ChargebeeConnector) -> None:
    connector._http_client.get_customer = AsyncMock(
        return_value={"customer": SAMPLE_CUSTOMER_RAW}
    )
    result = await connector.get_customer("cus_CUST001")
    assert result["id"] == "cus_CUST001"
    assert result["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_get_customer_not_found(connector: ChargebeeConnector) -> None:
    connector._http_client.get_customer = AsyncMock(
        side_effect=ChargebeeNotFoundError("customer", "cus_MISSING")
    )
    with pytest.raises(ChargebeeNotFoundError):
        await connector.get_customer("cus_MISSING")


# ── list_invoices() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_invoices_returns_page(connector: ChargebeeConnector) -> None:
    page = make_page([SAMPLE_INVOICE_ITEM, SAMPLE_INVOICE_ITEM_2])
    connector._http_client.list_invoices = AsyncMock(return_value=page)
    result = await connector.list_invoices(limit=100)
    assert len(result["list"]) == 2
    assert result["list"][0]["invoice"]["id"] == "inv_INV001"


@pytest.mark.asyncio
async def test_list_invoices_empty(connector: ChargebeeConnector) -> None:
    connector._http_client.list_invoices = AsyncMock(return_value=make_page([]))
    result = await connector.list_invoices()
    assert result["list"] == []


# ── get_invoice() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_invoice_success(connector: ChargebeeConnector) -> None:
    connector._http_client.get_invoice = AsyncMock(
        return_value={"invoice": SAMPLE_INVOICE_RAW}
    )
    result = await connector.get_invoice("inv_INV001")
    assert result["id"] == "inv_INV001"
    assert result["status"] == "paid"


@pytest.mark.asyncio
async def test_get_invoice_not_found(connector: ChargebeeConnector) -> None:
    connector._http_client.get_invoice = AsyncMock(
        side_effect=ChargebeeNotFoundError("invoice", "inv_MISSING")
    )
    with pytest.raises(ChargebeeNotFoundError):
        await connector.get_invoice("inv_MISSING")


# ── normalize_subscription() ──────────────────────────────────────────────────


def test_normalize_subscription_basic() -> None:
    doc = normalize_subscription(SAMPLE_SUBSCRIPTION_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert "sub_ABC123" in doc.title
    assert "professional-monthly" in doc.title
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["subscription_id"] == "sub_ABC123"
    assert doc.metadata["status"] == "active"
    assert doc.metadata["customer_id"] == "cus_CUST001"
    assert doc.metadata["mrr"] == 9900
    assert doc.source_url == f"https://{SITE}.chargebee.com/subscriptions/sub_ABC123"


def test_normalize_subscription_unwraps_envelope() -> None:
    """normalize_subscription must handle the list-item envelope."""
    doc = normalize_subscription(SAMPLE_SUBSCRIPTION_ITEM, CONNECTOR_ID, TENANT_ID, SITE)
    assert doc.metadata["subscription_id"] == "sub_ABC123"


def test_normalize_subscription_source_id_is_sha256_prefix() -> None:
    doc = normalize_subscription(SAMPLE_SUBSCRIPTION_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    expected = hashlib.sha256("subscription:sub_ABC123".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_subscription_content_has_plan() -> None:
    doc = normalize_subscription(SAMPLE_SUBSCRIPTION_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert "professional-monthly" in doc.content


def test_normalize_subscription_content_has_currency() -> None:
    doc = normalize_subscription(SAMPLE_SUBSCRIPTION_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert "USD" in doc.content


def test_normalize_subscription_content_has_mrr() -> None:
    doc = normalize_subscription(SAMPLE_SUBSCRIPTION_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert "9900" in doc.content


# ── normalize_customer() ──────────────────────────────────────────────────────


def test_normalize_customer_basic() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert doc.title == "Customer: Alice Smith"
    assert doc.metadata["email"] == "alice@example.com"
    assert doc.metadata["company"] == "Acme Corp"
    assert doc.metadata["phone"] == "+1-555-0101"
    assert doc.source_url == f"https://{SITE}.chargebee.com/customers/cus_CUST001"


def test_normalize_customer_unwraps_envelope() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER_ITEM, CONNECTOR_ID, TENANT_ID, SITE)
    assert doc.metadata["customer_id"] == "cus_CUST001"


def test_normalize_customer_source_id_is_sha256_prefix() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    expected = hashlib.sha256("customer:cus_CUST001".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_customer_full_name_in_title() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert "Alice" in doc.title
    assert "Smith" in doc.title


def test_normalize_customer_content_has_email() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert "alice@example.com" in doc.content


def test_normalize_customer_content_has_company() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert "Acme Corp" in doc.content


def test_normalize_customer_no_company() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER_RAW_2, CONNECTOR_ID, TENANT_ID, SITE)
    assert "Company" not in doc.content


def test_normalize_customer_no_phone() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER_RAW_2, CONNECTOR_ID, TENANT_ID, SITE)
    assert "Phone" not in doc.content


# ── normalize_invoice() ───────────────────────────────────────────────────────


def test_normalize_invoice_basic() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert "inv_INV001" in doc.title
    assert "paid" in doc.title
    assert doc.metadata["invoice_id"] == "inv_INV001"
    assert doc.metadata["status"] == "paid"
    assert doc.metadata["total"] == 9900
    assert doc.metadata["currency_code"] == "USD"
    assert doc.source_url == f"https://{SITE}.chargebee.com/invoices/inv_INV001"


def test_normalize_invoice_unwraps_envelope() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE_ITEM, CONNECTOR_ID, TENANT_ID, SITE)
    assert doc.metadata["invoice_id"] == "inv_INV001"


def test_normalize_invoice_source_id_is_sha256_prefix() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    expected = hashlib.sha256("invoice:inv_INV001".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_invoice_content_has_customer_id() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert "cus_CUST001" in doc.content


def test_normalize_invoice_content_has_subscription_id() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE_RAW, CONNECTOR_ID, TENANT_ID, SITE)
    assert "sub_ABC123" in doc.content


def test_normalize_invoice_voided_status() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE_RAW_2, CONNECTOR_ID, TENANT_ID, SITE)
    assert "voided" in doc.title
    assert doc.metadata["amount_due"] == 2900
    assert doc.metadata["paid_at"] is None


# ── with_retry() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    fn = AsyncMock(side_effect=[ChargebeeNetworkError("transient"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_auth_immediately() -> None:
    fn = AsyncMock(side_effect=ChargebeeAuthError("Unauthorized", 401))
    with pytest.raises(ChargebeeAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=ChargebeeNetworkError("always fails"))
    with pytest.raises(ChargebeeNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retried() -> None:
    fn = AsyncMock(
        side_effect=[
            ChargebeeRateLimitError("rate limited", retry_after=0),
            {"ok": True},
        ]
    )
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_chargebee_auth_error_is_chargebee_error() -> None:
    exc = ChargebeeAuthError("bad key", 401)
    assert isinstance(exc, ChargebeeError)
    assert exc.status_code == 401


def test_chargebee_not_found_error_message() -> None:
    exc = ChargebeeNotFoundError("subscription", "sub_ABC123")
    assert "sub_ABC123" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_chargebee_rate_limit_error_retry_after() -> None:
    exc = ChargebeeRateLimitError("too many", retry_after=60.0)
    assert exc.retry_after == 60.0
    assert exc.status_code == 429


def test_chargebee_network_error_inherits_base() -> None:
    exc = ChargebeeNetworkError("timeout")
    assert isinstance(exc, ChargebeeError)


def test_chargebee_error_fields() -> None:
    exc = ChargebeeError("some error", status_code=422, code="invalid_param")
    assert exc.message == "some error"
    assert exc.status_code == 422
    assert exc.code == "invalid_param"
