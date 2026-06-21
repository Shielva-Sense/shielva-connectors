"""Unit tests for WixConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import WixConnector
from exceptions import WixAuthError, WixError, WixNotFound

from tests.conftest import (
    CONNECTOR_ID,
    TENANT_ID,
    TEST_ACCOUNT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
    TEST_SITE_ID,
    WIX_BASE,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_account_id(connector):
    connector.config.pop("account_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (raw key, no Bearer) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_raw_key_no_bearer(connector):
    """Connector must send the api_key RAW in Authorization (no 'Bearer ' prefix)."""
    route = respx.get(f"{WIX_BASE}/site-list/v2/sites").mock(
        return_value=httpx.Response(200, json={"sites": []})
    )
    await connector.list_sites(paging_limit=1)
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == TEST_API_KEY
    assert not sent_auth.lower().startswith("bearer ")
    # Required Wix headers
    assert route.calls[0].request.headers.get("wix-account-id") == TEST_ACCOUNT_ID


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_wix_auth_error(connector):
    respx.get(f"{WIX_BASE}/site-list/v2/sites").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    with pytest.raises(WixAuthError):
        await connector.list_sites()


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{WIX_BASE}/site-list/v2/sites").mock(
        return_value=httpx.Response(200, json={"sites": [{"id": "s1"}]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{WIX_BASE}/site-list/v2/sites").mock(
        return_value=httpx.Response(401, json={"message": "Invalid key"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Sites
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_sites_success(connector):
    sites_resp = {"sites": [{"id": "s1", "displayName": "Site One"}], "metadata": {"count": 1}}
    route = respx.get(f"{WIX_BASE}/site-list/v2/sites").mock(
        return_value=httpx.Response(200, json=sites_resp)
    )
    result = await connector.list_sites(paging_limit=5, paging_offset=0)
    assert route.called
    assert result["sites"][0]["id"] == "s1"
    # Verify pagination params landed on the URL
    qs = route.calls[0].request.url.params
    assert qs.get("paging.limit") == "5"
    assert qs.get("paging.offset") == "0"


@respx.mock
@pytest.mark.asyncio
async def test_get_site_success(connector):
    site_id = "s99"
    respx.get(f"{WIX_BASE}/site-list/v2/sites/{site_id}").mock(
        return_value=httpx.Response(200, json={"site": {"id": site_id}})
    )
    result = await connector.get_site(site_id)
    assert result["site"]["id"] == site_id


@respx.mock
@pytest.mark.asyncio
async def test_get_site_not_found(connector):
    site_id = "missing"
    respx.get(f"{WIX_BASE}/site-list/v2/sites/{site_id}").mock(
        return_value=httpx.Response(404, json={"message": "site not found"})
    )
    with pytest.raises(WixNotFound):
        await connector.get_site(site_id)


# ═══════════════════════════════════════════════════════════════════════════
# Products
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_products_posts_query_body(connector):
    route = respx.post(f"{WIX_BASE}/stores-reader/v1/products/query").mock(
        return_value=httpx.Response(200, json={"products": [{"id": "p1", "name": "T-shirt"}]})
    )
    paging = {"limit": 20, "offset": 0}
    query = {"filter": {"name": "shirt"}}
    result = await connector.list_products(TEST_SITE_ID, paging=paging, query=query)
    assert route.called
    sent = route.calls[0].request
    # site_id header propagated
    assert sent.headers.get("wix-site-id") == TEST_SITE_ID
    # Body is JSON wrapping the query
    import json as _json
    body = _json.loads(sent.content.decode())
    assert "query" in body
    assert body["query"]["filter"] == {"name": "shirt"}
    assert body["query"]["paging"] == paging
    assert result["products"][0]["id"] == "p1"


@respx.mock
@pytest.mark.asyncio
async def test_get_product_success(connector):
    pid = "p42"
    respx.get(f"{WIX_BASE}/stores-reader/v1/products/{pid}").mock(
        return_value=httpx.Response(200, json={"product": {"id": pid, "name": "Hat"}})
    )
    result = await connector.get_product(TEST_SITE_ID, pid)
    assert result["product"]["id"] == pid


@respx.mock
@pytest.mark.asyncio
async def test_create_product_posts_product_envelope(connector):
    route = respx.post(f"{WIX_BASE}/stores/v1/products").mock(
        return_value=httpx.Response(200, json={"product": {"id": "newp"}})
    )
    product = {"name": "Mug", "priceData": {"price": 12}}
    result = await connector.create_product(TEST_SITE_ID, product)
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == {"product": product}
    assert result["product"]["id"] == "newp"


# ═══════════════════════════════════════════════════════════════════════════
# Orders
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_orders_search_body(connector):
    route = respx.post(f"{WIX_BASE}/ecom/v1/orders/search").mock(
        return_value=httpx.Response(200, json={"orders": [{"id": "o1", "number": "1001"}]})
    )
    result = await connector.list_orders(
        TEST_SITE_ID,
        paging={"limit": 50},
        filter={"status": "APPROVED"},
        sort={"field": "createdDate", "order": "DESC"},
    )
    assert route.called
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert "search" in body
    assert body["search"]["filter"] == {"status": "APPROVED"}
    assert body["search"]["cursorPaging"] == {"limit": 50}
    assert result["orders"][0]["id"] == "o1"


@respx.mock
@pytest.mark.asyncio
async def test_get_order_success(connector):
    oid = "o-555"
    respx.get(f"{WIX_BASE}/ecom/v1/orders/{oid}").mock(
        return_value=httpx.Response(200, json={"order": {"id": oid, "number": "555"}})
    )
    result = await connector.get_order(TEST_SITE_ID, oid)
    assert result["order"]["id"] == oid


# ═══════════════════════════════════════════════════════════════════════════
# Contacts
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_success(connector):
    route = respx.post(f"{WIX_BASE}/contacts/v4/contacts/query").mock(
        return_value=httpx.Response(200, json={"contacts": [{"id": "c1"}]})
    )
    result = await connector.list_contacts(TEST_SITE_ID, paging={"limit": 10})
    assert route.called
    assert result["contacts"][0]["id"] == "c1"


@respx.mock
@pytest.mark.asyncio
async def test_create_contact_posts_info_envelope(connector):
    route = respx.post(f"{WIX_BASE}/contacts/v4/contacts").mock(
        return_value=httpx.Response(200, json={"contact": {"id": "new-c"}})
    )
    info = {"name": {"first": "Ada", "last": "Lovelace"}, "emails": {"items": [{"email": "ada@ex.com"}]}}
    result = await connector.create_contact(TEST_SITE_ID, info)
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == {"info": info}
    assert result["contact"]["id"] == "new-c"


# ═══════════════════════════════════════════════════════════════════════════
# Members
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_members_success(connector):
    respx.post(f"{WIX_BASE}/members/v1/members/query").mock(
        return_value=httpx.Response(200, json={"members": [{"id": "m1"}]})
    )
    result = await connector.list_members(TEST_SITE_ID)
    assert result["members"][0]["id"] == "m1"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{WIX_BASE}/site-list/v2/sites").mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(200, json={"sites": [{"id": "after-retry"}]}),
        ]
    )
    result = await connector.list_sites(paging_limit=1)
    assert route.call_count == 2
    assert result["sites"][0]["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{WIX_BASE}/site-list/v2/sites").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"sites": []}),
        ]
    )
    result = await connector.list_sites()
    assert route.call_count == 2
    assert result == {"sites": []}


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert WixConnector.CONNECTOR_TYPE == "wix"


def test_auth_type_class_attr():
    assert WixConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(WixConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in WixConnector.REQUIRED_CONFIG_KEYS
    assert "account_id" in WixConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = WixConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = WixConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
