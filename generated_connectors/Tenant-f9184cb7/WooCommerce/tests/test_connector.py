"""Unit tests for WooCommerceConnector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import WooCommerceConnector
from exceptions import (
    WooCommerceAuthError,
    WooCommerceNetworkError,
    WooCommerceNotFoundError,
    WooCommerceRateLimitError,
)
from helpers.utils import (
    normalize_customer,
    normalize_order,
    normalize_product,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_woo_test_001"
STORE_URL = "https://mystore.example.com"
CONSUMER_KEY = "ck_test_abc123"
CONSUMER_SECRET = "cs_test_xyz789"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_ORDER: dict = {
    "id": 101,
    "number": "101",
    "status": "processing",
    "currency": "USD",
    "total": "49.99",
    "payment_method": "stripe",
    "payment_method_title": "Stripe",
    "date_created": "2026-06-01T10:00:00",
    "billing": {
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@example.com",
    },
    "line_items": [
        {"name": "Widget Pro", "quantity": 2, "subtotal": "29.98"},
        {"name": "Gizmo Basic", "quantity": 1, "subtotal": "20.01"},
    ],
}

SAMPLE_PRODUCT: dict = {
    "id": 55,
    "name": "Widget Pro",
    "type": "simple",
    "status": "publish",
    "price": "14.99",
    "regular_price": "14.99",
    "sku": "WDGT-PRO-01",
    "stock_quantity": 42,
    "permalink": "https://mystore.example.com/product/widget-pro/",
    "categories": [{"id": 9, "name": "Widgets"}, {"id": 10, "name": "Tools"}],
    "description": "<p>The best widget on the market.</p>",
    "short_description": "<p>Widget Pro — all you need.</p>",
}

SAMPLE_CUSTOMER: dict = {
    "id": 7,
    "first_name": "Bob",
    "last_name": "Smith",
    "email": "bob@example.com",
    "username": "bobsmith",
    "orders_count": 5,
    "total_spent": "249.95",
    "date_created": "2025-01-15T08:00:00",
    "billing": {"phone": "+15005550123", "city": "Austin", "country": "US"},
}

SYSTEM_STATUS: dict = {
    "environment": {"wp_version": "6.5.3"},
}

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> WooCommerceConnector:
    """Connector with credentials set and HTTP client mocked."""
    c = WooCommerceConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        store_url=STORE_URL,
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
    )
    c._http = MagicMock()
    return c


@pytest.fixture()
def no_creds_connector() -> WooCommerceConnector:
    """Connector with no credentials."""
    return WooCommerceConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)


# ═══════════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector: WooCommerceConnector) -> None:
    connector._http.get_system_status = AsyncMock(return_value=SYSTEM_STATUS)
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "6.5.3" in result.message


@pytest.mark.asyncio
async def test_install_missing_all_credentials() -> None:
    c = WooCommerceConnector()
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "store_url" in result.message


@pytest.mark.asyncio
async def test_install_missing_site_url() -> None:
    c = WooCommerceConnector(consumer_key="ck_abc", consumer_secret="cs_xyz")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "site_url" in result.message


@pytest.mark.asyncio
async def test_install_missing_consumer_key() -> None:
    c = WooCommerceConnector(store_url=STORE_URL, consumer_secret="cs_xyz")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector: WooCommerceConnector) -> None:
    connector._http.get_system_status = AsyncMock(
        side_effect=WooCommerceAuthError("Authentication failed (401): Invalid API credentials", 401)
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Authentication failed" in result.message


@pytest.mark.asyncio
async def test_install_forbidden(connector: WooCommerceConnector) -> None:
    connector._http.get_system_status = AsyncMock(
        side_effect=WooCommerceAuthError("Authentication failed (403): Forbidden", 403)
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error(connector: WooCommerceConnector) -> None:
    connector._http.get_system_status = AsyncMock(
        side_effect=WooCommerceNetworkError("Connection refused")
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED
    assert "Connection refused" in result.message


@pytest.mark.asyncio
async def test_install_unexpected_exception(connector: WooCommerceConnector) -> None:
    connector._http.get_system_status = AsyncMock(side_effect=RuntimeError("boom"))
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(connector: WooCommerceConnector) -> None:
    connector._http.get_system_status = AsyncMock(return_value=SYSTEM_STATUS)
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = WooCommerceConnector()
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: WooCommerceConnector) -> None:
    connector._http.get_system_status = AsyncMock(
        side_effect=WooCommerceAuthError("Invalid key", 401)
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: WooCommerceConnector) -> None:
    connector._http.get_system_status = AsyncMock(
        side_effect=WooCommerceNetworkError("timeout")
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: WooCommerceConnector) -> None:
    connector._http.get_system_status = AsyncMock(side_effect=Exception("unexpected"))
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty_store(connector: WooCommerceConnector) -> None:
    connector._http.list_orders = AsyncMock(return_value=([], {}))
    connector._http.list_products = AsyncMock(return_value=([], {}))
    connector._http.list_customers = AsyncMock(return_value=([], {}))
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_orders(connector: WooCommerceConnector) -> None:
    connector._http.list_orders = AsyncMock(
        return_value=([SAMPLE_ORDER], {"X-WP-TotalPages": "1"})
    )
    connector._http.list_products = AsyncMock(return_value=([], {}))
    connector._http.list_customers = AsyncMock(return_value=([], {}))
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_with_products(connector: WooCommerceConnector) -> None:
    connector._http.list_orders = AsyncMock(return_value=([], {}))
    connector._http.list_products = AsyncMock(
        return_value=([SAMPLE_PRODUCT], {"X-WP-TotalPages": "1"})
    )
    connector._http.list_customers = AsyncMock(return_value=([], {}))
    result = await connector.sync(full=True)
    assert result.documents_found == 1
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_with_customers(connector: WooCommerceConnector) -> None:
    connector._http.list_orders = AsyncMock(return_value=([], {}))
    connector._http.list_products = AsyncMock(return_value=([], {}))
    connector._http.list_customers = AsyncMock(
        return_value=([SAMPLE_CUSTOMER], {"X-WP-TotalPages": "1"})
    )
    result = await connector.sync(full=True)
    assert result.documents_found == 1
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_all_resources(connector: WooCommerceConnector) -> None:
    connector._http.list_orders = AsyncMock(
        return_value=([SAMPLE_ORDER], {"X-WP-TotalPages": "1"})
    )
    connector._http.list_products = AsyncMock(
        return_value=([SAMPLE_PRODUCT], {"X-WP-TotalPages": "1"})
    )
    connector._http.list_customers = AsyncMock(
        return_value=([SAMPLE_CUSTOMER], {"X-WP-TotalPages": "1"})
    )
    result = await connector.sync(full=True)
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_pagination_orders(connector: WooCommerceConnector) -> None:
    """Two pages of orders, 1 item each, TotalPages=2."""
    order2 = {**SAMPLE_ORDER, "id": 102, "number": "102"}
    connector._http.list_orders = AsyncMock(
        side_effect=[
            ([SAMPLE_ORDER], {"X-WP-TotalPages": "2"}),
            ([order2], {"X-WP-TotalPages": "2"}),
        ]
    )
    connector._http.list_products = AsyncMock(return_value=([], {}))
    connector._http.list_customers = AsyncMock(return_value=([], {}))
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert connector._http.list_orders.call_count == 2


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(connector: WooCommerceConnector) -> None:
    connector._http.list_orders = AsyncMock(
        return_value=([SAMPLE_ORDER], {"X-WP-TotalPages": "1"})
    )
    connector._http.list_products = AsyncMock(return_value=([], {}))
    connector._http.list_customers = AsyncMock(return_value=([], {}))
    connector._ingest_document = AsyncMock()
    result = await connector.sync(full=True, kb_id="kb_test")
    connector._ingest_document.assert_awaited_once()
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_partial_failure(connector: WooCommerceConnector) -> None:
    """One good order + one bad item that fails normalization."""
    bad_order: dict = {}  # empty dict — normalize_order will raise on missing data
    connector._http.list_orders = AsyncMock(
        return_value=([SAMPLE_ORDER, bad_order], {"X-WP-TotalPages": "1"})
    )
    connector._http.list_products = AsyncMock(return_value=([], {}))
    connector._http.list_customers = AsyncMock(return_value=([], {}))

    # Patch normalize_order to raise on the second call
    call_count = [0]
    original_normalize = normalize_order

    def patched_normalize(raw, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_count[0] += 1
        if call_count[0] == 2:
            raise ValueError("Normalization error")
        return original_normalize(raw, *args, **kwargs)

    with patch("connector.normalize_order", side_effect=patched_normalize):
        result = await connector.sync(full=True)

    assert result.documents_found == 2
    assert result.documents_synced == 1
    assert result.documents_failed == 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_incremental_passes_modified_after(connector: WooCommerceConnector) -> None:
    from datetime import datetime, timezone
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    connector._http.list_orders = AsyncMock(return_value=([], {}))
    connector._http.list_products = AsyncMock(return_value=([], {}))
    connector._http.list_customers = AsyncMock(return_value=([], {}))
    await connector.sync(full=False, since=since)
    call_kwargs = connector._http.list_orders.call_args
    assert "modified_after" in call_kwargs.kwargs
    assert "2026-01-01" in call_kwargs.kwargs["modified_after"]


@pytest.mark.asyncio
async def test_sync_full_no_modified_after(connector: WooCommerceConnector) -> None:
    connector._http.list_orders = AsyncMock(return_value=([], {}))
    connector._http.list_products = AsyncMock(return_value=([], {}))
    connector._http.list_customers = AsyncMock(return_value=([], {}))
    await connector.sync(full=True)
    call_kwargs = connector._http.list_orders.call_args
    assert call_kwargs.kwargs.get("modified_after") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Direct API methods
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_orders(connector: WooCommerceConnector) -> None:
    connector._http.list_orders = AsyncMock(
        return_value=([SAMPLE_ORDER], {"X-WP-TotalPages": "1"})
    )
    result = await connector.list_orders()
    assert len(result) == 1
    assert result[0]["id"] == 101


@pytest.mark.asyncio
async def test_list_orders_with_status(connector: WooCommerceConnector) -> None:
    connector._http.list_orders = AsyncMock(return_value=([], {}))
    await connector.list_orders(status="completed")
    call_kwargs = connector._http.list_orders.call_args
    assert call_kwargs.kwargs["status"] == "completed"


@pytest.mark.asyncio
async def test_get_order(connector: WooCommerceConnector) -> None:
    connector._http.get_order = AsyncMock(return_value=SAMPLE_ORDER)
    result = await connector.get_order(101)
    assert result["id"] == 101
    assert result["status"] == "processing"


@pytest.mark.asyncio
async def test_get_order_not_found(connector: WooCommerceConnector) -> None:
    connector._http.get_order = AsyncMock(
        side_effect=WooCommerceNotFoundError("order", 9999)
    )
    with pytest.raises(WooCommerceNotFoundError):
        await connector.get_order(9999)


@pytest.mark.asyncio
async def test_list_products(connector: WooCommerceConnector) -> None:
    connector._http.list_products = AsyncMock(
        return_value=([SAMPLE_PRODUCT], {"X-WP-TotalPages": "1"})
    )
    result = await connector.list_products()
    assert len(result) == 1
    assert result[0]["name"] == "Widget Pro"


@pytest.mark.asyncio
async def test_list_products_with_status(connector: WooCommerceConnector) -> None:
    connector._http.list_products = AsyncMock(return_value=([], {}))
    await connector.list_products(status="draft")
    call_kwargs = connector._http.list_products.call_args
    assert call_kwargs.kwargs["status"] == "draft"


@pytest.mark.asyncio
async def test_get_product(connector: WooCommerceConnector) -> None:
    connector._http.get_product = AsyncMock(return_value=SAMPLE_PRODUCT)
    result = await connector.get_product(55)
    assert result["id"] == 55
    assert result["sku"] == "WDGT-PRO-01"


@pytest.mark.asyncio
async def test_get_product_not_found(connector: WooCommerceConnector) -> None:
    connector._http.get_product = AsyncMock(
        side_effect=WooCommerceNotFoundError("product", 9999)
    )
    with pytest.raises(WooCommerceNotFoundError):
        await connector.get_product(9999)


@pytest.mark.asyncio
async def test_list_customers(connector: WooCommerceConnector) -> None:
    connector._http.list_customers = AsyncMock(
        return_value=([SAMPLE_CUSTOMER], {"X-WP-TotalPages": "1"})
    )
    result = await connector.list_customers()
    assert len(result) == 1
    assert result[0]["email"] == "bob@example.com"


@pytest.mark.asyncio
async def test_list_customers_with_modified_after(connector: WooCommerceConnector) -> None:
    connector._http.list_customers = AsyncMock(return_value=([], {}))
    await connector.list_customers(modified_after="2026-01-01T00:00:00")
    call_kwargs = connector._http.list_customers.call_args
    assert call_kwargs.kwargs["modified_after"] == "2026-01-01T00:00:00"


@pytest.mark.asyncio
async def test_get_customer(connector: WooCommerceConnector) -> None:
    connector._http.get_customer = AsyncMock(return_value=SAMPLE_CUSTOMER)
    result = await connector.get_customer(7)
    assert result["id"] == 7
    assert result["email"] == "bob@example.com"


@pytest.mark.asyncio
async def test_get_customer_not_found(connector: WooCommerceConnector) -> None:
    connector._http.get_customer = AsyncMock(
        side_effect=WooCommerceNotFoundError("customer", 9999)
    )
    with pytest.raises(WooCommerceNotFoundError):
        await connector.get_customer(9999)


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_order
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_order_basic() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert doc.title == "Order #101: Jane Doe"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert doc.metadata["order_id"] == 101
    assert doc.metadata["status"] == "processing"
    assert doc.metadata["total"] == "49.99"
    assert doc.metadata["currency"] == "USD"
    assert doc.metadata["billing_email"] == "jane@example.com"
    assert "wp-admin/post.php" in doc.source_url
    assert "101" in doc.source_url


def test_normalize_order_stable_id() -> None:
    doc1 = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    doc2 = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert doc1.source_id == doc2.source_id
    expected = hashlib.sha256(b"woocommerce_order:101").hexdigest()[:16]
    assert doc1.source_id == expected


def test_normalize_order_line_items_in_content() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "Widget Pro" in doc.content
    assert "Gizmo Basic" in doc.content
    assert "x2" in doc.content


def test_normalize_order_payment_method() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert doc.metadata["payment_method"] == "Stripe"


def test_normalize_order_no_line_items() -> None:
    order = {**SAMPLE_ORDER, "line_items": []}
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "Line Items" not in doc.content


def test_normalize_order_missing_billing_name() -> None:
    order = {**SAMPLE_ORDER, "billing": {"email": "anon@example.com"}}
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "Unknown" in doc.title


def test_normalize_order_date_created() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert doc.metadata["date_created"] == "2026-06-01T10:00:00"


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_product
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_product_basic() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert doc.title == "Widget Pro (simple)"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert doc.metadata["product_id"] == 55
    assert doc.metadata["sku"] == "WDGT-PRO-01"
    assert doc.metadata["price"] == "14.99"
    assert doc.metadata["stock_quantity"] == 42
    assert "Widgets" in doc.metadata["categories"]
    assert "Tools" in doc.metadata["categories"]


def test_normalize_product_stable_id() -> None:
    doc1 = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_URL)
    doc2 = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert doc1.source_id == doc2.source_id
    expected = hashlib.sha256(b"woocommerce_product:55").hexdigest()[:16]
    assert doc1.source_id == expected


def test_normalize_product_permalink() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert doc.source_url == "https://mystore.example.com/product/widget-pro/"


def test_normalize_product_strips_html() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "<p>" not in doc.content
    assert "best widget" in doc.content


def test_normalize_product_categories_in_content() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "Widgets" in doc.content
    assert "Tools" in doc.content


def test_normalize_product_no_stock() -> None:
    product = {**SAMPLE_PRODUCT, "stock_quantity": None}
    doc = normalize_product(product, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "Stock Quantity" not in doc.content


def test_normalize_product_no_sku() -> None:
    product = {**SAMPLE_PRODUCT, "sku": ""}
    doc = normalize_product(product, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "SKU: N/A" in doc.content


def test_normalize_product_status_in_metadata() -> None:
    doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert doc.metadata["status"] == "publish"


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_customer
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_customer_basic() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert doc.title == "Customer: Bob Smith"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert doc.metadata["customer_id"] == 7
    assert doc.metadata["email"] == "bob@example.com"
    assert doc.metadata["orders_count"] == 5
    assert doc.metadata["total_spent"] == "249.95"
    assert doc.metadata["date_created"] == "2025-01-15T08:00:00"


def test_normalize_customer_stable_id() -> None:
    doc1 = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    doc2 = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert doc1.source_id == doc2.source_id
    expected = hashlib.sha256(b"woocommerce_customer:7").hexdigest()[:16]
    assert doc1.source_id == expected


def test_normalize_customer_source_url() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "user-edit.php?user_id=7" in doc.source_url


def test_normalize_customer_phone_in_content() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "+15005550123" in doc.content


def test_normalize_customer_location_in_content() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "Austin" in doc.content
    assert "US" in doc.content


def test_normalize_customer_no_name_falls_back_to_username() -> None:
    customer = {**SAMPLE_CUSTOMER, "first_name": "", "last_name": ""}
    doc = normalize_customer(customer, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "bobsmith" in doc.title


def test_normalize_customer_no_name_or_username() -> None:
    customer = {**SAMPLE_CUSTOMER, "first_name": "", "last_name": "", "username": ""}
    doc = normalize_customer(customer, CONNECTOR_ID, TENANT_ID, STORE_URL)
    assert "Unknown" in doc.title


# ═══════════════════════════════════════════════════════════════════════════════
# Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════


def test_exception_hierarchy_auth() -> None:
    from exceptions import WooCommerceError
    exc = WooCommerceAuthError("auth failed", 401)
    assert isinstance(exc, WooCommerceError)
    assert exc.status_code == 401


def test_exception_hierarchy_network() -> None:
    from exceptions import WooCommerceError
    exc = WooCommerceNetworkError("timeout", 503)
    assert isinstance(exc, WooCommerceError)
    assert exc.status_code == 503


def test_exception_hierarchy_rate_limit() -> None:
    from exceptions import WooCommerceError
    exc = WooCommerceRateLimitError("rate limited", retry_after=30.0)
    assert isinstance(exc, WooCommerceError)
    assert exc.status_code == 429
    assert exc.retry_after == 30.0
    assert exc.code == "rate_limit"


def test_exception_hierarchy_not_found() -> None:
    from exceptions import WooCommerceError
    exc = WooCommerceNotFoundError("order", 999)
    assert isinstance(exc, WooCommerceError)
    assert exc.status_code == 404
    assert "999" in str(exc)


def test_exception_code_field() -> None:
    exc = WooCommerceAuthError("bad key", 401, "woocommerce_rest_cannot_view")
    assert exc.code == "woocommerce_rest_cannot_view"


# ═══════════════════════════════════════════════════════════════════════════════
# with_retry
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    fn = AsyncMock(side_effect=[
        WooCommerceNetworkError("timeout"),
        WooCommerceNetworkError("timeout"),
        {"ok": True},
    ])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    fn = AsyncMock(side_effect=WooCommerceAuthError("bad key", 401))
    with pytest.raises(WooCommerceAuthError):
        await with_retry(fn, max_attempts=3)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=WooCommerceNetworkError("timeout"))
    with pytest.raises(WooCommerceNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(side_effect=[
        WooCommerceRateLimitError("rate limited", retry_after=0.0),
        {"ok": True},
    ])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Connector context manager
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_connector_context_manager() -> None:
    async with WooCommerceConnector(
        store_url=STORE_URL,
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
    ) as c:
        assert c._store_url == STORE_URL


# ═══════════════════════════════════════════════════════════════════════════════
# Credentials validation
# ═══════════════════════════════════════════════════════════════════════════════


def test_has_credentials_true(connector: WooCommerceConnector) -> None:
    assert connector._has_credentials() is True


def test_has_credentials_false_no_store() -> None:
    c = WooCommerceConnector(consumer_key="ck_a", consumer_secret="cs_b")
    assert c._has_credentials() is False


def test_has_credentials_false_no_key() -> None:
    c = WooCommerceConnector(store_url=STORE_URL, consumer_secret="cs_b")
    assert c._has_credentials() is False


def test_has_credentials_false_no_secret() -> None:
    c = WooCommerceConnector(store_url=STORE_URL, consumer_key="ck_a")
    assert c._has_credentials() is False


def test_connector_type() -> None:
    assert WooCommerceConnector.CONNECTOR_TYPE == "woocommerce"


def test_auth_type() -> None:
    assert WooCommerceConnector.AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# Config dict initialization
# ═══════════════════════════════════════════════════════════════════════════════


def test_init_from_config_dict() -> None:
    c = WooCommerceConnector(
        config={
            "store_url": STORE_URL,
            "consumer_key": CONSUMER_KEY,
            "consumer_secret": CONSUMER_SECRET,
        }
    )
    assert c._store_url == STORE_URL
    assert c._consumer_key == CONSUMER_KEY
    assert c._consumer_secret == CONSUMER_SECRET


def test_init_kwargs_override_config() -> None:
    """Direct kwargs take precedence when config is empty."""
    c = WooCommerceConnector(
        store_url=STORE_URL,
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
    )
    assert c._store_url == STORE_URL


# ═══════════════════════════════════════════════════════════════════════════════
# _stable_id helper
# ═══════════════════════════════════════════════════════════════════════════════


def test_stable_id_length() -> None:
    from helpers.utils import _stable_id
    assert len(_stable_id("woocommerce_product", 12345)) == 16


def test_stable_id_deterministic() -> None:
    from helpers.utils import _stable_id
    assert _stable_id("woocommerce_product", 42) == _stable_id("woocommerce_product", 42)


def test_stable_id_different_inputs() -> None:
    from helpers.utils import _stable_id
    assert _stable_id("woocommerce_product", 1) != _stable_id("woocommerce_product", 2)


def test_stable_id_different_prefixes() -> None:
    from helpers.utils import _stable_id
    # Same numeric ID but different prefix → different stable ID (type safety)
    assert _stable_id("woocommerce_product", 55) != _stable_id("woocommerce_order", 55)


def test_stable_id_matches_spec_format() -> None:
    """Verify the SHA-256 prefix format matches the spec exactly."""
    from helpers.utils import _stable_id
    expected = hashlib.sha256(b"woocommerce_product:55").hexdigest()[:16]
    assert _stable_id("woocommerce_product", 55) == expected


# ═══════════════════════════════════════════════════════════════════════════════
# list_categories
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLE_CATEGORY: dict = {
    "id": 9,
    "name": "Widgets",
    "slug": "widgets",
    "parent": 0,
    "count": 12,
}


@pytest.mark.asyncio
async def test_list_categories(connector: WooCommerceConnector) -> None:
    connector._http.list_categories = AsyncMock(
        return_value=([SAMPLE_CATEGORY], {"X-WP-TotalPages": "1"})
    )
    result = await connector.list_categories()
    assert len(result) == 1
    assert result[0]["name"] == "Widgets"


@pytest.mark.asyncio
async def test_list_categories_page_and_per_page(connector: WooCommerceConnector) -> None:
    connector._http.list_categories = AsyncMock(return_value=([], {}))
    await connector.list_categories(page=2, per_page=50)
    call_kwargs = connector._http.list_categories.call_args
    assert call_kwargs.kwargs["page"] == 2
    assert call_kwargs.kwargs["per_page"] == 50


@pytest.mark.asyncio
async def test_list_categories_auth_error(connector: WooCommerceConnector) -> None:
    connector._http.list_categories = AsyncMock(
        side_effect=WooCommerceAuthError("forbidden", 403)
    )
    with pytest.raises(WooCommerceAuthError):
        await connector.list_categories()


# ═══════════════════════════════════════════════════════════════════════════════
# Site URL normalization (strips https://, trailing slash)
# ═══════════════════════════════════════════════════════════════════════════════


def test_site_url_normalization_strips_https() -> None:
    """connector accepts 'https://mystore.com' and stores it correctly."""
    from client.http_client import _normalize_site_url
    assert _normalize_site_url("https://mystore.com") == "https://mystore.com"


def test_site_url_normalization_strips_http() -> None:
    from client.http_client import _normalize_site_url
    assert _normalize_site_url("http://mystore.com") == "https://mystore.com"


def test_site_url_normalization_strips_trailing_slash() -> None:
    from client.http_client import _normalize_site_url
    assert _normalize_site_url("https://mystore.com/") == "https://mystore.com"


def test_site_url_normalization_bare_domain() -> None:
    """Bare domain (no protocol) gets https:// prepended."""
    from client.http_client import _normalize_site_url
    assert _normalize_site_url("mystore.com") == "https://mystore.com"


def test_site_url_normalization_trailing_slash_only() -> None:
    from client.http_client import _normalize_site_url
    assert _normalize_site_url("mystore.com/") == "https://mystore.com"


def test_api_base_construction() -> None:
    from client.http_client import _api_base
    base = _api_base("mystore.com")
    assert base == "https://mystore.com/wp-json/wc/v3"


def test_api_base_strips_protocol() -> None:
    from client.http_client import _api_base
    base = _api_base("https://mystore.com/")
    assert base == "https://mystore.com/wp-json/wc/v3"


# ═══════════════════════════════════════════════════════════════════════════════
# site_url config field
# ═══════════════════════════════════════════════════════════════════════════════


def test_init_from_config_with_site_url() -> None:
    c = WooCommerceConnector(
        config={
            "site_url": STORE_URL,
            "consumer_key": CONSUMER_KEY,
            "consumer_secret": CONSUMER_SECRET,
        }
    )
    assert c._site_url == STORE_URL


def test_init_site_url_kwarg() -> None:
    c = WooCommerceConnector(site_url=STORE_URL, consumer_key=CONSUMER_KEY, consumer_secret=CONSUMER_SECRET)
    assert c._site_url == STORE_URL


def test_install_missing_message_mentions_site_url() -> None:
    """install() missing message explicitly lists 'site_url'."""
    import asyncio
    c = WooCommerceConnector(consumer_key="ck_a", consumer_secret="cs_b")
    result = asyncio.get_event_loop().run_until_complete(c.install())
    assert "site_url" in result.message
