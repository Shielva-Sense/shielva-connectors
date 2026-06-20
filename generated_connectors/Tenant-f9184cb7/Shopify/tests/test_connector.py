"""Unit tests for ShopifyConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import ShopifyConnector
from exceptions import ShopifyAuthError, ShopifyNetworkError, ShopifyRateLimitError, ShopifyNotFoundError, ShopifyError
from helpers.utils import normalize_order, normalize_product, normalize_customer, with_retry, _stable_id, _strip_html
from models import AuthStatus, ConnectorHealth, SyncStatus, ConnectorDocument

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_shopify_test_001"
SHOP_URL = "teststore.myshopify.com"
ACCESS_TOKEN = "shpat_test_abc123def456"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_ORDER: dict = {
    "id": 450789469,
    "order_number": 1001,
    "financial_status": "paid",
    "fulfillment_status": "fulfilled",
    "total_price": "199.00",
    "currency": "USD",
    "created_at": "2024-01-15T10:00:00-05:00",
    "email": "customer@example.com",
    "customer": {
        "id": 207119551,
        "first_name": "John",
        "last_name": "Smith",
        "email": "customer@example.com",
    },
    "line_items": [
        {"name": "Awesome Widget", "quantity": 2, "price": "89.50"},
        {"name": "Cool Gadget", "quantity": 1, "price": "20.00"},
    ],
}

SAMPLE_PRODUCT: dict = {
    "id": 632910392,
    "title": "IPod Nano - 8GB",
    "vendor": "Apple",
    "product_type": "Cult Products",
    "status": "active",
    "tags": "Emotive, Flash Memory, MP3, Music",
    "body_html": "<p>It's the <em>perfect</em> gift for <b>everyone</b>!</p>",
    "variants": [
        {"title": "Pink", "price": "199.00", "sku": "IPOD2008PINK"},
        {"title": "Red", "price": "199.00", "sku": "IPOD2008RED"},
        {"title": "Black", "price": "199.00", "sku": "IPOD2008BLACK"},
    ],
}

SAMPLE_CUSTOMER: dict = {
    "id": 207119551,
    "first_name": "John",
    "last_name": "Smith",
    "email": "customer@example.com",
    "phone": "+16135551111",
    "orders_count": 4,
    "total_spent": "398.00",
    "tags": "loyal, vip",
    "created_at": "2024-01-10T08:00:00-05:00",
    "accepts_marketing": True,
    "verified_email": True,
}

SAMPLE_SHOP: dict = {
    "shop": {
        "id": 548380009,
        "name": "Test Store",
        "email": "admin@test-store.myshopify.com",
        "domain": "teststore.myshopify.com",
    }
}

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> ShopifyConnector:
    c = ShopifyConnector(
        shop_url=SHOP_URL,
        access_token=ACCESS_TOKEN,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ── install() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = ShopifyConnector(shop_url=SHOP_URL, access_token=ACCESS_TOKEN, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    with patch("connector.ShopifyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_shop = AsyncMock(return_value=SAMPLE_SHOP)
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test Store" in result.message


@pytest.mark.asyncio
async def test_install_missing_shop_url() -> None:
    c = ShopifyConnector(shop_url="", access_token=ACCESS_TOKEN, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "shop_domain" in result.message


@pytest.mark.asyncio
async def test_install_missing_access_token() -> None:
    c = ShopifyConnector(shop_url=SHOP_URL, access_token="", connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_missing_both_credentials() -> None:
    c = ShopifyConnector(shop_url="", access_token="", connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "shop_domain" in result.message
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_invalid_token() -> None:
    c = ShopifyConnector(shop_url=SHOP_URL, access_token="bad_token", connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    with patch("connector.ShopifyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_shop = AsyncMock(side_effect=ShopifyAuthError("Invalid access token", 401))
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = ShopifyConnector(shop_url=SHOP_URL, access_token=ACCESS_TOKEN, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    with patch("connector.ShopifyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_shop = AsyncMock(side_effect=Exception("Connection refused"))
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ── health_check() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(authed: ShopifyConnector) -> None:
    with patch("connector.ShopifyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_shop = AsyncMock(return_value=SAMPLE_SHOP)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test Store" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: ShopifyConnector) -> None:
    with patch("connector.ShopifyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_shop = AsyncMock(side_effect=ShopifyAuthError("Invalid token", 401))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: ShopifyConnector) -> None:
    with patch("connector.ShopifyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_shop = AsyncMock(side_effect=ShopifyNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = ShopifyConnector(shop_url="", access_token="", connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── sync() ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(authed: ShopifyConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=([], None))
    authed.http_client.list_products = AsyncMock(return_value=([], None))
    authed.http_client.list_customers = AsyncMock(return_value=([], None))
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_data(authed: ShopifyConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=([SAMPLE_ORDER], None))
    authed.http_client.list_products = AsyncMock(return_value=([SAMPLE_PRODUCT], None))
    authed.http_client.list_customers = AsyncMock(return_value=([SAMPLE_CUSTOMER], None))
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_orders_pagination(authed: ShopifyConnector) -> None:
    order2 = {**SAMPLE_ORDER, "id": 999888777, "order_number": 1002}
    authed.http_client.list_orders = AsyncMock(
        side_effect=[([SAMPLE_ORDER], "cursor_abc"), ([order2], None)]
    )
    authed.http_client.list_products = AsyncMock(return_value=([], None))
    authed.http_client.list_customers = AsyncMock(return_value=([], None))
    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert authed.http_client.list_orders.call_count == 2


@pytest.mark.asyncio
async def test_sync_products_pagination(authed: ShopifyConnector) -> None:
    product2 = {**SAMPLE_PRODUCT, "id": 111222333, "title": "Second Product"}
    authed.http_client.list_orders = AsyncMock(return_value=([], None))
    authed.http_client.list_products = AsyncMock(
        side_effect=[([SAMPLE_PRODUCT], "cursor_xyz"), ([product2], None)]
    )
    authed.http_client.list_customers = AsyncMock(return_value=([], None))
    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert authed.http_client.list_products.call_count == 2


@pytest.mark.asyncio
async def test_sync_customers_pagination(authed: ShopifyConnector) -> None:
    customer2 = {**SAMPLE_CUSTOMER, "id": 555666777, "email": "other@example.com"}
    authed.http_client.list_orders = AsyncMock(return_value=([], None))
    authed.http_client.list_products = AsyncMock(return_value=([], None))
    authed.http_client.list_customers = AsyncMock(
        side_effect=[([SAMPLE_CUSTOMER], "cursor_def"), ([customer2], None)]
    )
    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert authed.http_client.list_customers.call_count == 2


@pytest.mark.asyncio
async def test_sync_orders_fail_returns_failed(authed: ShopifyConnector) -> None:
    authed.http_client.list_orders = AsyncMock(
        side_effect=ShopifyNetworkError("server error", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "Orders sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_products_fail_returns_partial(authed: ShopifyConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=([], None))
    authed.http_client.list_products = AsyncMock(
        side_effect=ShopifyNetworkError("server error", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert "Products sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_customers_fail_returns_partial(authed: ShopifyConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=([SAMPLE_ORDER], None))
    authed.http_client.list_products = AsyncMock(return_value=([], None))
    authed.http_client.list_customers = AsyncMock(
        side_effect=ShopifyNetworkError("server error", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert "Customers sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_with_since(authed: ShopifyConnector) -> None:
    from datetime import datetime, timezone
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    authed.http_client.list_orders = AsyncMock(return_value=([], None))
    authed.http_client.list_products = AsyncMock(return_value=([], None))
    authed.http_client.list_customers = AsyncMock(return_value=([], None))
    result = await authed.sync(full=False, since=since)
    assert result.status == SyncStatus.COMPLETED
    # Verify created_at_min was passed to list_orders (arg index 5: shop, token, limit, page_info, status, created_at_min)
    call_args = authed.http_client.list_orders.call_args
    assert "2024-01-01" in call_args.args[5]  # created_at_min arg


@pytest.mark.asyncio
async def test_sync_partial_failure_in_normalization(authed: ShopifyConnector) -> None:
    bad_order = {"id": None, "order_number": None}  # will cause normalization issues
    authed.http_client.list_orders = AsyncMock(return_value=([bad_order, SAMPLE_ORDER], None))
    authed.http_client.list_products = AsyncMock(return_value=([], None))
    authed.http_client.list_customers = AsyncMock(return_value=([], None))
    # Patch normalize_order to raise on first call
    call_count = {"n": 0}
    original_normalize = normalize_order
    def patched_normalize(order, connector_id, tenant_id, shop_url):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("normalize failed")
        return original_normalize(order, connector_id, tenant_id, shop_url)
    with patch("connector.normalize_order", side_effect=patched_normalize):
        result = await authed.sync(full=True)
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


# ── list/get API methods ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_orders_single_page(authed: ShopifyConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=([SAMPLE_ORDER], None))
    result = await authed.list_orders()
    assert len(result) == 1
    assert result[0]["id"] == 450789469


@pytest.mark.asyncio
async def test_list_orders_multi_page(authed: ShopifyConnector) -> None:
    order2 = {**SAMPLE_ORDER, "id": 111}
    authed.http_client.list_orders = AsyncMock(
        side_effect=[([SAMPLE_ORDER], "page2"), ([order2], None)]
    )
    result = await authed.list_orders(limit=1)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_order(authed: ShopifyConnector) -> None:
    authed.http_client.get_order = AsyncMock(return_value={"order": SAMPLE_ORDER})
    result = await authed.get_order(450789469)
    assert result["order"]["id"] == 450789469


@pytest.mark.asyncio
async def test_get_order_not_found(authed: ShopifyConnector) -> None:
    authed.http_client.get_order = AsyncMock(
        side_effect=ShopifyNotFoundError("order", "99999")
    )
    with pytest.raises(ShopifyNotFoundError):
        await authed.get_order(99999)


@pytest.mark.asyncio
async def test_list_products_single_page(authed: ShopifyConnector) -> None:
    authed.http_client.list_products = AsyncMock(return_value=([SAMPLE_PRODUCT], None))
    result = await authed.list_products()
    assert len(result) == 1
    assert result[0]["id"] == 632910392


@pytest.mark.asyncio
async def test_list_products_multi_page(authed: ShopifyConnector) -> None:
    product2 = {**SAMPLE_PRODUCT, "id": 222}
    authed.http_client.list_products = AsyncMock(
        side_effect=[([SAMPLE_PRODUCT], "pageB"), ([product2], None)]
    )
    result = await authed.list_products(limit=1)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_product(authed: ShopifyConnector) -> None:
    authed.http_client.get_product = AsyncMock(return_value={"product": SAMPLE_PRODUCT})
    result = await authed.get_product(632910392)
    assert result["product"]["title"] == "IPod Nano - 8GB"


@pytest.mark.asyncio
async def test_get_product_not_found(authed: ShopifyConnector) -> None:
    authed.http_client.get_product = AsyncMock(
        side_effect=ShopifyNotFoundError("product", "99999")
    )
    with pytest.raises(ShopifyNotFoundError):
        await authed.get_product(99999)


@pytest.mark.asyncio
async def test_list_customers_single_page(authed: ShopifyConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value=([SAMPLE_CUSTOMER], None))
    result = await authed.list_customers()
    assert len(result) == 1
    assert result[0]["id"] == 207119551


@pytest.mark.asyncio
async def test_list_customers_multi_page(authed: ShopifyConnector) -> None:
    customer2 = {**SAMPLE_CUSTOMER, "id": 333}
    authed.http_client.list_customers = AsyncMock(
        side_effect=[([SAMPLE_CUSTOMER], "pageC"), ([customer2], None)]
    )
    result = await authed.list_customers(limit=1)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_customer(authed: ShopifyConnector) -> None:
    authed.http_client.get_customer = AsyncMock(return_value={"customer": SAMPLE_CUSTOMER})
    result = await authed.get_customer(207119551)
    assert result["customer"]["email"] == "customer@example.com"


@pytest.mark.asyncio
async def test_get_customer_not_found(authed: ShopifyConnector) -> None:
    authed.http_client.get_customer = AsyncMock(
        side_effect=ShopifyNotFoundError("customer", "99999")
    )
    with pytest.raises(ShopifyNotFoundError):
        await authed.get_customer(99999)


# ── normalize_order() ───────────────────────────────────────────────────────


def test_normalize_order_basic() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert isinstance(doc, ConnectorDocument)
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "Order #1001" in doc.title
    assert "John Smith" in doc.title
    assert doc.metadata["order_id"] == 450789469
    assert doc.metadata["order_number"] == 1001
    assert doc.metadata["financial_status"] == "paid"
    assert doc.metadata["fulfillment_status"] == "fulfilled"
    assert doc.metadata["total_price"] == "199.00"
    assert doc.metadata["currency"] == "USD"
    assert doc.metadata["customer_email"] == "customer@example.com"
    assert "teststore.myshopify.com" in doc.source_url
    assert "450789469" in doc.source_url


def test_normalize_order_source_id_is_sha256() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert len(doc.source_id) == 16
    assert doc.source_id == _stable_id(450789469)


def test_normalize_order_content_has_line_items() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert "Awesome Widget" in doc.content
    assert "Cool Gadget" in doc.content


def test_normalize_order_guest_customer() -> None:
    order = {**SAMPLE_ORDER, "customer": None, "email": "guest@example.com"}
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert "guest@example.com" in doc.title or "guest@example.com" in doc.content


def test_normalize_order_shop_url_with_https() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, "https://teststore.myshopify.com")
    assert "https://teststore.myshopify.com" in doc.source_url


def test_normalize_order_unfulfilled() -> None:
    order = {**SAMPLE_ORDER, "fulfillment_status": None}
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert doc.metadata["fulfillment_status"] == "unfulfilled"


# ── normalize_product() ─────────────────────────────────────────────────────


def test_normalize_product_basic() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert isinstance(doc, ConnectorDocument)
    assert "IPod Nano - 8GB" in doc.title
    assert "Apple" in doc.title
    assert doc.metadata["product_id"] == 632910392
    assert doc.metadata["vendor"] == "Apple"
    assert doc.metadata["product_type"] == "Cult Products"
    assert doc.metadata["status"] == "active"
    assert doc.metadata["variants_count"] == 3
    assert "teststore.myshopify.com" in doc.source_url
    assert "632910392" in doc.source_url


def test_normalize_product_source_id_is_sha256() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert len(doc.source_id) == 16
    assert doc.source_id == _stable_id(632910392)


def test_normalize_product_strips_html() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert "<p>" not in doc.content
    assert "<em>" not in doc.content
    assert "perfect" in doc.content


def test_normalize_product_variants_in_content() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert "Pink" in doc.content
    assert "IPOD2008PINK" in doc.content


def test_normalize_product_no_vendor() -> None:
    product = {**SAMPLE_PRODUCT, "vendor": ""}
    doc = normalize_product(product, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert doc.title == "IPod Nano - 8GB"


def test_normalize_product_no_body_html() -> None:
    product = {**SAMPLE_PRODUCT, "body_html": None}
    doc = normalize_product(product, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert "Description:" not in doc.content


def test_normalize_product_tags_in_metadata() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert "Emotive" in doc.metadata["tags"]


# ── normalize_customer() ────────────────────────────────────────────────────


def test_normalize_customer_basic() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert isinstance(doc, ConnectorDocument)
    assert "John Smith" in doc.title
    assert "Customer:" in doc.title
    assert doc.metadata["customer_id"] == 207119551
    assert doc.metadata["email"] == "customer@example.com"
    assert doc.metadata["orders_count"] == 4
    assert doc.metadata["total_spent"] == "398.00"
    assert doc.metadata["created_at"] == "2024-01-10T08:00:00-05:00"
    assert "teststore.myshopify.com" in doc.source_url
    assert "207119551" in doc.source_url


def test_normalize_customer_source_id_is_sha256() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert len(doc.source_id) == 16
    assert doc.source_id == _stable_id(207119551)


def test_normalize_customer_phone_in_content() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert "+16135551111" in doc.content


def test_normalize_customer_no_phone() -> None:
    customer = {**SAMPLE_CUSTOMER, "phone": None}
    doc = normalize_customer(customer, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert "Phone:" not in doc.content


def test_normalize_customer_tags_in_metadata() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert "loyal" in doc.metadata["tags"]


def test_normalize_customer_email_only_name() -> None:
    customer = {**SAMPLE_CUSTOMER, "first_name": "", "last_name": ""}
    doc = normalize_customer(customer, CONNECTOR_ID, TENANT_ID, SHOP_URL)
    assert "customer@example.com" in doc.title


# ── Exception hierarchy ──────────────────────────────────────────────────────


def test_shopify_error_base() -> None:
    exc = ShopifyError("base error", status_code=500, code="server_error")
    assert str(exc) == "base error"
    assert exc.status_code == 500
    assert exc.code == "server_error"


def test_shopify_auth_error_is_shopify_error() -> None:
    exc = ShopifyAuthError("auth error", 401)
    assert isinstance(exc, ShopifyError)
    assert exc.status_code == 401


def test_shopify_rate_limit_error() -> None:
    exc = ShopifyRateLimitError("rate limited", retry_after=5.0)
    assert isinstance(exc, ShopifyError)
    assert exc.status_code == 429
    assert exc.retry_after == 5.0
    assert exc.code == "rate_limit"


def test_shopify_rate_limit_error_default_retry() -> None:
    exc = ShopifyRateLimitError("rate limited")
    assert exc.retry_after == 2.0


def test_shopify_not_found_error() -> None:
    exc = ShopifyNotFoundError("order", "12345")
    assert isinstance(exc, ShopifyError)
    assert exc.status_code == 404
    assert "12345" in str(exc)
    assert "order" in str(exc)


def test_shopify_network_error() -> None:
    exc = ShopifyNetworkError("connection refused", 503)
    assert isinstance(exc, ShopifyError)
    assert exc.status_code == 503


# ── with_retry() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value="ok")
    result = await with_retry(fn, max_attempts=3)
    assert result == "ok"
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    fn = AsyncMock(side_effect=[ShopifyNetworkError("timeout"), "ok"])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == "ok"
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_auth_immediately() -> None:
    fn = AsyncMock(side_effect=ShopifyAuthError("invalid token", 401))
    with pytest.raises(ShopifyAuthError):
        await with_retry(fn, max_attempts=3)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=ShopifyNetworkError("persistent error"))
    with pytest.raises(ShopifyNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_honours_retry_after() -> None:
    import asyncio
    fn = AsyncMock(side_effect=[ShopifyRateLimitError("rate limit", retry_after=0), "ok"])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == "ok"


@pytest.mark.asyncio
async def test_with_retry_rate_limit_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=ShopifyRateLimitError("persistent rate limit", retry_after=0))
    with pytest.raises(ShopifyRateLimitError):
        await with_retry(fn, max_attempts=2, base_delay=0)
    assert fn.call_count == 2


# ── _stable_id() and _strip_html() ─────────────────────────────────────────


def test_stable_id_deterministic() -> None:
    assert _stable_id(12345) == _stable_id(12345)
    assert len(_stable_id(12345)) == 16


def test_stable_id_different_for_different_ids() -> None:
    assert _stable_id(12345) != _stable_id(67890)


def test_stable_id_string_input() -> None:
    assert _stable_id("abc") == _stable_id("abc")
    assert len(_stable_id("abc")) == 16


def test_strip_html_removes_tags() -> None:
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_html_empty_string() -> None:
    assert _strip_html("") == ""


def test_strip_html_no_tags() -> None:
    assert _strip_html("plain text") == "plain text"


def test_strip_html_nested_tags() -> None:
    result = _strip_html("<div><span>nested</span></div>")
    assert result == "nested"


# ── HTTP client: Link header parsing ────────────────────────────────────────


def test_parse_next_page_info_with_next_rel() -> None:
    from client.http_client import _parse_next_page_info
    link = '<https://mystore.myshopify.com/admin/api/2024-01/orders.json?page_info=abc123&limit=100>; rel="next"'
    result = _parse_next_page_info(link)
    assert result == "abc123"


def test_parse_next_page_info_no_next_rel() -> None:
    from client.http_client import _parse_next_page_info
    link = '<https://mystore.myshopify.com/admin/api/2024-01/orders.json?page_info=prev456&limit=100>; rel="previous"'
    result = _parse_next_page_info(link)
    assert result is None


def test_parse_next_page_info_none_header() -> None:
    from client.http_client import _parse_next_page_info
    result = _parse_next_page_info(None)
    assert result is None


def test_parse_next_page_info_both_rels() -> None:
    from client.http_client import _parse_next_page_info
    link = (
        '<https://mystore.myshopify.com/admin/api/2024-01/orders.json?page_info=prev456&limit=100>; rel="previous", '
        '<https://mystore.myshopify.com/admin/api/2024-01/orders.json?page_info=next789&limit=100>; rel="next"'
    )
    result = _parse_next_page_info(link)
    assert result == "next789"


# ── Models ──────────────────────────────────────────────────────────────────


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
        metadata={"key": "value"},
    )
    assert doc.metadata["key"] == "value"


# ── ShopifyConnector lifecycle ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_cleans_up_client(authed: ShopifyConnector) -> None:
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    authed.http_client = mock_client
    await authed.aclose()
    mock_client.aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_when_no_client() -> None:
    c = ShopifyConnector(shop_url=SHOP_URL, access_token=ACCESS_TOKEN)
    c.http_client = None
    await c.aclose()  # should not raise


@pytest.mark.asyncio
async def test_context_manager() -> None:
    c = ShopifyConnector(shop_url=SHOP_URL, access_token=ACCESS_TOKEN)
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    async with c:
        pass
    mock_client.aclose.assert_called_once()


def test_ensure_client_creates_if_none() -> None:
    c = ShopifyConnector(shop_url=SHOP_URL, access_token=ACCESS_TOKEN)
    c.http_client = None
    client = c._ensure_client()
    assert client is not None
    assert c.http_client is client


# ── shop_domain config key (ACP install_fields key) ─────────────────────────


def test_connector_accepts_shop_domain_config() -> None:
    """Config dict keyed by 'shop_domain' (ACP install_fields key) must be accepted."""
    c = ShopifyConnector(config={"shop_domain": SHOP_URL, "access_token": ACCESS_TOKEN})
    assert c._shop_url == SHOP_URL
    assert c._access_token == ACCESS_TOKEN


def test_connector_shop_domain_takes_precedence_over_shop_url() -> None:
    """When both keys are present, shop_domain wins."""
    c = ShopifyConnector(config={
        "shop_domain": "domain-store.myshopify.com",
        "shop_url": "url-store.myshopify.com",
        "access_token": ACCESS_TOKEN,
    })
    assert c._shop_url == "domain-store.myshopify.com"


@pytest.mark.asyncio
async def test_install_with_shop_domain_config() -> None:
    """install() works when config uses shop_domain key."""
    c = ShopifyConnector(config={"shop_domain": SHOP_URL, "access_token": ACCESS_TOKEN})
    with patch("connector.ShopifyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_shop = AsyncMock(return_value=SAMPLE_SHOP)
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


# ── list_collections ─────────────────────────────────────────────────────────

SAMPLE_COLLECTION: dict = {
    "id": 841564295,
    "title": "IPods",
    "body_html": "<p>The best portable music players.</p>",
    "published_at": "2024-01-01T00:00:00-05:00",
    "sort_order": "manual",
    "template_suffix": None,
    "handle": "ipods",
}


@pytest.mark.asyncio
async def test_list_collections_single_page(authed: ShopifyConnector) -> None:
    """list_collections via HTTP client returns collections list."""
    from client.http_client import ShopifyHTTPClient
    client = ShopifyHTTPClient()
    with patch.object(client, "_request", new=AsyncMock(return_value=(
        {"custom_collections": [SAMPLE_COLLECTION]},
        {},
    ))):
        collections, next_pi = await client.list_collections(SHOP_URL, ACCESS_TOKEN)
    assert len(collections) == 1
    assert collections[0]["id"] == 841564295
    assert next_pi is None


@pytest.mark.asyncio
async def test_list_collections_with_page_info(authed: ShopifyConnector) -> None:
    """list_collections passes page_info and parses Link header."""
    from client.http_client import ShopifyHTTPClient
    client = ShopifyHTTPClient()
    link_header = '<https://mystore.myshopify.com/admin/api/2024-01/custom_collections.json?page_info=col_next&limit=250>; rel="next"'
    with patch.object(client, "_request", new=AsyncMock(return_value=(
        {"custom_collections": [SAMPLE_COLLECTION]},
        {"Link": link_header},
    ))):
        collections, next_pi = await client.list_collections(SHOP_URL, ACCESS_TOKEN, page_info="col_prev")
    assert next_pi == "col_next"
