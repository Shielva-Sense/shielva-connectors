"""Unit tests for RampConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import RampConnector
from exceptions import RampAuthError, RampError, RampNotFound

from tests.conftest import (
    CONNECTOR_ID,
    RAMP_BASE,
    TENANT_ID,
    TEST_CLIENT_ID,
    TEST_CLIENT_SECRET,
    TEST_CONFIG,
    TOKEN_URL,
    token_response,
)


def _mock_token(status: int = 200):
    """Install a respx mock for the OAuth2 token endpoint."""
    if status == 200:
        return respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=token_response())
        )
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(status, json={"error": "invalid_client"})
    )


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_install_success(connector):
    _mock_token()
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_client_id(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_client_secret(connector):
    connector.config.pop("client_secret", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@respx.mock
@pytest.mark.asyncio
async def test_install_credentials_rejected(connector):
    _mock_token(status=401)
    result = await connector.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# OAuth2 client_credentials wire shape
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_token_uses_basic_auth_and_client_credentials_grant(connector):
    """Token call uses HTTP Basic(client_id:client_secret) + grant_type=client_credentials."""
    route = _mock_token()
    respx.get(f"{RAMP_BASE}/users").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    await connector.list_users(page_size=1)  # triggers token mint then API call
    assert route.called
    sent = route.calls[0].request
    auth = sent.headers.get("authorization", "")
    assert auth.startswith("Basic ")
    import base64
    expected = base64.b64encode(
        f"{TEST_CLIENT_ID}:{TEST_CLIENT_SECRET}".encode("utf-8")
    ).decode("ascii")
    assert auth == f"Basic {expected}"
    body = sent.content.decode()
    assert "grant_type=client_credentials" in body


@respx.mock
@pytest.mark.asyncio
async def test_api_call_sends_bearer_access_token(connector):
    _mock_token()
    route = respx.get(f"{RAMP_BASE}/users").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    await connector.list_users(page_size=1)
    assert route.called
    auth = route.calls[0].request.headers.get("authorization")
    assert auth == "Bearer tok_abc123"


# ═══════════════════════════════════════════════════════════════════════════
# health_check
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/users").mock(
        return_value=httpx.Response(200, json={"data": [], "page": {"next": None}})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_failed(connector, no_retry_sleep):
    _mock_token()
    respx.get(f"{RAMP_BASE}/users").mock(
        return_value=httpx.Response(401, json={"error": "invalid_token"})
    )
    # Re-mint token on 401 also returns 401 path → RampAuthError surfaced.
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_users_passes_filters(connector):
    _mock_token()
    route = respx.get(f"{RAMP_BASE}/users").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "u1"}]})
    )
    result = await connector.list_users(department_id="dep_1", role="BUSINESS_USER", page_size=10)
    assert result["data"][0]["id"] == "u1"
    qp = route.calls[0].request.url.params
    assert qp.get("department_id") == "dep_1"
    assert qp.get("role") == "BUSINESS_USER"
    assert qp.get("page_size") == "10"


@respx.mock
@pytest.mark.asyncio
async def test_get_user_not_found(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/users/u_missing").mock(
        return_value=httpx.Response(404, json={"error": "not_found"})
    )
    with pytest.raises(RampNotFound):
        await connector.get_user("u_missing")


@respx.mock
@pytest.mark.asyncio
async def test_invite_user_sends_idempotency_key(connector):
    _mock_token()
    route = respx.post(f"{RAMP_BASE}/users/deferred").mock(
        return_value=httpx.Response(200, json={"id": "u_new"})
    )
    result = await connector.invite_user(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        role="BUSINESS_USER",
        idempotency_key="idem-1234",
    )
    assert result["id"] == "u_new"
    req = route.calls[0].request
    assert req.headers.get("idempotency-key") == "idem-1234"
    import json as _json
    body = _json.loads(req.content.decode())
    assert body["email"] == "ada@example.com"
    assert body["role"] == "BUSINESS_USER"


# ═══════════════════════════════════════════════════════════════════════════
# Cards
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_cards_with_user_filter(connector):
    _mock_token()
    route = respx.get(f"{RAMP_BASE}/cards").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "card_1"}]})
    )
    result = await connector.list_cards(user_id="u1", is_physical=True)
    assert result["data"][0]["id"] == "card_1"
    qp = route.calls[0].request.url.params
    assert qp.get("user_id") == "u1"
    assert qp.get("is_physical") == "true"


@respx.mock
@pytest.mark.asyncio
async def test_get_card_success(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/cards/card_42").mock(
        return_value=httpx.Response(200, json={"id": "card_42", "display_name": "Marketing"})
    )
    result = await connector.get_card("card_42")
    assert result["id"] == "card_42"


# ═══════════════════════════════════════════════════════════════════════════
# Transactions
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_transactions_date_range(connector):
    _mock_token()
    route = respx.get(f"{RAMP_BASE}/transactions").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "tx_1"}]})
    )
    result = await connector.list_transactions(
        start="2026-01-01T00:00:00Z",
        end="2026-01-31T23:59:59Z",
        page_size=25,
    )
    assert result["data"][0]["id"] == "tx_1"
    qp = route.calls[0].request.url.params
    assert qp.get("from_date") == "2026-01-01T00:00:00Z"
    assert qp.get("to_date") == "2026-01-31T23:59:59Z"
    assert qp.get("page_size") == "25"


@respx.mock
@pytest.mark.asyncio
async def test_get_transaction_success(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/transactions/tx_999").mock(
        return_value=httpx.Response(200, json={"id": "tx_999", "amount": 1234})
    )
    result = await connector.get_transaction("tx_999")
    assert result == {"id": "tx_999", "amount": 1234}


# ═══════════════════════════════════════════════════════════════════════════
# Departments / Locations / Vendors / Bills / Reimbursements
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_departments(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/departments").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "dep_1"}]})
    )
    result = await connector.list_departments()
    assert result["data"][0]["id"] == "dep_1"


@respx.mock
@pytest.mark.asyncio
async def test_list_locations(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/locations").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "loc_1"}]})
    )
    result = await connector.list_locations()
    assert result["data"][0]["id"] == "loc_1"


@respx.mock
@pytest.mark.asyncio
async def test_list_vendors(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/vendors").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "vend_1"}]})
    )
    result = await connector.list_vendors()
    assert result["data"][0]["id"] == "vend_1"


@respx.mock
@pytest.mark.asyncio
async def test_list_bills(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/bills").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "bill_1"}]})
    )
    result = await connector.list_bills(page_size=5)
    assert result["data"][0]["id"] == "bill_1"


@respx.mock
@pytest.mark.asyncio
async def test_list_reimbursements(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/reimbursements").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "re_1"}]})
    )
    result = await connector.list_reimbursements(user_id="u1")
    assert result["data"][0]["id"] == "re_1"


# ═══════════════════════════════════════════════════════════════════════════
# Memos
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_memos(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/memos").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "memo_1"}]})
    )
    result = await connector.list_memos()
    assert result["data"][0]["id"] == "memo_1"


@respx.mock
@pytest.mark.asyncio
async def test_get_memo_success(connector):
    _mock_token()
    respx.get(f"{RAMP_BASE}/memos/memo_42").mock(
        return_value=httpx.Response(200, json={"id": "memo_42", "text": "lunch"})
    )
    result = await connector.get_memo("memo_42")
    assert result["id"] == "memo_42"


# ═══════════════════════════════════════════════════════════════════════════
# Refresh-on-401 and retry-on-429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_refresh_on_401_then_success(connector, no_retry_sleep):
    """A single 401 invalidates the cached token and retries with a fresh one."""
    token_route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok_old", "token_type": "Bearer", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok_new", "token_type": "Bearer", "expires_in": 3600}),
        ]
    )
    user_route = respx.get(f"{RAMP_BASE}/users").mock(
        side_effect=[
            httpx.Response(401, json={"error": "invalid_token"}),
            httpx.Response(200, json={"data": [{"id": "u1"}]}),
        ]
    )
    result = await connector.list_users()
    assert result["data"][0]["id"] == "u1"
    assert token_route.call_count == 2  # initial mint + refresh after 401
    assert user_route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    _mock_token()
    user_route = respx.get(f"{RAMP_BASE}/users").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate_limited"}),
            httpx.Response(200, json={"data": [{"id": "u_after_429"}]}),
        ]
    )
    result = await connector.list_users()
    assert result["data"][0]["id"] == "u_after_429"
    assert user_route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    _mock_token()
    user_route = respx.get(f"{RAMP_BASE}/users").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    result = await connector.list_users()
    assert result == {"data": []}
    assert user_route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert RampConnector.CONNECTOR_TYPE == "ramp"


def test_auth_type_class_attr():
    assert RampConnector.AUTH_TYPE == "oauth2_client_credentials"


def test_required_config_keys_defined():
    assert hasattr(RampConnector, "REQUIRED_CONFIG_KEYS")
    assert "client_id" in RampConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in RampConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(RampConnector, "_STATUS_MAP")
    assert 401 in RampConnector._STATUS_MAP
    assert 403 in RampConnector._STATUS_MAP
    assert 429 in RampConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = RampConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = RampConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — NormalizedDocument id = f"{tenant_id}_{source_id}"
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_transaction_id_format():
    from helpers.normalizer import normalize_transaction

    doc = normalize_transaction(
        {"id": "tx_001", "amount": 1500, "currency_code": "USD", "merchant_name": "Coffee"},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    assert doc.id == f"{TENANT_ID}_tx_001"
    assert doc.source_id == "tx_001"
    assert doc.metadata["kind"] == "ramp.transaction"


def test_normalize_user_id_format():
    from helpers.normalizer import normalize_user

    doc = normalize_user(
        {"id": "u_001", "email": "a@b.com", "first_name": "Ada", "last_name": "L."},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    assert doc.id == f"{TENANT_ID}_u_001"
    assert doc.title == "Ada L."
    assert doc.metadata["kind"] == "ramp.user"
