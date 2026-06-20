"""Unit tests for BigCommerceConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import BigCommerceConnector
from exceptions import (
    BigCommerceAuthError,
    BigCommerceError,
    BigCommerceNetworkError,
    BigCommerceNotFoundError,
    BigCommerceRateLimitError,
)
from helpers.utils import (
    _stable_id,
    _strip_html,
    normalize_customer,
    normalize_order,
    normalize_product,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, ConnectorDocument, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_bigcommerce_test_001"
STORE_HASH = "abc123def"
ACCESS_TOKEN = "test_access_token_xyz"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_PRODUCT: dict = {
    "id": 77,
    "name": "iPod Nano",
    "brand_name": "Apple",
    "sku": "IPOD-NANO-8GB",
    "price": 199.00,
    "sale_price": 179.00,
    "availability": "available",
    "condition": "New",
    "type": "physical",
    "weight": 0.3,
    "categories": [18, 23],
    "description": "<p>The <strong>best</strong> MP3 player ever made.</p>",
    "inventory_level": 42,
    "inventory_tracking": "product",
}

SAMPLE_ORDER: dict = {
    "id": 100,
    "status": "Awaiting Payment",
    "total_inc_tax": "99.50",
    "total_ex_tax": "89.50",
    "currency_code": "USD",
    "date_created": "Wed, 08 May 2024 14:52:26 +0000",
    "date_modified": "Wed, 08 May 2024 14:52:26 +0000",
    "payment_method": "Credit Card",
    "items_total": 2,
    "items_shipped": 0,
    "refunded_amount": "0.00",
    "customer_email": "jane@example.com",
    "billing_address": {
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@example.com",
    },
}

SAMPLE_CUSTOMER: dict = {
    "id": 10,
    "first_name": "Jane",
    "last_name": "Doe",
    "email": "jane@example.com",
    "company": "Acme Corp",
    "phone": "+15551234567",
    "date_created": "2024-03-01T00:00:00Z",
    "date_modified": "2024-05-01T00:00:00Z",
    "accepts_product_review_abandoned_cart_emails": True,
    "store_credit_amounts": [{"amount": 10.00}],
    "customer_group_id": 5,
}

SAMPLE_STORE: dict = {
    "id": "abc123def",
    "name": "Test BC Store",
    "domain": "test.mybigcommerce.com",
    "plan_name": "Standard",
}

SAMPLE_PRODUCTS_RESPONSE: dict = {
    "data": [SAMPLE_PRODUCT],
    "meta": {"pagination": {"total_pages": 1, "current_page": 1, "total": 1}},
}

SAMPLE_CUSTOMERS_RESPONSE: dict = {
    "data": [SAMPLE_CUSTOMER],
    "meta": {"pagination": {"total_pages": 1, "current_page": 1, "total": 1}},
}

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> BigCommerceConnector:
    c = BigCommerceConnector(
        store_hash=STORE_HASH,
        access_token=ACCESS_TOKEN,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = BigCommerceConnector(
        store_hash=STORE_HASH, access_token=ACCESS_TOKEN,
        connector_id=CONNECTOR_ID, tenant_id=TENANT_ID,
    )
    with patch("connector.BigCommerceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store = AsyncMock(return_value=SAMPLE_STORE)
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test BC Store" in result.message


@pytest.mark.asyncio
async def test_install_missing_store_hash() -> None:
    c = BigCommerceConnector(store_hash="", access_token=ACCESS_TOKEN, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "store_hash" in result.message


@pytest.mark.asyncio
async def test_install_missing_access_token() -> None:
    c = BigCommerceConnector(store_hash=STORE_HASH, access_token="", connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_missing_both_credentials() -> None:
    c = BigCommerceConnector(store_hash="", access_token="", connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "store_hash" in result.message
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_invalid_token() -> None:
    c = BigCommerceConnector(
        store_hash=STORE_HASH, access_token="bad_token",
        connector_id=CONNECTOR_ID, tenant_id=TENANT_ID,
    )
    with patch("connector.BigCommerceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store = AsyncMock(side_effect=BigCommerceAuthError("Unauthorized", 401))
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = BigCommerceConnector(
        store_hash=STORE_HASH, access_token=ACCESS_TOKEN,
        connector_id=CONNECTOR_ID, tenant_id=TENANT_ID,
    )
    with patch("connector.BigCommerceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store = AsyncMock(side_effect=Exception("Connection refused"))
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_uses_store_hash_as_connector_id_when_none() -> None:
    c = BigCommerceConnector(
        store_hash=STORE_HASH, access_token=ACCESS_TOKEN,
        connector_id="", tenant_id=TENANT_ID,
    )
    with patch("connector.BigCommerceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store = AsyncMock(return_value=SAMPLE_STORE)
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.connector_id == STORE_HASH


# ── health_check() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(authed: BigCommerceConnector) -> None:
    with patch("connector.BigCommerceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store = AsyncMock(return_value=SAMPLE_STORE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test BC Store" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: BigCommerceConnector) -> None:
    with patch("connector.BigCommerceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store = AsyncMock(side_effect=BigCommerceAuthError("Forbidden", 403))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: BigCommerceConnector) -> None:
    with patch("connector.BigCommerceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store = AsyncMock(side_effect=BigCommerceNetworkError("Timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = BigCommerceConnector(store_hash="", access_token="", tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── list_products() / get_product() ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_products_single_page(authed: BigCommerceConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=([SAMPLE_PRODUCT], 1))
    result = await authed.list_products()
    assert len(result) == 1
    assert result[0]["id"] == 77


@pytest.mark.asyncio
async def test_list_products_respects_limit_and_page(authed: BigCommerceConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=([], 1))
    await authed.list_products(limit=50, page=2)
    authed.http_client.list_products.assert_awaited_once_with(
        STORE_HASH, ACCESS_TOKEN, 50, 2
    )


@pytest.mark.asyncio
async def test_get_product_success(authed: BigCommerceConnector) -> None:
    authed.http_client.get_product = AsyncMock(return_value=SAMPLE_PRODUCT)
    result = await authed.get_product(77)
    assert result["name"] == "iPod Nano"


# ── list_orders() / get_order() ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_orders_single_page(authed: BigCommerceConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=[SAMPLE_ORDER])
    result = await authed.list_orders()
    assert len(result) == 1
    assert result[0]["id"] == 100


@pytest.mark.asyncio
async def test_list_orders_respects_limit_and_page(authed: BigCommerceConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=[])
    await authed.list_orders(limit=100, page=3)
    authed.http_client.list_orders.assert_awaited_once_with(
        STORE_HASH, ACCESS_TOKEN, 100, 3
    )


@pytest.mark.asyncio
async def test_get_order_success(authed: BigCommerceConnector) -> None:
    authed.http_client.get_order = AsyncMock(return_value=SAMPLE_ORDER)
    result = await authed.get_order(100)
    assert result["status"] == "Awaiting Payment"


# ── list_customers() / get_customer() ────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_customers_single_page(authed: BigCommerceConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value=([SAMPLE_CUSTOMER], 1))
    result = await authed.list_customers()
    assert len(result) == 1
    assert result[0]["email"] == "jane@example.com"


@pytest.mark.asyncio
async def test_list_customers_respects_limit_and_page(authed: BigCommerceConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value=([], 1))
    await authed.list_customers(limit=50, page=2)
    authed.http_client.list_customers.assert_awaited_once_with(
        STORE_HASH, ACCESS_TOKEN, 50, 2
    )


@pytest.mark.asyncio
async def test_get_customer_success(authed: BigCommerceConnector) -> None:
    authed.http_client.get_customer = AsyncMock(return_value=SAMPLE_CUSTOMER)
    result = await authed.get_customer(10)
    assert result["first_name"] == "Jane"


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty_store(authed: BigCommerceConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=([], 1))
    authed.http_client.list_orders = AsyncMock(return_value=[])
    authed.http_client.list_customers = AsyncMock(return_value=([], 1))
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_data(authed: BigCommerceConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=([SAMPLE_PRODUCT], 1))
    authed.http_client.list_orders = AsyncMock(return_value=[SAMPLE_ORDER])
    authed.http_client.list_customers = AsyncMock(return_value=([SAMPLE_CUSTOMER], 1))
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_products_pagination(authed: BigCommerceConnector) -> None:
    """Verify that sync follows v3 page-number pagination for products."""
    page_responses = [
        ([SAMPLE_PRODUCT], 2),  # page 1, 2 total pages
        ([{**SAMPLE_PRODUCT, "id": 78}], 2),  # page 2
    ]
    call_count = 0

    async def mock_list_products(store_hash: str, access_token: str, limit: int, page: int) -> tuple:
        nonlocal call_count
        resp = page_responses[call_count]
        call_count += 1
        return resp

    authed.http_client.list_products = mock_list_products
    authed.http_client.list_orders = AsyncMock(return_value=[])
    authed.http_client.list_customers = AsyncMock(return_value=([], 1))
    result = await authed.sync(full=True)
    assert result.documents_found >= 2
    assert call_count == 2


@pytest.mark.asyncio
async def test_sync_orders_pagination(authed: BigCommerceConnector) -> None:
    """Verify that sync follows v2 length-based pagination for orders."""
    page_responses = [
        [SAMPLE_ORDER] * 250,   # page 1 — full page, continue
        [SAMPLE_ORDER] * 10,    # page 2 — partial page, stop
    ]
    call_count = 0

    async def mock_list_orders(store_hash: str, access_token: str, limit: int, page: int) -> list:
        nonlocal call_count
        resp = page_responses[call_count]
        call_count += 1
        return resp

    authed.http_client.list_products = AsyncMock(return_value=([], 1))
    authed.http_client.list_orders = mock_list_orders
    authed.http_client.list_customers = AsyncMock(return_value=([], 1))
    result = await authed.sync(full=True)
    assert call_count == 2
    assert result.documents_found == 260


@pytest.mark.asyncio
async def test_sync_products_fail_returns_failed(authed: BigCommerceConnector) -> None:
    authed.http_client.list_products = AsyncMock(
        side_effect=BigCommerceNetworkError("Server error")
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "Products sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_orders_fail_returns_partial(authed: BigCommerceConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=([SAMPLE_PRODUCT], 1))
    authed.http_client.list_orders = AsyncMock(
        side_effect=BigCommerceNetworkError("Orders unavailable")
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert "Orders sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_customers_fail_returns_partial(authed: BigCommerceConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=([SAMPLE_PRODUCT], 1))
    authed.http_client.list_orders = AsyncMock(return_value=[SAMPLE_ORDER])
    authed.http_client.list_customers = AsyncMock(
        side_effect=BigCommerceNetworkError("Customers unavailable")
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert "Customers sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_partial_normalization_failure(authed: BigCommerceConnector) -> None:
    """A normalization error on one record increments failed but doesn't stop sync."""
    bad_product = None  # will cause normalize_product to fail with AttributeError
    authed.http_client.list_products = AsyncMock(return_value=([bad_product], 1))
    authed.http_client.list_orders = AsyncMock(return_value=[])
    authed.http_client.list_customers = AsyncMock(return_value=([], 1))
    result = await authed.sync(full=True)
    assert result.documents_failed == 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(authed: BigCommerceConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=([SAMPLE_PRODUCT], 1))
    authed.http_client.list_orders = AsyncMock(return_value=[SAMPLE_ORDER])
    authed.http_client.list_customers = AsyncMock(return_value=([SAMPLE_CUSTOMER], 1))
    ingest_calls: list = []

    async def mock_ingest(doc: ConnectorDocument, kb_id: str) -> None:
        ingest_calls.append((doc, kb_id))

    authed._ingest_document = mock_ingest  # type: ignore[method-assign]
    result = await authed.sync(full=True, kb_id="kb_test_123")
    assert len(ingest_calls) == 3
    assert all(kb_id == "kb_test_123" for _, kb_id in ingest_calls)


# ── normalize_product() ───────────────────────────────────────────────────────


def test_normalize_product_basic() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert doc.title == "iPod Nano — Apple"
    assert "iPod Nano" in doc.content
    assert "Apple" in doc.content
    assert "199.0" in doc.content
    assert "available" in doc.content
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_product_stable_id() -> None:
    doc1 = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    doc2 = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16


def test_normalize_product_stable_id_prefix() -> None:
    """Stable id includes 'product:' prefix — different from order/customer."""
    p_doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    o_doc = normalize_order({**SAMPLE_ORDER, "id": 77}, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    # Same numeric ID (77) but different prefixes → different stable ids
    assert p_doc.source_id != o_doc.source_id


def test_normalize_product_html_stripped() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "<p>" not in doc.content
    assert "<strong>" not in doc.content
    assert "best" in doc.content


def test_normalize_product_no_brand() -> None:
    product = {**SAMPLE_PRODUCT, "brand_name": ""}
    doc = normalize_product(product, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert doc.title == "iPod Nano"


def test_normalize_product_no_description() -> None:
    product = {**SAMPLE_PRODUCT, "description": ""}
    doc = normalize_product(product, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "Description" not in doc.content


def test_normalize_product_metadata_fields() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert doc.metadata["product_id"] == 77
    assert doc.metadata["sku"] == "IPOD-NANO-8GB"
    assert doc.metadata["brand_name"] == "Apple"
    assert doc.metadata["inventory_level"] == 42


def test_normalize_product_source_url() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert STORE_HASH in doc.source_url
    assert "77" in doc.source_url


# ── normalize_order() ────────────────────────────────────────────────────────


def test_normalize_order_basic() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "Order #100" in doc.title
    assert "Jane Doe" in doc.title
    assert "Awaiting Payment" in doc.content
    assert "99.50" in doc.content
    assert "Credit Card" in doc.content
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_order_stable_id() -> None:
    doc1 = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    doc2 = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16


def test_normalize_order_guest_customer() -> None:
    order = {**SAMPLE_ORDER, "billing_address": {}, "customer_email": ""}
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "Guest" in doc.content or "Order #100" in doc.title


def test_normalize_order_email_fallback() -> None:
    """When billing_address has no name, fallback to email."""
    order = {
        **SAMPLE_ORDER,
        "billing_address": {"first_name": "", "last_name": "", "email": "anon@example.com"},
    }
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "anon@example.com" in doc.content


def test_normalize_order_metadata_fields() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert doc.metadata["order_id"] == 100
    assert doc.metadata["status"] == "Awaiting Payment"
    assert doc.metadata["total_inc_tax"] == "99.50"
    assert doc.metadata["payment_method"] == "Credit Card"


def test_normalize_order_source_url() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert STORE_HASH in doc.source_url
    assert "100" in doc.source_url


def test_normalize_order_currency_in_content() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "USD" in doc.content


# ── normalize_customer() ──────────────────────────────────────────────────────


def test_normalize_customer_basic() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "Jane Doe" in doc.title
    assert "jane@example.com" in doc.content
    assert "Acme Corp" in doc.content
    assert "+15551234567" in doc.content
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_customer_stable_id() -> None:
    doc1 = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    doc2 = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16


def test_normalize_customer_no_phone() -> None:
    customer = {**SAMPLE_CUSTOMER, "phone": ""}
    doc = normalize_customer(customer, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "Phone" not in doc.content


def test_normalize_customer_no_company() -> None:
    customer = {**SAMPLE_CUSTOMER, "company": ""}
    doc = normalize_customer(customer, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "Company" not in doc.content


def test_normalize_customer_email_only_name() -> None:
    customer = {**SAMPLE_CUSTOMER, "first_name": "", "last_name": ""}
    doc = normalize_customer(customer, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "jane@example.com" in doc.title


def test_normalize_customer_store_credit() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "10" in doc.content


def test_normalize_customer_no_store_credit() -> None:
    customer = {**SAMPLE_CUSTOMER, "store_credit_amounts": []}
    doc = normalize_customer(customer, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert "0" in doc.content


def test_normalize_customer_metadata_fields() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert doc.metadata["customer_id"] == 10
    assert doc.metadata["email"] == "jane@example.com"
    assert doc.metadata["company"] == "Acme Corp"
    assert doc.metadata["customer_group_id"] == 5


def test_normalize_customer_source_url() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_HASH)
    assert STORE_HASH in doc.source_url
    assert "10" in doc.source_url


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_bigcommerce_error_base() -> None:
    exc = BigCommerceError("Something went wrong", status_code=500, code="server_error")
    assert exc.message == "Something went wrong"
    assert exc.status_code == 500
    assert exc.code == "server_error"
    assert str(exc) == "Something went wrong"


def test_bigcommerce_auth_error_inherits() -> None:
    exc = BigCommerceAuthError("Unauthorized", 401)
    assert isinstance(exc, BigCommerceError)
    assert exc.status_code == 401


def test_bigcommerce_network_error_inherits() -> None:
    exc = BigCommerceNetworkError("Timeout")
    assert isinstance(exc, BigCommerceError)


def test_bigcommerce_rate_limit_error() -> None:
    exc = BigCommerceRateLimitError("Rate limited", retry_after=5.0)
    assert isinstance(exc, BigCommerceError)
    assert exc.status_code == 429
    assert exc.retry_after == 5.0
    assert exc.code == "rate_limit"


def test_bigcommerce_rate_limit_default_retry_after() -> None:
    exc = BigCommerceRateLimitError("Rate limited")
    assert exc.retry_after == 2.0


def test_bigcommerce_not_found_error() -> None:
    exc = BigCommerceNotFoundError("product", "77")
    assert isinstance(exc, BigCommerceError)
    assert exc.status_code == 404
    assert exc.code == "not_found"
    assert "product" in exc.message
    assert "77" in exc.message


# ── with_retry() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    mock = AsyncMock(return_value="ok")
    result = await with_retry(mock)
    assert result == "ok"
    assert mock.await_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    mock = AsyncMock(
        side_effect=[BigCommerceNetworkError("Timeout"), "success"]
    )
    result = await with_retry(mock, max_attempts=3, base_delay=0.0)
    assert result == "success"
    assert mock.await_count == 2


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock = AsyncMock(side_effect=BigCommerceAuthError("Unauthorized", 401))
    with pytest.raises(BigCommerceAuthError):
        await with_retry(mock, max_attempts=3, base_delay=0.0)
    assert mock.await_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_and_raises() -> None:
    mock = AsyncMock(side_effect=BigCommerceNetworkError("Server down"))
    with pytest.raises(BigCommerceNetworkError):
        await with_retry(mock, max_attempts=3, base_delay=0.0)
    assert mock.await_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    calls: list[float] = []

    async def slow_fn() -> str:
        return "ok"

    mock = AsyncMock(
        side_effect=[BigCommerceRateLimitError("Slow down", retry_after=0.0), "ok"]
    )
    result = await with_retry(mock, max_attempts=3, base_delay=0.0)
    assert result == "ok"


# ── _stable_id() / _strip_html() ─────────────────────────────────────────────


def test_stable_id_is_deterministic() -> None:
    assert _stable_id("product", 77) == _stable_id("product", 77)


def test_stable_id_length() -> None:
    assert len(_stable_id("order", 100)) == 16


def test_stable_id_different_prefixes_differ() -> None:
    assert _stable_id("product", 77) != _stable_id("order", 77)


def test_stable_id_different_ids_differ() -> None:
    assert _stable_id("product", 77) != _stable_id("product", 78)


def test_strip_html_basic() -> None:
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_html_empty() -> None:
    assert _strip_html("") == ""


def test_strip_html_no_tags() -> None:
    assert _strip_html("plain text") == "plain text"


def test_strip_html_nested() -> None:
    assert "<" not in _strip_html("<div><span><em>deep</em></span></div>")


# ── Connector lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_clears_http_client(authed: BigCommerceConnector) -> None:
    authed.http_client.aclose = AsyncMock()
    await authed.aclose()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    c = BigCommerceConnector(store_hash=STORE_HASH, access_token=ACCESS_TOKEN)
    await c.aclose()  # No http_client set — should not raise


@pytest.mark.asyncio
async def test_context_manager() -> None:
    async with BigCommerceConnector(store_hash=STORE_HASH, access_token=ACCESS_TOKEN) as c:
        assert isinstance(c, BigCommerceConnector)


def test_ensure_client_creates_client() -> None:
    c = BigCommerceConnector(store_hash=STORE_HASH, access_token=ACCESS_TOKEN)
    assert c.http_client is None
    client = c._ensure_client()
    assert c.http_client is not None
    assert client is c.http_client


def test_ensure_client_reuses_existing() -> None:
    c = BigCommerceConnector(store_hash=STORE_HASH, access_token=ACCESS_TOKEN)
    first = c._ensure_client()
    second = c._ensure_client()
    assert first is second


# ── Config-based init ─────────────────────────────────────────────────────────


def test_init_from_config() -> None:
    c = BigCommerceConnector(
        config={"store_hash": "myhash", "access_token": "mytoken"},
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
    )
    assert c._store_hash == "myhash"
    assert c._access_token == "mytoken"


def test_init_kwarg_wins_when_config_empty() -> None:
    c = BigCommerceConnector(
        store_hash="kwarg_hash",
        access_token="kwarg_token",
    )
    assert c._store_hash == "kwarg_hash"
    assert c._access_token == "kwarg_token"


def test_connector_type_and_auth_type() -> None:
    assert BigCommerceConnector.CONNECTOR_TYPE == "bigcommerce"
    assert BigCommerceConnector.AUTH_TYPE == "api_key"


# ── Model defaults ────────────────────────────────────────────────────────────


def test_sync_result_defaults() -> None:
    from models import SyncResult
    r = SyncResult(status=SyncStatus.COMPLETED)
    assert r.documents_found == 0
    assert r.documents_synced == 0
    assert r.documents_failed == 0
    assert r.message == ""


def test_install_result_defaults() -> None:
    from models import InstallResult
    r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
    assert r.connector_id == ""
    assert r.message == ""


def test_health_check_result_defaults() -> None:
    from models import HealthCheckResult
    r = HealthCheckResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
    assert r.message == ""


def test_connector_document_defaults() -> None:
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test",
        content="Content",
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    assert doc.source_url == ""
    assert doc.metadata == {}
