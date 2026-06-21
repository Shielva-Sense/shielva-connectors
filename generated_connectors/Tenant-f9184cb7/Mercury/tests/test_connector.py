"""Unit tests for MercuryConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import MercuryConnector
from exceptions import MercuryAuthError, MercuryError, MercuryNotFound

from tests.conftest import (
    CONNECTOR_ID,
    MERCURY_BASE,
    TENANT_ID,
    TEST_ACCOUNT_ID,
    TEST_API_TOKEN,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_install_success(connector):
    respx.get(f"{MERCURY_BASE}/accounts").mock(
        return_value=httpx.Response(200, json={"accounts": []})
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_token(connector):
    connector.api_token = ""
    connector.http_client._api_token = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_install_auth_error_401(connector):
    respx.get(f"{MERCURY_BASE}/accounts").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer prefix) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_uses_bearer_prefix(connector):
    """Mercury sends the token as `Authorization: Bearer <token>`."""
    route = respx.get(f"{MERCURY_BASE}/accounts").mock(
        return_value=httpx.Response(200, json={"accounts": []})
    )
    await connector.list_accounts()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_API_TOKEN}"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_mercury_auth_error(connector):
    respx.get(f"{MERCURY_BASE}/accounts").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    with pytest.raises(MercuryAuthError):
        await connector.list_accounts()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{MERCURY_BASE}/accounts").mock(
        return_value=httpx.Response(200, json={"accounts": [{"id": "a1"}]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{MERCURY_BASE}/accounts").mock(
        return_value=httpx.Response(401, json={"message": "bad token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Accounts
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_accounts_success(connector):
    payload = {
        "accounts": [
            {"id": "acc_1", "name": "Operating", "status": "active"},
            {"id": "acc_2", "name": "Savings", "status": "active"},
        ]
    }
    route = respx.get(f"{MERCURY_BASE}/accounts").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_accounts()
    assert result == payload
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_get_account_success(connector):
    respx.get(f"{MERCURY_BASE}/account/acc_1").mock(
        return_value=httpx.Response(
            200,
            json={"id": "acc_1", "name": "Operating", "availableBalance": 1234.56},
        )
    )
    result = await connector.get_account("acc_1")
    assert result["id"] == "acc_1"
    assert result["availableBalance"] == 1234.56


@respx.mock
@pytest.mark.asyncio
async def test_get_account_not_found(connector):
    respx.get(f"{MERCURY_BASE}/account/missing").mock(
        return_value=httpx.Response(404, json={"message": "no such account"})
    )
    with pytest.raises(MercuryNotFound):
        await connector.get_account("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Transactions
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_account_transactions_with_filters(connector):
    payload = {"transactions": [{"id": "tx1", "amount": -42.0, "status": "sent"}]}
    route = respx.get(f"{MERCURY_BASE}/account/acc_1/transactions").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_account_transactions(
        "acc_1",
        limit=25,
        offset=0,
        status="sent",
        start="2026-01-01",
        end="2026-06-01",
        order="asc",
        search="payroll",
    )
    assert result["transactions"][0]["id"] == "tx1"
    sent_url = str(route.calls.last.request.url)
    assert "limit=25" in sent_url
    assert "status=sent" in sent_url
    assert "start=2026-01-01" in sent_url
    assert "end=2026-06-01" in sent_url
    assert "order=asc" in sent_url
    assert "search=payroll" in sent_url


@respx.mock
@pytest.mark.asyncio
async def test_get_transaction_success(connector):
    respx.get(f"{MERCURY_BASE}/account/acc_1/transaction/tx99").mock(
        return_value=httpx.Response(
            200, json={"id": "tx99", "amount": -10.0, "status": "sent"}
        )
    )
    result = await connector.get_transaction("acc_1", "tx99")
    assert result["id"] == "tx99"


@respx.mock
@pytest.mark.asyncio
async def test_get_transaction_404(connector):
    respx.get(f"{MERCURY_BASE}/account/acc_1/transaction/missing").mock(
        return_value=httpx.Response(404, json={"message": "no such transaction"})
    )
    with pytest.raises(MercuryNotFound):
        await connector.get_transaction("acc_1", "missing")


# ═══════════════════════════════════════════════════════════════════════════
# Recipients
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_recipients_success(connector):
    respx.get(f"{MERCURY_BASE}/recipients").mock(
        return_value=httpx.Response(200, json={"recipients": [{"id": "rec_1", "name": "Acme"}]})
    )
    result = await connector.list_recipients()
    assert result["recipients"][0]["id"] == "rec_1"


@respx.mock
@pytest.mark.asyncio
async def test_get_recipient_success(connector):
    respx.get(f"{MERCURY_BASE}/recipient/rec_1").mock(
        return_value=httpx.Response(200, json={"id": "rec_1", "name": "Acme"})
    )
    result = await connector.get_recipient("rec_1")
    assert result["id"] == "rec_1"


@respx.mock
@pytest.mark.asyncio
async def test_create_recipient_posts_body(connector):
    route = respx.post(f"{MERCURY_BASE}/recipient").mock(
        return_value=httpx.Response(200, json={"id": "rec_new", "name": "Acme Inc"})
    )
    result = await connector.create_recipient(
        name="Acme Inc",
        emails=["billing@acme.test"],
        default_payment_method="ach",
    )
    assert result["id"] == "rec_new"
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["name"] == "Acme Inc"
    assert body["emails"] == ["billing@acme.test"]
    assert body["defaultPaymentMethod"] == "ach"


# ═══════════════════════════════════════════════════════════════════════════
# send_payment() — Idempotency-Key header
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_send_payment_includes_idempotency_key(connector):
    route = respx.post(f"{MERCURY_BASE}/account/acc_1/transactions").mock(
        return_value=httpx.Response(
            200, json={"id": "tx_new", "status": "pending", "amount": 250.0}
        )
    )
    idem_key = "idem-abc-123"
    result = await connector.send_payment(
        account_id="acc_1",
        recipient_id="rec_1",
        amount=250.0,
        payment_method="ach",
        idempotency_key=idem_key,
        note="Test ACH",
    )
    assert result["id"] == "tx_new"
    sent_headers = route.calls.last.request.headers
    assert sent_headers.get("Idempotency-Key") == idem_key
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["recipientId"] == "rec_1"
    assert body["amount"] == 250.0
    assert body["paymentMethod"] == "ach"
    assert body["note"] == "Test ACH"


@pytest.mark.asyncio
async def test_send_payment_rejects_missing_idempotency_key(connector):
    with pytest.raises(MercuryError):
        await connector.send_payment(
            account_id="acc_1",
            recipient_id="rec_1",
            amount=10.0,
            payment_method="ach",
            idempotency_key="",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Statements
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_statements_success(connector):
    payload = {
        "statements": [{"id": "stmt_1", "start": "2026-01-01", "end": "2026-01-31"}]
    }
    route = respx.get(f"{MERCURY_BASE}/account/acc_1/statements").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_statements("acc_1", "2026-01-01", "2026-01-31")
    assert result["statements"][0]["id"] == "stmt_1"
    url = str(route.calls.last.request.url)
    assert "start=2026-01-01" in url
    assert "end=2026-01-31" in url


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{MERCURY_BASE}/accounts").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow down"}),
            httpx.Response(200, json={"accounts": [{"id": "after-retry"}]}),
        ]
    )
    result = await connector.list_accounts()
    assert route.call_count == 2
    assert result["accounts"][0]["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{MERCURY_BASE}/accounts").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"accounts": []}),
        ]
    )
    result = await connector.list_accounts()
    assert route.call_count == 2
    assert result == {"accounts": []}


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert MercuryConnector.CONNECTOR_TYPE == "mercury"


def test_auth_type_class_attr():
    assert MercuryConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(MercuryConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_token" in MercuryConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(MercuryConnector, "_STATUS_MAP")
    assert 401 in MercuryConnector._STATUS_MAP
    assert 403 in MercuryConnector._STATUS_MAP
    assert 429 in MercuryConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = MercuryConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = MercuryConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — NormalizedDocument id format
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_transaction_id_is_tenant_scoped():
    from helpers.normalizer import normalize_transaction

    raw = {"id": "tx_abc", "amount": 100.0, "counterpartyName": "Vendor"}
    doc = normalize_transaction(raw, "conn-x", "tenant-y", account_id="acc_1")
    assert doc.id == "tenant-y_tx_abc"
    assert doc.source_id == "tx_abc"
    assert doc.metadata["accountId"] == "acc_1"


def test_normalize_account_id_is_tenant_scoped():
    from helpers.normalizer import normalize_account

    raw = {"id": "acc_1", "name": "Operating", "kind": "checking"}
    doc = normalize_account(raw, "conn-x", "tenant-y")
    assert doc.id == "tenant-y_acc_1"
    assert doc.source_id == "acc_1"
