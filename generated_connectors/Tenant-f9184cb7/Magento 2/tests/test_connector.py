"""Unit tests for MagentoConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import MagentoConnector
from exceptions import (
    MagentoAuthError,
    MagentoError,
    MagentoNetworkError,
    MagentoNotFoundError,
    MagentoRateLimitError,
)
from helpers.utils import (
    _stable_id,
    normalize_customer,
    normalize_order,
    normalize_product,
    with_retry,
)
from models import AuthStatus, ConnectorDocument, ConnectorHealth, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_magento_test_001"
BASE_URL = "https://mystore.example.com"
ACCESS_TOKEN = "abc123xyz_magento_integration_token"

# ── Sample fixtures ───────────────────────────────────────────────────────────

SAMPLE_ORDER: dict = {
    "entity_id": 100000001,
    "increment_id": "000000001",
    "status": "processing",
    "grand_total": 199.00,
    "customer_firstname": "Jane",
    "customer_lastname": "Doe",
    "customer_email": "jane.doe@example.com",
    "total_item_count": 2,
    "items_qty_ordered": 3,
    "created_at": "2024-01-15 10:00:00",
    "items": [
        {"name": "Widget Pro", "qty_ordered": 2, "price": 89.50},
        {"name": "Gadget Max", "qty_ordered": 1, "price": 20.00},
    ],
}

SAMPLE_PRODUCT: dict = {
    "id": 1,
    "sku": "WIDGET-PRO-001",
    "name": "Widget Pro",
    "type_id": "simple",
    "status": 1,
    "price": 89.50,
    "visibility": 4,
    "created_at": "2024-01-01 08:00:00",
}

SAMPLE_CUSTOMER: dict = {
    "id": 5001,
    "firstname": "Jane",
    "lastname": "Doe",
    "email": "jane.doe@example.com",
    "created_at": "2024-01-05 09:00:00",
    "group_id": 1,
}

SAMPLE_STORE_CONFIGS: list = [
    {
        "id": 1,
        "code": "default",
        "website_id": 1,
        "locale": "en_US",
        "base_currency_code": "USD",
        "base_url": "https://mystore.example.com/",
        "store_name": "My Magento Store",
    }
]

SAMPLE_CATEGORIES: dict = {
    "id": 1,
    "parent_id": 0,
    "name": "Root Catalog",
    "children_data": [
        {"id": 2, "parent_id": 1, "name": "Default Category", "children_data": []},
        {"id": 3, "parent_id": 1, "name": "Electronics", "children_data": []},
    ],
}

PAGE_RESPONSE_EMPTY: dict = {"items": [], "total_count": 0}


def _orders_page(items: list, total: int) -> dict:
    return {"items": items, "total_count": total}


def _products_page(items: list, total: int) -> dict:
    return {"items": items, "total_count": total}


def _customers_page(items: list, total: int) -> dict:
    return {"items": items, "total_count": total}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> MagentoConnector:
    c = MagentoConnector(
        base_url=BASE_URL,
        access_token=ACCESS_TOKEN,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ── Class attributes ──────────────────────────────────────────────────────────


def test_connector_type() -> None:
    assert MagentoConnector.CONNECTOR_TYPE == "magento"


def test_auth_type() -> None:
    assert MagentoConnector.AUTH_TYPE == "api_key"


def test_constructor_from_kwargs() -> None:
    c = MagentoConnector(base_url=BASE_URL, access_token=ACCESS_TOKEN, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert c._base_url == BASE_URL
    assert c._access_token == ACCESS_TOKEN
    assert c.connector_id == CONNECTOR_ID
    assert c._tenant_id == TENANT_ID
    assert c.http_client is None


def test_constructor_from_config() -> None:
    config = {"base_url": BASE_URL, "access_token": ACCESS_TOKEN}
    c = MagentoConnector(config=config, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert c._base_url == BASE_URL
    assert c._access_token == ACCESS_TOKEN


def test_constructor_config_overrides_kwargs() -> None:
    config = {"base_url": "https://from-config.com", "access_token": "config_token"}
    c = MagentoConnector(config=config, base_url="https://kwarg.com", access_token="kwarg_token")
    assert c._base_url == "https://from-config.com"
    assert c._access_token == "config_token"


# ── install() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = MagentoConnector(base_url=BASE_URL, access_token=ACCESS_TOKEN, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    with patch("connector.MagentoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store_info = AsyncMock(return_value=SAMPLE_STORE_CONFIGS)
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "My Magento Store" in result.message or "mystore" in result.message


@pytest.mark.asyncio
async def test_install_missing_base_url() -> None:
    c = MagentoConnector(base_url="", access_token=ACCESS_TOKEN)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "base_url" in result.message


@pytest.mark.asyncio
async def test_install_missing_access_token() -> None:
    c = MagentoConnector(base_url=BASE_URL, access_token="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_missing_both_credentials() -> None:
    c = MagentoConnector(base_url="", access_token="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "base_url" in result.message
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_invalid_token() -> None:
    c = MagentoConnector(base_url=BASE_URL, access_token="bad_token")
    with patch("connector.MagentoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store_info = AsyncMock(side_effect=MagentoAuthError("Invalid token", 401))
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = MagentoConnector(base_url=BASE_URL, access_token=ACCESS_TOKEN)
    with patch("connector.MagentoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store_info = AsyncMock(side_effect=Exception("Connection refused"))
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_creates_new_http_client_after_success() -> None:
    c = MagentoConnector(base_url=BASE_URL, access_token=ACCESS_TOKEN)
    with patch("connector.MagentoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store_info = AsyncMock(return_value=SAMPLE_STORE_CONFIGS)
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        await c.install()
    assert c.http_client is not None


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(authed: MagentoConnector) -> None:
    with patch("connector.MagentoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store_info = AsyncMock(return_value=SAMPLE_STORE_CONFIGS)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "My Magento Store" in result.message or "Connected" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: MagentoConnector) -> None:
    with patch("connector.MagentoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store_info = AsyncMock(side_effect=MagentoAuthError("Invalid token", 401))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: MagentoConnector) -> None:
    with patch("connector.MagentoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_store_info = AsyncMock(side_effect=MagentoNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = MagentoConnector(base_url="", access_token="")
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_products = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_customers = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_all_objects(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=_orders_page([SAMPLE_ORDER], 1))
    authed.http_client.list_products = AsyncMock(return_value=_products_page([SAMPLE_PRODUCT], 1))
    authed.http_client.list_customers = AsyncMock(return_value=_customers_page([SAMPLE_CUSTOMER], 1))
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_orders_only(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=_orders_page([SAMPLE_ORDER], 1))
    authed.http_client.list_products = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_customers = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    result = await authed.sync(full=True)
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_orders_pagination(authed: MagentoConnector) -> None:
    order2 = {**SAMPLE_ORDER, "entity_id": 100000002, "increment_id": "000000002"}
    # total_count=101 forces a second page (page 1: 100 items read, but total=101)
    # We simulate by returning 1 item but total=2 (page_size=100 >= 2 so stops after first)
    # Actually with page_size=100, page 1*100=100 < 2 is False, so one page.
    # To get 2 calls: total_count=150, first page has 100 items, second page has 50.
    orders_page1 = [SAMPLE_ORDER] * 100
    orders_page2 = [order2] * 50
    authed.http_client.list_orders = AsyncMock(
        side_effect=[
            _orders_page(orders_page1, 150),
            _orders_page(orders_page2, 150),
        ]
    )
    authed.http_client.list_products = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_customers = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    result = await authed.sync(full=True)
    assert result.documents_found == 150
    assert authed.http_client.list_orders.call_count == 2


@pytest.mark.asyncio
async def test_sync_products_pagination(authed: MagentoConnector) -> None:
    product2 = {**SAMPLE_PRODUCT, "id": 2, "sku": "PRODUCT-002"}
    prods_page1 = [SAMPLE_PRODUCT] * 100
    prods_page2 = [product2] * 25
    authed.http_client.list_orders = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_products = AsyncMock(
        side_effect=[
            _products_page(prods_page1, 125),
            _products_page(prods_page2, 125),
        ]
    )
    authed.http_client.list_customers = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    result = await authed.sync(full=True)
    assert result.documents_found == 125
    assert authed.http_client.list_products.call_count == 2


@pytest.mark.asyncio
async def test_sync_customers_pagination(authed: MagentoConnector) -> None:
    customer2 = {**SAMPLE_CUSTOMER, "id": 5002, "email": "other@example.com"}
    custs_page1 = [SAMPLE_CUSTOMER] * 100
    custs_page2 = [customer2] * 10
    authed.http_client.list_orders = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_products = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_customers = AsyncMock(
        side_effect=[
            _customers_page(custs_page1, 110),
            _customers_page(custs_page2, 110),
        ]
    )
    result = await authed.sync(full=True)
    assert result.documents_found == 110
    assert authed.http_client.list_customers.call_count == 2


@pytest.mark.asyncio
async def test_sync_orders_fail_returns_failed(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(
        side_effect=MagentoNetworkError("server error", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "Orders sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_products_fail_returns_partial(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_products = AsyncMock(
        side_effect=MagentoNetworkError("server error", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert "Products sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_customers_fail_returns_partial(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=_orders_page([SAMPLE_ORDER], 1))
    authed.http_client.list_products = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_customers = AsyncMock(
        side_effect=MagentoNetworkError("server error", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert "Customers sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_with_since(authed: MagentoConnector) -> None:
    from datetime import datetime, timezone
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    authed.http_client.list_orders = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_products = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_customers = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    result = await authed.sync(full=False, since=since)
    assert result.status == SyncStatus.COMPLETED
    # created_after should be passed as 7th arg (index 6): base_url, token, page, page_size, sort_field, sort_dir, created_after
    call_args = authed.http_client.list_orders.call_args
    assert "2024-01-01" in call_args.args[6]


@pytest.mark.asyncio
async def test_sync_normalize_error_increments_failed(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=_orders_page([SAMPLE_ORDER, SAMPLE_ORDER], 2))
    authed.http_client.list_products = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_customers = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    call_count = {"n": 0}
    original_normalize = normalize_order

    def patched_normalize(order, connector_id, tenant_id, base_url):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("normalize failed")
        return original_normalize(order, connector_id, tenant_id, base_url)

    with patch("connector.normalize_order", side_effect=patched_normalize):
        result = await authed.sync(full=True)
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_creates_client_if_none(authed: MagentoConnector) -> None:
    authed.http_client = None
    mock_client = MagicMock()
    mock_client.list_orders = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    mock_client.list_products = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    mock_client.list_customers = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed._make_client = lambda: mock_client
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert authed.http_client is mock_client


# ── list_orders() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_orders_returns_items(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=_orders_page([SAMPLE_ORDER], 1))
    result = await authed.list_orders()
    assert len(result) == 1
    assert result[0]["entity_id"] == 100000001


@pytest.mark.asyncio
async def test_list_orders_with_page_and_size(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=_orders_page([SAMPLE_ORDER], 1))
    result = await authed.list_orders(page=2, page_size=50)
    call_args = authed.http_client.list_orders.call_args
    assert call_args.args[2] == 2   # page
    assert call_args.args[3] == 50  # page_size


@pytest.mark.asyncio
async def test_list_orders_with_created_after(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=_orders_page([], 0))
    await authed.list_orders(created_after="2024-01-01T00:00:00")
    call_args = authed.http_client.list_orders.call_args
    assert call_args.args[6] == "2024-01-01T00:00:00"


@pytest.mark.asyncio
async def test_list_orders_empty(authed: MagentoConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    result = await authed.list_orders()
    assert result == []


# ── get_order() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_order_success(authed: MagentoConnector) -> None:
    authed.http_client.get_order = AsyncMock(return_value=SAMPLE_ORDER)
    result = await authed.get_order(100000001)
    assert result["entity_id"] == 100000001


@pytest.mark.asyncio
async def test_get_order_not_found(authed: MagentoConnector) -> None:
    authed.http_client.get_order = AsyncMock(
        side_effect=MagentoNotFoundError("order", "99999")
    )
    with pytest.raises(MagentoNotFoundError):
        await authed.get_order(99999)


# ── list_products() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_products_returns_items(authed: MagentoConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=_products_page([SAMPLE_PRODUCT], 1))
    result = await authed.list_products()
    assert len(result) == 1
    assert result[0]["sku"] == "WIDGET-PRO-001"


@pytest.mark.asyncio
async def test_list_products_with_page(authed: MagentoConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=_products_page([], 0))
    await authed.list_products(page=3, page_size=25)
    call_args = authed.http_client.list_products.call_args
    assert call_args.args[2] == 3
    assert call_args.args[3] == 25


@pytest.mark.asyncio
async def test_list_products_empty(authed: MagentoConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    result = await authed.list_products()
    assert result == []


# ── get_product() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_product_success(authed: MagentoConnector) -> None:
    authed.http_client.get_product = AsyncMock(return_value=SAMPLE_PRODUCT)
    result = await authed.get_product("WIDGET-PRO-001")
    assert result["name"] == "Widget Pro"


@pytest.mark.asyncio
async def test_get_product_not_found(authed: MagentoConnector) -> None:
    authed.http_client.get_product = AsyncMock(
        side_effect=MagentoNotFoundError("product", "UNKNOWN-SKU")
    )
    with pytest.raises(MagentoNotFoundError):
        await authed.get_product("UNKNOWN-SKU")


# ── list_customers() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_customers_returns_items(authed: MagentoConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value=_customers_page([SAMPLE_CUSTOMER], 1))
    result = await authed.list_customers()
    assert len(result) == 1
    assert result[0]["id"] == 5001


@pytest.mark.asyncio
async def test_list_customers_with_page(authed: MagentoConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value=_customers_page([], 0))
    await authed.list_customers(page=2, page_size=50)
    call_args = authed.http_client.list_customers.call_args
    assert call_args.args[2] == 2
    assert call_args.args[3] == 50


@pytest.mark.asyncio
async def test_list_customers_empty(authed: MagentoConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    result = await authed.list_customers()
    assert result == []


# ── get_customer() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_customer_success(authed: MagentoConnector) -> None:
    authed.http_client.get_customer = AsyncMock(return_value=SAMPLE_CUSTOMER)
    result = await authed.get_customer(5001)
    assert result["email"] == "jane.doe@example.com"


@pytest.mark.asyncio
async def test_get_customer_not_found(authed: MagentoConnector) -> None:
    authed.http_client.get_customer = AsyncMock(
        side_effect=MagentoNotFoundError("customer", "99999")
    )
    with pytest.raises(MagentoNotFoundError):
        await authed.get_customer(99999)


# ── list_categories() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_categories_returns_tree(authed: MagentoConnector) -> None:
    authed.http_client.list_categories = AsyncMock(return_value=SAMPLE_CATEGORIES)
    result = await authed.list_categories()
    assert result["id"] == 1
    assert result["name"] == "Root Catalog"
    assert len(result["children_data"]) == 2


@pytest.mark.asyncio
async def test_list_categories_empty(authed: MagentoConnector) -> None:
    authed.http_client.list_categories = AsyncMock(return_value={"id": 1, "name": "Root", "children_data": []})
    result = await authed.list_categories()
    assert result["children_data"] == []


# ── normalize_order() ─────────────────────────────────────────────────────────


def test_normalize_order_basic() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert isinstance(doc, ConnectorDocument)
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "Order #000000001" in doc.title
    assert "Jane Doe" in doc.title
    assert doc.metadata["order_id"] == 100000001
    assert doc.metadata["increment_id"] == "000000001"
    assert doc.metadata["status"] == "processing"
    assert doc.metadata["grand_total"] == 199.00
    assert doc.metadata["customer_email"] == "jane.doe@example.com"
    assert "mystore.example.com" in doc.source_url
    assert "100000001" in doc.source_url


def test_normalize_order_source_id_is_sha256() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert len(doc.source_id) == 16
    assert doc.source_id == _stable_id(100000001)


def test_normalize_order_content_has_items() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "Widget Pro" in doc.content
    assert "Gadget Max" in doc.content


def test_normalize_order_content_has_status() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "processing" in doc.content


def test_normalize_order_guest_customer() -> None:
    order = {**SAMPLE_ORDER, "customer_firstname": "", "customer_lastname": "", "customer_email": "guest@example.com"}
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "guest@example.com" in doc.title or "guest@example.com" in doc.content


def test_normalize_order_source_url_format() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "/admin/sales/order/view/order_id/100000001" in doc.source_url


def test_normalize_order_uses_entity_id() -> None:
    order = {**SAMPLE_ORDER, "entity_id": 999}
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert doc.source_id == _stable_id(999)


def test_normalize_order_minimal() -> None:
    """Minimal order with only entity_id."""
    doc = normalize_order({"entity_id": 1}, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert isinstance(doc, ConnectorDocument)
    assert doc.source_id == _stable_id(1)


# ── normalize_product() ───────────────────────────────────────────────────────


def test_normalize_product_basic() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert isinstance(doc, ConnectorDocument)
    assert "Widget Pro" in doc.title
    assert "simple" in doc.title
    assert doc.metadata["sku"] == "WIDGET-PRO-001"
    assert doc.metadata["type_id"] == "simple"
    assert doc.metadata["status"] == 1
    assert doc.metadata["price"] == 89.50
    assert doc.metadata["visibility"] == 4
    assert "mystore.example.com" in doc.source_url
    assert "/admin/catalog/product/edit/id/1" in doc.source_url


def test_normalize_product_source_id_is_sha256_of_sku() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert len(doc.source_id) == 16
    assert doc.source_id == _stable_id("WIDGET-PRO-001")


def test_normalize_product_content_has_sku() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "WIDGET-PRO-001" in doc.content


def test_normalize_product_status_enabled() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "enabled" in doc.content


def test_normalize_product_status_disabled() -> None:
    product = {**SAMPLE_PRODUCT, "status": 2}
    doc = normalize_product(product, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "disabled" in doc.content


def test_normalize_product_no_type_id() -> None:
    product = {**SAMPLE_PRODUCT, "type_id": ""}
    doc = normalize_product(product, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert doc.title == "Widget Pro"


def test_normalize_product_visibility_label() -> None:
    product = {**SAMPLE_PRODUCT, "visibility": 1}
    doc = normalize_product(product, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "not visible" in doc.content


def test_normalize_product_minimal() -> None:
    doc = normalize_product({"id": 42, "sku": "MIN-SKU"}, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert isinstance(doc, ConnectorDocument)
    assert doc.source_id == _stable_id("MIN-SKU")


# ── normalize_customer() ──────────────────────────────────────────────────────


def test_normalize_customer_basic() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert isinstance(doc, ConnectorDocument)
    assert "Jane Doe" in doc.title
    assert "Customer:" in doc.title
    assert doc.metadata["customer_id"] == 5001
    assert doc.metadata["email"] == "jane.doe@example.com"
    assert doc.metadata["group_id"] == 1
    assert doc.metadata["created_at"] == "2024-01-05 09:00:00"
    assert "mystore.example.com" in doc.source_url
    assert "/admin/customer/index/edit/id/5001" in doc.source_url


def test_normalize_customer_source_id_is_sha256() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert len(doc.source_id) == 16
    assert doc.source_id == _stable_id(5001)


def test_normalize_customer_content_has_email() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "jane.doe@example.com" in doc.content


def test_normalize_customer_email_only_name() -> None:
    customer = {**SAMPLE_CUSTOMER, "firstname": "", "lastname": ""}
    doc = normalize_customer(customer, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "jane.doe@example.com" in doc.title


def test_normalize_customer_content_has_group() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert "Group ID" in doc.content


def test_normalize_customer_minimal() -> None:
    doc = normalize_customer({"id": 99}, CONNECTOR_ID, TENANT_ID, BASE_URL)
    assert isinstance(doc, ConnectorDocument)
    assert doc.source_id == _stable_id(99)


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_magento_error_base() -> None:
    exc = MagentoError("base error", status_code=500, code="server_error")
    assert str(exc) == "base error"
    assert exc.status_code == 500
    assert exc.code == "server_error"


def test_magento_auth_error_is_magento_error() -> None:
    exc = MagentoAuthError("auth error", 401)
    assert isinstance(exc, MagentoError)
    assert exc.status_code == 401


def test_magento_auth_error_403() -> None:
    exc = MagentoAuthError("forbidden", 403)
    assert exc.status_code == 403


def test_magento_rate_limit_error() -> None:
    exc = MagentoRateLimitError("rate limited", retry_after=5.0)
    assert isinstance(exc, MagentoError)
    assert exc.status_code == 429
    assert exc.retry_after == 5.0
    assert exc.code == "rate_limit"


def test_magento_rate_limit_error_default_retry() -> None:
    exc = MagentoRateLimitError("rate limited")
    assert exc.retry_after == 2.0


def test_magento_not_found_error() -> None:
    exc = MagentoNotFoundError("order", "12345")
    assert isinstance(exc, MagentoError)
    assert exc.status_code == 404
    assert "12345" in str(exc)
    assert "order" in str(exc)
    assert exc.code == "not_found"


def test_magento_network_error() -> None:
    exc = MagentoNetworkError("connection refused", 503)
    assert isinstance(exc, MagentoError)
    assert exc.status_code == 503


def test_all_exceptions_inherit_from_magento_error() -> None:
    for cls in [MagentoAuthError, MagentoNetworkError, MagentoRateLimitError]:
        assert issubclass(cls, MagentoError)


# ── with_retry() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value="ok")
    result = await with_retry(fn, max_attempts=3)
    assert result == "ok"
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    fn = AsyncMock(side_effect=[MagentoNetworkError("timeout"), "ok"])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == "ok"
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_auth_immediately() -> None:
    fn = AsyncMock(side_effect=MagentoAuthError("invalid token", 401))
    with pytest.raises(MagentoAuthError):
        await with_retry(fn, max_attempts=3)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=MagentoNetworkError("persistent error"))
    with pytest.raises(MagentoNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_honours_retry_after() -> None:
    fn = AsyncMock(side_effect=[MagentoRateLimitError("rate limit", retry_after=0), "ok"])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == "ok"


@pytest.mark.asyncio
async def test_with_retry_rate_limit_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=MagentoRateLimitError("persistent rate limit", retry_after=0))
    with pytest.raises(MagentoRateLimitError):
        await with_retry(fn, max_attempts=2, base_delay=0)
    assert fn.call_count == 2


# ── _stable_id() ──────────────────────────────────────────────────────────────


def test_stable_id_deterministic() -> None:
    assert _stable_id(12345) == _stable_id(12345)
    assert len(_stable_id(12345)) == 16


def test_stable_id_different_for_different_ids() -> None:
    assert _stable_id(12345) != _stable_id(67890)


def test_stable_id_string_input() -> None:
    assert _stable_id("WIDGET-PRO-001") == _stable_id("WIDGET-PRO-001")
    assert len(_stable_id("WIDGET-PRO-001")) == 16


def test_stable_id_string_vs_int() -> None:
    # "123" and 123 both convert to str("123") before hashing
    assert _stable_id("123") == _stable_id(123)


# ── Models ────────────────────────────────────────────────────────────────────


def test_connector_health_enum_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.FAILED == "failed"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_enum_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_connector_document_defaults() -> None:
    doc = ConnectorDocument(
        source_id="abc",
        title="Test",
        content="body",
        connector_id="conn1",
        tenant_id="tenant1",
    )
    assert doc.source_url == ""
    assert doc.metadata == {}


def test_connector_document_with_metadata() -> None:
    doc = ConnectorDocument(
        source_id="abc",
        title="Test",
        content="body",
        connector_id="conn1",
        tenant_id="tenant1",
        metadata={"sku": "TEST-001"},
    )
    assert doc.metadata["sku"] == "TEST-001"


# ── MagentoConnector lifecycle ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_cleans_up_client(authed: MagentoConnector) -> None:
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    authed.http_client = mock_client
    await authed.aclose()
    mock_client.aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_when_no_client() -> None:
    c = MagentoConnector(base_url=BASE_URL, access_token=ACCESS_TOKEN)
    c.http_client = None
    await c.aclose()  # should not raise


@pytest.mark.asyncio
async def test_context_manager() -> None:
    c = MagentoConnector(base_url=BASE_URL, access_token=ACCESS_TOKEN)
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    async with c:
        pass
    mock_client.aclose.assert_called_once()


def test_ensure_client_creates_if_none() -> None:
    c = MagentoConnector(base_url=BASE_URL, access_token=ACCESS_TOKEN)
    c.http_client = None
    client = c._ensure_client()
    assert client is not None
    assert c.http_client is client


def test_ensure_client_returns_existing() -> None:
    c = MagentoConnector(base_url=BASE_URL, access_token=ACCESS_TOKEN)
    existing = MagicMock()
    c.http_client = existing
    client = c._ensure_client()
    assert client is existing


# ── Pagination stop condition ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_stops_when_page_times_size_gte_total(authed: MagentoConnector) -> None:
    """With page_size=100 and total_count=100, one page fetch is sufficient."""
    orders = [SAMPLE_ORDER] * 100
    authed.http_client.list_orders = AsyncMock(return_value=_orders_page(orders, 100))
    authed.http_client.list_products = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    authed.http_client.list_customers = AsyncMock(return_value=PAGE_RESPONSE_EMPTY)
    result = await authed.sync(full=True)
    assert authed.http_client.list_orders.call_count == 1
    assert result.documents_found == 100
