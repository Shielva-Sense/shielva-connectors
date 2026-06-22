"""Unit tests for LightspeedConnector — respx-mocked, zero real I/O."""
from datetime import datetime

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import LightspeedConnector
from exceptions import (
    LightspeedAuthError,
    LightspeedBadRequestError,
    LightspeedConflictError,
    LightspeedError,
    LightspeedNotFound,
    LightspeedRateLimitError,
)

from tests.conftest import (
    ACCOUNT_ID,
    BASE_URL,
    CONNECTOR_ID,
    SAMPLE_CUSTOMER,
    SAMPLE_ITEM,
    SAMPLE_SALE,
    TENANT_ID,
    TEST_CONFIG,
    TOKEN_URL,
)


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attribute():
    assert LightspeedConnector.CONNECTOR_TYPE == "lightspeed"


def test_auth_type_class_attribute():
    assert LightspeedConnector.AUTH_TYPE == "oauth2_code"


def test_required_config_keys_public_and_minimal():
    # Public attribute, minimal required set per task spec.
    assert LightspeedConnector.REQUIRED_CONFIG_KEYS == ["client_id", "client_secret"]


def test_status_map_string_tuples():
    sm = LightspeedConnector._STATUS_MAP
    assert sm[401] == ("DEGRADED", "TOKEN_EXPIRED")
    assert sm[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert sm[429] == ("DEGRADED", "CONNECTED")


def test_base_url_composed_from_account_id(connector):
    assert connector.http_client._base_url == BASE_URL


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_client_id(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE
    assert "client_id" in (result.message or "")


@pytest.mark.asyncio
async def test_install_missing_client_secret(connector):
    connector.config.pop("client_secret", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_empty_config_returns_offline_missing_credentials():
    blank = LightspeedConnector(
        tenant_id="t", connector_id="c", config={}
    )
    result = await blank.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_authorize_success(authed):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 1800,
                "token_type": "Bearer",
                "scope": "employee:all employee:register",
            },
        )
    )
    result = await authed.authorize("auth-code-123")
    assert result.access_token == "new-access-token"
    assert result.refresh_token == "new-refresh-token"
    assert isinstance(result.expires_at, datetime)
    assert "employee:all" in result.scopes


@pytest.mark.asyncio
@respx.mock
async def test_authorize_no_scope_in_response_falls_back_to_required(authed):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "acc",
                "refresh_token": "ref",
                "expires_in": 1800,
                "token_type": "Bearer",
            },
        )
    )
    result = await authed.authorize("code")
    assert isinstance(result.scopes, list)
    assert "employee:all" in result.scopes


@pytest.mark.asyncio
@respx.mock
async def test_authorize_error_raises_auth_error(authed):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    with pytest.raises(LightspeedAuthError):
        await authed.authorize("bad-code")


# ═══════════════════════════════════════════════════════════════════════════
# health_check() — probes /Account
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(authed):
    respx.get(f"{BASE_URL}/Account.json").mock(
        return_value=httpx.Response(200, json={"Account": {"accountID": ACCOUNT_ID, "name": "Test Shop"}})
    )
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error_returns_degraded_token_expired(authed):
    # /Account.json → 401, refresh endpoint also returns 401 (no recovery).
    respx.get(f"{BASE_URL}/Account.json").mock(
        return_value=httpx.Response(401, json={"message": "expired"})
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_grant"})
    )
    result = await authed.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# get_account()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_account_success(authed):
    respx.get(f"{BASE_URL}/Account.json").mock(
        return_value=httpx.Response(200, json={"Account": {"accountID": ACCOUNT_ID}})
    )
    result = await authed.get_account()
    assert "Account" in result
    assert result["Account"]["accountID"] == ACCOUNT_ID


# ═══════════════════════════════════════════════════════════════════════════
# list_items() / get_item() / create_item()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_items_success(authed):
    respx.get(f"{BASE_URL}/Item.json").mock(
        return_value=httpx.Response(
            200,
            json={"Item": [SAMPLE_ITEM], "@attributes": {"count": "1", "limit": "100"}},
        )
    )
    result = await authed.list_items(limit=100, offset=0)
    assert "Item" in result
    assert result["Item"][0]["itemID"] == "1234"


@pytest.mark.asyncio
@respx.mock
async def test_list_items_with_search_passes_description_filter(authed):
    route = respx.get(f"{BASE_URL}/Item.json").mock(
        return_value=httpx.Response(200, json={"Item": [SAMPLE_ITEM]})
    )
    await authed.list_items(search="widget")
    sent_params = dict(route.calls.last.request.url.params)
    assert "description" in sent_params
    assert "widget" in sent_params["description"]


@pytest.mark.asyncio
@respx.mock
async def test_list_items_with_category_filter(authed):
    route = respx.get(f"{BASE_URL}/Item.json").mock(
        return_value=httpx.Response(200, json={"Item": []})
    )
    await authed.list_items(category_id=10)
    sent_params = dict(route.calls.last.request.url.params)
    assert sent_params.get("categoryID") == "10"


@pytest.mark.asyncio
@respx.mock
async def test_get_item_success(authed):
    respx.get(f"{BASE_URL}/Item/1234.json").mock(
        return_value=httpx.Response(200, json={"Item": SAMPLE_ITEM})
    )
    result = await authed.get_item(1234)
    assert result["Item"]["itemID"] == "1234"


@pytest.mark.asyncio
@respx.mock
async def test_get_item_not_found(authed):
    respx.get(f"{BASE_URL}/Item/99999.json").mock(
        return_value=httpx.Response(404, json={"message": "Item not found"})
    )
    with pytest.raises(LightspeedNotFound):
        await authed.get_item(99999)


@pytest.mark.asyncio
@respx.mock
async def test_create_item_success_builds_prices_envelope(authed):
    route = respx.post(f"{BASE_URL}/Item.json").mock(
        return_value=httpx.Response(201, json={"Item": SAMPLE_ITEM})
    )
    result = await authed.create_item(
        description="Test Widget",
        default_cost=5.00,
        default_price=9.99,
        category_id=10,
    )
    assert result["Item"]["description"] == "Test Widget"
    import json as _json
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["description"] == "Test Widget"
    assert body["defaultCost"] == "5.0"
    assert body["categoryID"] == "10"
    assert body["Prices"]["ItemPrice"][0]["amount"] == "9.99"


# ═══════════════════════════════════════════════════════════════════════════
# list_categories()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_categories_success(authed):
    respx.get(f"{BASE_URL}/Category.json").mock(
        return_value=httpx.Response(
            200,
            json={"Category": [{"categoryID": "10", "name": "Widgets"}]},
        )
    )
    result = await authed.list_categories()
    assert result["Category"][0]["categoryID"] == "10"


# ═══════════════════════════════════════════════════════════════════════════
# list_customers() + get_customer() + create_customer()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_customers_success(authed):
    respx.get(f"{BASE_URL}/Customer.json").mock(
        return_value=httpx.Response(200, json={"Customer": [SAMPLE_CUSTOMER]})
    )
    result = await authed.list_customers()
    assert result["Customer"][0]["customerID"] == "555"


@pytest.mark.asyncio
@respx.mock
async def test_list_customers_search_uses_last_name_wildcard(authed):
    route = respx.get(f"{BASE_URL}/Customer.json").mock(
        return_value=httpx.Response(200, json={"Customer": []})
    )
    await authed.list_customers(search="Lovelace")
    params = dict(route.calls.last.request.url.params)
    assert "lastName" in params
    assert "Lovelace" in params["lastName"]


@pytest.mark.asyncio
@respx.mock
async def test_get_customer_success(authed):
    respx.get(f"{BASE_URL}/Customer/555.json").mock(
        return_value=httpx.Response(200, json={"Customer": SAMPLE_CUSTOMER})
    )
    result = await authed.get_customer(555)
    assert result["Customer"]["customerID"] == "555"


@pytest.mark.asyncio
@respx.mock
async def test_create_customer_with_email_phone_builds_contact_envelope(authed):
    route = respx.post(f"{BASE_URL}/Customer.json").mock(
        return_value=httpx.Response(201, json={"Customer": SAMPLE_CUSTOMER})
    )
    await authed.create_customer(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        phone="555-1234",
    )
    import json as _json
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["firstName"] == "Ada"
    assert body["lastName"] == "Lovelace"
    assert body["Contact"]["Emails"]["ContactEmail"][0]["address"] == "ada@example.com"
    assert body["Contact"]["Phones"]["ContactPhone"][0]["number"] == "555-1234"


# ═══════════════════════════════════════════════════════════════════════════
# list_sales() + filters + get_sale() + create_sale()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_sales_with_completed_and_date_range(authed):
    route = respx.get(f"{BASE_URL}/Sale.json").mock(
        return_value=httpx.Response(200, json={"Sale": [SAMPLE_SALE]})
    )
    await authed.list_sales(
        completed=True,
        start_date="2024-01-01T00:00:00+00:00",
        end_date="2024-12-31T23:59:59+00:00",
    )
    params = dict(route.calls.last.request.url.params)
    assert params["completed"] == "true"
    assert "createTime" in params
    assert "><" in params["createTime"]


@pytest.mark.asyncio
@respx.mock
async def test_list_sales_customer_filter(authed):
    route = respx.get(f"{BASE_URL}/Sale.json").mock(
        return_value=httpx.Response(200, json={"Sale": []})
    )
    await authed.list_sales(customer_id=555)
    params = dict(route.calls.last.request.url.params)
    assert params["customerID"] == "555"


@pytest.mark.asyncio
@respx.mock
async def test_get_sale_success(authed):
    respx.get(f"{BASE_URL}/Sale/42.json").mock(
        return_value=httpx.Response(200, json={"Sale": SAMPLE_SALE})
    )
    result = await authed.get_sale(42)
    assert result["Sale"]["saleID"] == "42"


@pytest.mark.asyncio
@respx.mock
async def test_create_sale_success(authed):
    route = respx.post(f"{BASE_URL}/Sale.json").mock(
        return_value=httpx.Response(201, json={"Sale": SAMPLE_SALE})
    )
    result = await authed.create_sale({"completed": "true", "shopID": "1"})
    assert result["Sale"]["saleID"] == "42"
    assert route.called


@pytest.mark.asyncio
async def test_create_sale_rejects_empty_body(authed):
    with pytest.raises(ValueError):
        await authed.create_sale({})


# ═══════════════════════════════════════════════════════════════════════════
# list_shops / list_inventory / list_employees / list_vendors
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_shops_success(authed):
    respx.get(f"{BASE_URL}/Shop.json").mock(
        return_value=httpx.Response(200, json={"Shop": [{"shopID": "1", "name": "Main"}]})
    )
    result = await authed.list_shops()
    assert result["Shop"][0]["shopID"] == "1"


@pytest.mark.asyncio
@respx.mock
async def test_list_inventory_with_filters(authed):
    route = respx.get(f"{BASE_URL}/ItemShop.json").mock(
        return_value=httpx.Response(200, json={"ItemShop": []})
    )
    await authed.list_inventory(item_id=1234, shop_id=1)
    params = dict(route.calls.last.request.url.params)
    assert params["itemID"] == "1234"
    assert params["shopID"] == "1"


@pytest.mark.asyncio
@respx.mock
async def test_list_employees_success(authed):
    respx.get(f"{BASE_URL}/Employee.json").mock(
        return_value=httpx.Response(
            200, json={"Employee": [{"employeeID": "7", "firstName": "Pat"}]}
        )
    )
    result = await authed.list_employees()
    assert result["Employee"][0]["employeeID"] == "7"


@pytest.mark.asyncio
@respx.mock
async def test_list_vendors_success(authed):
    respx.get(f"{BASE_URL}/Vendor.json").mock(
        return_value=httpx.Response(
            200, json={"Vendor": [{"vendorID": "3", "name": "Acme"}]}
        )
    )
    result = await authed.list_vendors()
    assert result["Vendor"][0]["vendorID"] == "3"


# ═══════════════════════════════════════════════════════════════════════════
# Refresh-on-401 (single replay)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_refresh_on_401_then_retry_succeeds(authed):
    """First /Account.json returns 401 → refresh succeeds → retry returns 200."""
    call_count = {"n": 0}

    def account_handler(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(401, json={"message": "token expired"})
        return httpx.Response(200, json={"Account": {"accountID": ACCOUNT_ID}})

    respx.get(f"{BASE_URL}/Account.json").mock(side_effect=account_handler)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed-access-token",
                "expires_in": 1800,
                "token_type": "Bearer",
            },
        )
    )

    result = await authed.get_account()
    assert call_count["n"] == 2
    assert result["Account"]["accountID"] == ACCOUNT_ID


# ═══════════════════════════════════════════════════════════════════════════
# 429 retry + bucket header backoff
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_eventually_succeeds(authed, no_retry_sleep):
    """First call returns 429 with Retry-After=0; subsequent succeeds."""
    call_count = {"n": 0}

    def shop_handler(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"})
        return httpx.Response(200, json={"Shop": [{"shopID": "1"}]})

    respx.get(f"{BASE_URL}/Shop.json").mock(side_effect=shop_handler)

    result = await authed.list_shops()
    assert call_count["n"] >= 2
    assert result["Shop"][0]["shopID"] == "1"


@pytest.mark.asyncio
@respx.mock
async def test_bucket_header_does_not_break_success(authed, no_retry_sleep):
    """A near-full bucket header triggers a brief sleep but the call still returns data."""
    respx.get(f"{BASE_URL}/Account.json").mock(
        return_value=httpx.Response(
            200,
            headers={"X-LS-API-Bucket-Level": "59/60"},  # 98% full
            json={"Account": {"accountID": ACCOUNT_ID}},
        )
    )
    result = await authed.get_account()
    assert result["Account"]["accountID"] == ACCOUNT_ID


# ═══════════════════════════════════════════════════════════════════════════
# Mocked-client orchestration (mock_LightspeedHTTPClient fixture)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_orchestration_list_items_calls_client_get(mock_LightspeedHTTPClient):
    """list_items should delegate to http_client.get with the right path + params."""
    mock_LightspeedHTTPClient.http_client.get.return_value = {"Item": []}
    await mock_LightspeedHTTPClient.list_items(limit=10, offset=5)
    args, kwargs = mock_LightspeedHTTPClient.http_client.get.call_args
    # path is positional[0]
    assert args[0] == "Item.json"
    assert kwargs["params"]["limit"] == 10
    assert kwargs["params"]["offset"] == 5


@pytest.mark.asyncio
async def test_orchestration_update_item_calls_client_put(mock_LightspeedHTTPClient):
    """update_item should delegate to http_client.put."""
    mock_LightspeedHTTPClient.http_client.put.return_value = {"Item": {"itemID": "1"}}
    result = await mock_LightspeedHTTPClient.update_item(1, {"description": "renamed"})
    assert result["Item"]["itemID"] == "1"
    args, kwargs = mock_LightspeedHTTPClient.http_client.put.call_args
    assert args[0] == "Item/1.json"
    assert kwargs["json_body"] == {"description": "renamed"}


@pytest.mark.asyncio
async def test_orchestration_update_item_rejects_empty_dict(mock_LightspeedHTTPClient):
    with pytest.raises(ValueError):
        await mock_LightspeedHTTPClient.update_item(1, {})


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_different_tenants_different_connector_instances():
    c1 = LightspeedConnector(tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = LightspeedConnector(tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    # Same account → same base URL.
    assert c1.http_client._base_url == c2.http_client._base_url
