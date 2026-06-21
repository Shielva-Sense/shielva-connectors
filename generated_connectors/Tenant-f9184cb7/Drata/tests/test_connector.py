"""Unit tests for DrataConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import DrataConnector
from exceptions import DrataAuthError, DrataError, DrataNotFound

from tests.conftest import (
    CONNECTOR_ID,
    DRATA_BASE,
    TENANT_ID,
    TEST_API_KEY,
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
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_empty_config_does_not_raise():
    c = DrataConnector(tenant_id="t", connector_id="empty", config={})
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer prefix) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer(connector):
    """Connector must send the api_key as Bearer in Authorization."""
    route = respx.get(f"{DRATA_BASE}/personnel").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    await connector.list_personnel(limit=1)
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_API_KEY}"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_drata_auth_error(connector):
    respx.get(f"{DRATA_BASE}/personnel").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    with pytest.raises(DrataAuthError):
        await connector.list_personnel()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{DRATA_BASE}/personnel").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "p1"}]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_token_expired(connector):
    respx.get(f"{DRATA_BASE}/personnel").mock(
        return_value=httpx.Response(401, json={"message": "Invalid key"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_invalid_credentials(connector):
    respx.get(f"{DRATA_BASE}/personnel").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Personnel
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_personnel_query_params(connector):
    route = respx.get(f"{DRATA_BASE}/personnel").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "p1"}]})
    )
    result = await connector.list_personnel(limit=25, offset=10, status="ACTIVE")
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("limit") == "25"
    assert qs.get("offset") == "10"
    assert qs.get("status") == "ACTIVE"
    assert result["data"][0]["id"] == "p1"


@respx.mock
@pytest.mark.asyncio
async def test_get_personnel_success(connector):
    pid = "p99"
    respx.get(f"{DRATA_BASE}/personnel/{pid}").mock(
        return_value=httpx.Response(200, json={"id": pid, "firstName": "Ada"})
    )
    result = await connector.get_personnel(pid)
    assert result["id"] == pid


@respx.mock
@pytest.mark.asyncio
async def test_get_personnel_not_found(connector):
    respx.get(f"{DRATA_BASE}/personnel/missing").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    with pytest.raises(DrataNotFound):
        await connector.get_personnel("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Controls
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_controls_success(connector):
    respx.get(f"{DRATA_BASE}/controls").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "c1", "name": "AC-1"}]})
    )
    result = await connector.list_controls(limit=50)
    assert result["data"][0]["name"] == "AC-1"


@respx.mock
@pytest.mark.asyncio
async def test_get_control_success(connector):
    respx.get(f"{DRATA_BASE}/controls/c42").mock(
        return_value=httpx.Response(200, json={"id": "c42", "name": "Logical Access"})
    )
    result = await connector.get_control("c42")
    assert result["id"] == "c42"


# ═══════════════════════════════════════════════════════════════════════════
# Evidence
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_evidence_with_control_id(connector):
    route = respx.get(f"{DRATA_BASE}/evidence").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "e1"}]})
    )
    result = await connector.list_evidence(limit=10, control_id="ctrl-7")
    qs = route.calls[0].request.url.params
    assert qs.get("controlId") == "ctrl-7"
    assert result["data"][0]["id"] == "e1"


# ═══════════════════════════════════════════════════════════════════════════
# Risks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_risks_success(connector):
    respx.get(f"{DRATA_BASE}/risks").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "r1", "severity": "HIGH"}]})
    )
    result = await connector.list_risks()
    assert result["data"][0]["severity"] == "HIGH"


# ═══════════════════════════════════════════════════════════════════════════
# Vendors
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_vendors_success(connector):
    respx.get(f"{DRATA_BASE}/vendors").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "v1"}]})
    )
    result = await connector.list_vendors()
    assert result["data"][0]["id"] == "v1"


@respx.mock
@pytest.mark.asyncio
async def test_get_vendor_success(connector):
    respx.get(f"{DRATA_BASE}/vendors/v9").mock(
        return_value=httpx.Response(200, json={"id": "v9", "name": "AWS"})
    )
    result = await connector.get_vendor("v9")
    assert result["name"] == "AWS"


# ═══════════════════════════════════════════════════════════════════════════
# Audits / Policies / Devices / Frameworks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_audits_success(connector):
    respx.get(f"{DRATA_BASE}/audits").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a1"}]})
    )
    result = await connector.list_audits()
    assert result["data"][0]["id"] == "a1"


@respx.mock
@pytest.mark.asyncio
async def test_list_policies_success(connector):
    respx.get(f"{DRATA_BASE}/policies").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "pol1"}]})
    )
    result = await connector.list_policies()
    assert result["data"][0]["id"] == "pol1"


@respx.mock
@pytest.mark.asyncio
async def test_list_devices_success(connector):
    respx.get(f"{DRATA_BASE}/devices").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "d1"}]})
    )
    result = await connector.list_devices()
    assert result["data"][0]["id"] == "d1"


@respx.mock
@pytest.mark.asyncio
async def test_list_frameworks_success(connector):
    respx.get(f"{DRATA_BASE}/frameworks").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "fw1", "name": "SOC2"}]})
    )
    result = await connector.list_frameworks()
    assert result["data"][0]["name"] == "SOC2"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{DRATA_BASE}/personnel").mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(200, json={"data": [{"id": "after-retry"}]}),
        ]
    )
    result = await connector.list_personnel(limit=1)
    assert route.call_count == 2
    assert result["data"][0]["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{DRATA_BASE}/personnel").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    result = await connector.list_personnel()
    assert route.call_count == 2
    assert result == {"data": []}


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert DrataConnector.CONNECTOR_TYPE == "drata"


def test_auth_type_class_attr():
    assert DrataConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(DrataConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in DrataConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = DrataConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = DrataConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
