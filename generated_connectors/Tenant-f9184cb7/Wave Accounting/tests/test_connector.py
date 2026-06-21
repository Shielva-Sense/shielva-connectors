"""Unit tests for WaveConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import WaveConnector
from exceptions import (
    WaveAuthError,
    WaveError,
    WaveNotFoundError,
    WaveValidationError,
)

from tests.conftest import (
    CONNECTOR_ID,
    TEST_ACCESS_TOKEN,
    TEST_BUSINESS_ID,
    TEST_CONFIG,
    WAVE_BASE,
)


def _ok(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


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
async def test_install_missing_access_token(connector):
    connector.config.pop("access_token", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape + auth-error path
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer_access_token(connector):
    """Connector must send the access_token as a Bearer header."""
    route = respx.post(WAVE_BASE).mock(
        return_value=_ok({"user": {"id": "u_1"}}),
    )
    await connector.get_user()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_ACCESS_TOKEN}"
    # Content-Type should be JSON for GraphQL.
    assert route.calls[0].request.headers.get("content-type") == "application/json"


@respx.mock
@pytest.mark.asyncio
async def test_post_body_is_graphql_envelope(connector):
    """Wave is GraphQL — every request must POST {query, variables}."""
    route = respx.post(WAVE_BASE).mock(return_value=_ok({"user": {"id": "u_1"}}))
    await connector.get_user()
    body = json.loads(route.calls[0].request.content.decode())
    assert "query" in body
    assert "variables" in body
    assert isinstance(body["query"], str)
    assert body["query"].lstrip().startswith("query")


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_wave_auth_error(connector):
    respx.post(WAVE_BASE).mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    with pytest.raises(WaveAuthError):
        await connector.get_user()


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_403_raises_wave_auth_error(connector):
    respx.post(WAVE_BASE).mock(
        return_value=httpx.Response(403, json={"message": "Forbidden — missing scope"})
    )
    with pytest.raises(WaveAuthError):
        await connector.get_user()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.post(WAVE_BASE).mock(return_value=_ok({"user": {"id": "u_1"}}))
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error_401(connector):
    respx.post(WAVE_BASE).mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error_403(connector):
    respx.post(WAVE_BASE).mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.FAILED
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# get_user / list_users
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_get_user_returns_node(connector):
    respx.post(WAVE_BASE).mock(
        return_value=_ok(
            {
                "user": {
                    "id": "u_1",
                    "defaultEmail": "owner@example.com",
                    "firstName": "Wave",
                    "lastName": "Owner",
                }
            }
        )
    )
    user = await connector.get_user()
    assert user["id"] == "u_1"
    assert user["defaultEmail"] == "owner@example.com"


@respx.mock
@pytest.mark.asyncio
async def test_list_users_wraps_user_in_connection(connector):
    respx.post(WAVE_BASE).mock(return_value=_ok({"user": {"id": "u_1"}}))
    result = await connector.list_users()
    assert result["users"]["edges"][0]["node"]["id"] == "u_1"
    assert result["users"]["pageInfo"]["totalCount"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Businesses
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_businesses_passes_pagination(connector):
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok(
            {
                "businesses": {
                    "pageInfo": {"currentPage": 2, "totalPages": 5, "totalCount": 100},
                    "edges": [{"node": {"id": "b_1", "name": "Acme"}}],
                }
            }
        )

    respx.post(WAVE_BASE).mock(side_effect=_handler)
    result = await connector.list_businesses(page=2, page_size=25)
    assert captured["variables"] == {"page": 2, "pageSize": 25}
    assert result["businesses"]["edges"][0]["node"]["id"] == "b_1"


@respx.mock
@pytest.mark.asyncio
async def test_get_business_uses_default_business_id(connector):
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok({"business": {"id": TEST_BUSINESS_ID, "name": "Default Co"}})

    respx.post(WAVE_BASE).mock(side_effect=_handler)
    result = await connector.get_business()  # no business_id arg
    assert captured["variables"] == {"id": TEST_BUSINESS_ID}
    assert result["business"]["id"] == TEST_BUSINESS_ID


@pytest.mark.asyncio
async def test_get_business_requires_id_when_no_default(connector):
    connector.default_business_id = ""
    with pytest.raises(WaveValidationError):
        await connector.get_business()


# ═══════════════════════════════════════════════════════════════════════════
# Customers
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_customers_returns_edges(connector):
    respx.post(WAVE_BASE).mock(
        return_value=_ok(
            {
                "business": {
                    "id": TEST_BUSINESS_ID,
                    "customers": {
                        "pageInfo": {"currentPage": 1, "totalPages": 1, "totalCount": 1},
                        "edges": [
                            {
                                "node": {
                                    "id": "c_1",
                                    "name": "Jane",
                                    "email": "j@example.com",
                                }
                            }
                        ],
                    },
                }
            }
        )
    )
    result = await connector.list_customers()
    assert result["business"]["customers"]["edges"][0]["node"]["id"] == "c_1"


@respx.mock
@pytest.mark.asyncio
async def test_get_customer_success(connector):
    respx.post(WAVE_BASE).mock(
        return_value=_ok(
            {
                "business": {
                    "id": TEST_BUSINESS_ID,
                    "customer": {"id": "c_42", "name": "Jane"},
                }
            }
        )
    )
    result = await connector.get_customer("c_42")
    assert result["business"]["customer"]["id"] == "c_42"


@pytest.mark.asyncio
async def test_get_customer_requires_id(connector):
    with pytest.raises(WaveValidationError):
        await connector.get_customer("")


@respx.mock
@pytest.mark.asyncio
async def test_create_customer_mutation_envelope(connector):
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok(
            {
                "customerCreate": {
                    "didSucceed": True,
                    "inputErrors": [],
                    "customer": {
                        "id": "c_99",
                        "name": "Jane",
                        "email": "jane@example.com",
                    },
                }
            }
        )

    respx.post(WAVE_BASE).mock(side_effect=_handler)
    result = await connector.create_customer(name="Jane", email="jane@example.com")
    assert captured["query"].lstrip().startswith("mutation")
    assert captured["variables"]["input"] == {
        "businessId": TEST_BUSINESS_ID,
        "name": "Jane",
        "email": "jane@example.com",
    }
    assert result["customerCreate"]["customer"]["id"] == "c_99"


@pytest.mark.asyncio
async def test_create_customer_requires_name(connector):
    with pytest.raises(WaveValidationError):
        await connector.create_customer(name="")


# ═══════════════════════════════════════════════════════════════════════════
# Invoices
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_invoices_uppercases_status(connector):
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok(
            {
                "business": {
                    "id": TEST_BUSINESS_ID,
                    "invoices": {
                        "pageInfo": {"currentPage": 1, "totalPages": 1, "totalCount": 0},
                        "edges": [],
                    },
                }
            }
        )

    respx.post(WAVE_BASE).mock(side_effect=_handler)
    await connector.list_invoices(status="paid")
    assert captured["variables"]["status"] == "PAID"


@respx.mock
@pytest.mark.asyncio
async def test_get_invoice_success(connector):
    respx.post(WAVE_BASE).mock(
        return_value=_ok(
            {
                "business": {
                    "id": TEST_BUSINESS_ID,
                    "invoice": {"id": "inv_1", "invoiceNumber": "INV-0001"},
                }
            }
        )
    )
    result = await connector.get_invoice("inv_1")
    assert result["business"]["invoice"]["id"] == "inv_1"


@respx.mock
@pytest.mark.asyncio
async def test_create_invoice_translates_items_camel_case(connector):
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok(
            {
                "invoiceCreate": {
                    "didSucceed": True,
                    "inputErrors": [],
                    "invoice": {
                        "id": "inv_1",
                        "invoiceNumber": "INV-0001",
                        "status": "DRAFT",
                        "total": {"value": "10.00", "currency": {"code": "USD"}},
                    },
                }
            }
        )

    respx.post(WAVE_BASE).mock(side_effect=_handler)
    result = await connector.create_invoice(
        customer_id="c_1",
        items=[
            {
                "product_id": "p_1",
                "quantity": 2,
                "unit_price": 5.0,
                "description": "ea",
            }
        ],
        due_date="2026-07-01",
    )
    item = captured["variables"]["input"]["items"][0]
    assert item == {
        "productId": "p_1",
        "quantity": 2,
        "unitPrice": 5.0,
        "description": "ea",
    }
    assert captured["variables"]["input"]["dueDate"] == "2026-07-01"
    assert result["invoiceCreate"]["invoice"]["id"] == "inv_1"


@pytest.mark.asyncio
async def test_create_invoice_requires_items(connector):
    with pytest.raises(WaveValidationError):
        await connector.create_invoice(customer_id="c_1", items=[])


@pytest.mark.asyncio
async def test_create_invoice_requires_customer_id(connector):
    with pytest.raises(WaveValidationError):
        await connector.create_invoice(customer_id="", items=[{"product_id": "p"}])


@pytest.mark.asyncio
async def test_create_invoice_item_requires_product_id(connector):
    with pytest.raises(WaveValidationError):
        await connector.create_invoice(customer_id="c_1", items=[{"quantity": 1}])


# ═══════════════════════════════════════════════════════════════════════════
# Products
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_products_success(connector):
    respx.post(WAVE_BASE).mock(
        return_value=_ok(
            {
                "business": {
                    "id": TEST_BUSINESS_ID,
                    "products": {
                        "pageInfo": {"currentPage": 1, "totalPages": 1, "totalCount": 1},
                        "edges": [{"node": {"id": "p_1", "name": "Hosting"}}],
                    },
                }
            }
        )
    )
    result = await connector.list_products()
    assert result["business"]["products"]["edges"][0]["node"]["id"] == "p_1"


@respx.mock
@pytest.mark.asyncio
async def test_create_product_mutation_envelope(connector):
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok(
            {
                "productCreate": {
                    "didSucceed": True,
                    "inputErrors": [],
                    "product": {
                        "id": "p_99",
                        "name": "Consulting",
                        "unitPrice": "150",
                        "description": None,
                    },
                }
            }
        )

    respx.post(WAVE_BASE).mock(side_effect=_handler)
    result = await connector.create_product(name="Consulting", unit_price=150)
    assert captured["variables"]["input"] == {
        "businessId": TEST_BUSINESS_ID,
        "name": "Consulting",
        "unitPrice": 150,
    }
    assert result["productCreate"]["product"]["id"] == "p_99"


# ═══════════════════════════════════════════════════════════════════════════
# Accounts / Transactions / Sales taxes
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_accounts_success(connector):
    respx.post(WAVE_BASE).mock(
        return_value=_ok(
            {
                "business": {
                    "id": TEST_BUSINESS_ID,
                    "accounts": {
                        "edges": [{"node": {"id": "a_1", "name": "Cash"}}],
                    },
                }
            }
        )
    )
    result = await connector.list_accounts()
    assert result["business"]["accounts"]["edges"][0]["node"]["id"] == "a_1"


@respx.mock
@pytest.mark.asyncio
async def test_list_transactions_passes_date_range(connector):
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok(
            {
                "business": {
                    "id": TEST_BUSINESS_ID,
                    "transactions": {
                        "pageInfo": {"currentPage": 1, "totalPages": 1, "totalCount": 0},
                        "edges": [],
                    },
                }
            }
        )

    respx.post(WAVE_BASE).mock(side_effect=_handler)
    await connector.list_transactions(from_date="2026-01-01", to_date="2026-06-30")
    assert captured["variables"]["from"] == "2026-01-01"
    assert captured["variables"]["to"] == "2026-06-30"


@respx.mock
@pytest.mark.asyncio
async def test_list_sales_taxes_success(connector):
    respx.post(WAVE_BASE).mock(
        return_value=_ok(
            {
                "business": {
                    "id": TEST_BUSINESS_ID,
                    "salesTaxes": {
                        "edges": [
                            {
                                "node": {
                                    "id": "st_1",
                                    "name": "GST",
                                    "abbreviation": "GST",
                                    "rate": "5",
                                    "taxNumber": None,
                                }
                            }
                        ]
                    },
                }
            }
        )
    )
    result = await connector.list_sales_taxes()
    assert result["business"]["salesTaxes"]["edges"][0]["node"]["id"] == "st_1"


# ═══════════════════════════════════════════════════════════════════════════
# GraphQL errors on HTTP 200 still raise
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_graphql_errors_array_raises_wave_error(connector):
    respx.post(WAVE_BASE).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": None,
                "errors": [
                    {"message": "Field 'foo' is not defined"},
                    {"message": "Unauthorized for business b_1"},
                ],
            },
        )
    )
    with pytest.raises(WaveError) as exc_info:
        await connector.get_user()
    msg = str(exc_info.value)
    assert "Field 'foo'" in msg
    assert "Unauthorized for business" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.post(WAVE_BASE).mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            _ok({"user": {"id": "after-retry"}}),
        ]
    )
    result = await connector.get_user()
    assert route.call_count == 2
    assert result["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.post(WAVE_BASE).mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            _ok({"user": {"id": "after-retry"}}),
        ]
    )
    result = await connector.get_user()
    assert route.call_count == 2
    assert result["id"] == "after-retry"


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert WaveConnector.CONNECTOR_TYPE == "wave"


def test_auth_type_class_attr():
    assert WaveConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(WaveConnector, "REQUIRED_CONFIG_KEYS")
    assert "access_token" in WaveConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = WaveConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = WaveConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# NormalizedDocument id is tenant-scoped (SOC constraint)
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_customer_id_is_tenant_scoped():
    from helpers.normalizer import normalize_customer

    doc = normalize_customer(
        {"node": {"id": "src-123", "name": "Jane", "email": "j@example.com"}},
        connector_id="conn-1",
        tenant_id="tenant-A",
    )
    assert doc.id == "tenant-A_src-123"
    assert doc.source_id == "src-123"
    assert doc.metadata["kind"] == "wave.customer"


def test_normalize_invoice_id_is_tenant_scoped():
    from helpers.normalizer import normalize_invoice

    doc = normalize_invoice(
        {
            "node": {
                "id": "inv-1",
                "invoiceNumber": "INV-001",
                "status": "PAID",
                "total": {"value": "100", "currency": {"code": "USD"}},
                "customer": {"id": "c-1", "name": "Jane"},
            }
        },
        connector_id="conn-1",
        tenant_id="tenant-B",
    )
    assert doc.id == "tenant-B_inv-1"
    assert doc.metadata["kind"] == "wave.invoice"
    assert doc.metadata["currency"] == "USD"
