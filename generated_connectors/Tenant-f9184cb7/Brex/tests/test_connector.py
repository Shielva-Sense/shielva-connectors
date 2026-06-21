"""Unit tests for BrexConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import BrexConnector
from exceptions import BrexAuthError, BrexError, BrexNotFound

from tests.conftest import (
    BREX_BASE,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_ACCESS_TOKEN,
    TEST_CONFIG,
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
async def test_install_missing_access_token(connector):
    connector.config.pop("access_token", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer prefix) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer(connector):
    """Connector must send the access_token as 'Bearer <token>'."""
    route = respx.get(f"{BREX_BASE}/v2/users/me").mock(
        return_value=httpx.Response(200, json={"id": "u1", "email": "ada@brex.com"})
    )
    await connector.get_current_user()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_ACCESS_TOKEN}"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_brex_auth_error(connector):
    respx.get(f"{BREX_BASE}/v2/users/me").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    with pytest.raises(BrexAuthError):
        await connector.get_current_user()


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{BREX_BASE}/v2/users/me").mock(
        return_value=httpx.Response(200, json={"id": "u1"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{BREX_BASE}/v2/users/me").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_users_success(connector):
    route = respx.get(f"{BREX_BASE}/v2/users").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"id": "u1", "email": "a@b.com"}], "next_cursor": None},
        )
    )
    result = await connector.list_users(limit=10)
    assert route.called
    assert result["items"][0]["id"] == "u1"
    qs = route.calls[0].request.url.params
    assert qs.get("limit") == "10"


@respx.mock
@pytest.mark.asyncio
async def test_get_user_success(connector):
    uid = "u-42"
    respx.get(f"{BREX_BASE}/v2/users/{uid}").mock(
        return_value=httpx.Response(200, json={"id": uid, "email": "x@y.com"})
    )
    result = await connector.get_user(uid)
    assert result["id"] == uid


@respx.mock
@pytest.mark.asyncio
async def test_get_user_not_found(connector):
    uid = "missing"
    respx.get(f"{BREX_BASE}/v2/users/{uid}").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    with pytest.raises(BrexNotFound):
        await connector.get_user(uid)


# ═══════════════════════════════════════════════════════════════════════════
# Cards
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_cards_success(connector):
    route = respx.get(f"{BREX_BASE}/v2/cards").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "c1", "last_four": "4242"}]},
        )
    )
    result = await connector.list_cards(limit=5, user_id="u1")
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("limit") == "5"
    assert qs.get("user_id") == "u1"
    assert result["items"][0]["id"] == "c1"


@respx.mock
@pytest.mark.asyncio
async def test_get_card_success(connector):
    cid = "card-99"
    respx.get(f"{BREX_BASE}/v2/cards/{cid}").mock(
        return_value=httpx.Response(200, json={"id": cid, "status": "ACTIVE"})
    )
    result = await connector.get_card(cid)
    assert result["id"] == cid


# ═══════════════════════════════════════════════════════════════════════════
# Transactions
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_transactions_success(connector):
    route = respx.get(f"{BREX_BASE}/v2/transactions/card/primary").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "tx1", "description": "Coffee"}]},
        )
    )
    result = await connector.list_transactions(
        limit=20, posted_at_start="2025-01-01",
    )
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("limit") == "20"
    assert qs.get("posted_at_start") == "2025-01-01"
    assert result["items"][0]["id"] == "tx1"


@respx.mock
@pytest.mark.asyncio
async def test_get_transaction_success(connector):
    tid = "tx-555"
    respx.get(f"{BREX_BASE}/v2/transactions/card/primary/{tid}").mock(
        return_value=httpx.Response(200, json={"id": tid, "description": "Lunch"})
    )
    result = await connector.get_transaction(tid)
    assert result["id"] == tid


# ═══════════════════════════════════════════════════════════════════════════
# Expenses
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_expenses_success(connector):
    route = respx.get(f"{BREX_BASE}/v1/expenses/card").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "e1", "memo": "Lunch"}]},
        )
    )
    result = await connector.list_expenses(limit=10, status=["APPROVED"])
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("limit") == "10"
    assert "APPROVED" in qs.get_list("status[]")
    assert result["items"][0]["id"] == "e1"


@respx.mock
@pytest.mark.asyncio
async def test_get_expense_success(connector):
    eid = "exp-1"
    respx.get(f"{BREX_BASE}/v1/expenses/card/{eid}").mock(
        return_value=httpx.Response(200, json={"id": eid, "memo": "Taxi"})
    )
    result = await connector.get_expense(eid)
    assert result["id"] == eid


# ═══════════════════════════════════════════════════════════════════════════
# Departments / Locations / Vendors / Receipts / Budgets / Spend Limits
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_departments_success(connector):
    respx.get(f"{BREX_BASE}/v2/departments").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "d1"}]})
    )
    result = await connector.list_departments(limit=10)
    assert result["items"][0]["id"] == "d1"


@respx.mock
@pytest.mark.asyncio
async def test_list_locations_success(connector):
    respx.get(f"{BREX_BASE}/v2/locations").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "l1"}]})
    )
    result = await connector.list_locations()
    assert result["items"][0]["id"] == "l1"


@respx.mock
@pytest.mark.asyncio
async def test_list_vendors_success(connector):
    respx.get(f"{BREX_BASE}/v1/vendors").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "v1", "company_name": "ACME"}]},
        )
    )
    result = await connector.list_vendors()
    assert result["items"][0]["id"] == "v1"


@respx.mock
@pytest.mark.asyncio
async def test_list_receipts_success(connector):
    respx.get(f"{BREX_BASE}/v1/expenses/card/receipt_match").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "r1"}]})
    )
    result = await connector.list_receipts()
    assert result["items"][0]["id"] == "r1"


@respx.mock
@pytest.mark.asyncio
async def test_list_budgets_success(connector):
    respx.get(f"{BREX_BASE}/v2/budgets").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "b1"}]})
    )
    result = await connector.list_budgets()
    assert result["items"][0]["id"] == "b1"


@respx.mock
@pytest.mark.asyncio
async def test_list_spend_limits_success(connector):
    respx.get(f"{BREX_BASE}/v2/spend_limits").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "sl1"}]})
    )
    result = await connector.list_spend_limits()
    assert result["items"][0]["id"] == "sl1"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{BREX_BASE}/v2/users/me").mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(200, json={"id": "after-retry"}),
        ]
    )
    result = await connector.get_current_user()
    assert route.call_count == 2
    assert result["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{BREX_BASE}/v2/users").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"items": []}),
        ]
    )
    result = await connector.list_users()
    assert route.call_count == 2
    assert result == {"items": []}


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert BrexConnector.CONNECTOR_TYPE == "brex"


def test_auth_type_class_attr():
    assert BrexConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(BrexConnector, "REQUIRED_CONFIG_KEYS")
    assert BrexConnector.REQUIRED_CONFIG_KEYS == ["access_token"]


def test_status_map_defined():
    assert hasattr(BrexConnector, "_STATUS_MAP")
    assert 401 in BrexConnector._STATUS_MAP
    assert 403 in BrexConnector._STATUS_MAP
    assert 429 in BrexConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = BrexConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = BrexConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer ID scheme: f"{tenant_id}_{source_id}"
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_expense_id_is_tenant_scoped():
    from helpers.normalizer import normalize_expense

    doc = normalize_expense(
        {"id": "exp-1", "memo": "Lunch", "amount": {"amount": 1500, "currency": "USD"}},
        connector_id="conn-x",
        tenant_id="tenant-y",
    )
    assert doc.id == "tenant-y_exp-1"
    assert doc.source_id == "exp-1"
    assert doc.source == "brex.expenses"


def test_normalize_transaction_id_is_tenant_scoped():
    from helpers.normalizer import normalize_transaction

    doc = normalize_transaction(
        {"id": "tx-1", "description": "Coffee"},
        connector_id="conn-x",
        tenant_id="tenant-y",
    )
    assert doc.id == "tenant-y_tx-1"
    assert doc.source == "brex.transactions"


def test_normalize_user_id_is_tenant_scoped():
    from helpers.normalizer import normalize_user

    doc = normalize_user(
        {"id": "u-1", "first_name": "Ada", "last_name": "Lovelace", "email": "ada@b.com"},
        connector_id="conn-x",
        tenant_id="tenant-y",
    )
    assert doc.id == "tenant-y_u-1"
    assert doc.title == "Ada Lovelace"
    assert doc.source == "brex.users"
