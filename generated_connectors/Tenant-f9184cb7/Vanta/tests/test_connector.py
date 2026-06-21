"""Unit tests for VantaConnector — respx-mocked, zero real network I/O."""
from __future__ import annotations

import httpx
import pytest
import respx
from shared.base_connector import AuthStatus, ConnectorHealth

from connector import VantaConnector
from exceptions import VantaAuthError, VantaError, VantaNotFound

from tests.conftest import (
    CONNECTOR_ID,
    TENANT_ID,
    TEST_ACCESS_TOKEN,
    TEST_CLIENT_ID,
    TEST_CLIENT_SECRET,
    TEST_CONFIG,
    TEST_SCOPES,
    VANTA_BASE,
    VANTA_TOKEN_URL,
)


TOKEN_RESPONSE = {
    "access_token": TEST_ACCESS_TOKEN,
    "expires_in": 3600,
    "token_type": "Bearer",
    "scope": TEST_SCOPES,
}


def _token_route():
    return respx.post(VANTA_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_install_success(connector):
    _token_route()
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
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_install_token_endpoint_401_returns_unhealthy(connector):
    respx.post(VANTA_TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.UNHEALTHY
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# OAuth2 client_credentials grant
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_token_endpoint_called_with_client_credentials_body(connector):
    route = _token_route()
    info = await connector.authorize()
    assert route.called
    sent = route.calls[0].request
    body = sent.content.decode()
    assert "grant_type=client_credentials" in body
    assert f"client_id={TEST_CLIENT_ID}" in body
    assert f"client_secret={TEST_CLIENT_SECRET}" in body
    assert info.access_token == TEST_ACCESS_TOKEN
    assert info.token_type == "Bearer"


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_uses_bearer_token(connector):
    _token_route()
    route = respx.get(f"{VANTA_BASE}/frameworks").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await connector.list_frameworks(page_size=1)
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_ACCESS_TOKEN}"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_vanta_auth_error(primed_connector):
    """A 401 mid-call triggers a single re-mint; if the re-mint also 401s the
    error surfaces."""
    respx.post(VANTA_TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    respx.get(f"{VANTA_BASE}/frameworks").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    with pytest.raises(VantaAuthError):
        await primed_connector.list_frameworks()


# ═══════════════════════════════════════════════════════════════════════════
# health_check
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    _token_route()
    respx.get(f"{VANTA_BASE}/frameworks").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "f1"}]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error_401(connector):
    _token_route()
    respx.get(f"{VANTA_BASE}/frameworks").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    respx.post(VANTA_TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_returns_unhealthy(primed_connector):
    respx.get(f"{VANTA_BASE}/frameworks").mock(
        return_value=httpx.Response(403, json={"message": "scope insufficient"})
    )
    result = await primed_connector.health_check()
    assert result.health == ConnectorHealth.UNHEALTHY
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Frameworks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_frameworks_success(primed_connector):
    payload = {
        "results": [{"id": "f1", "name": "SOC 2 Type II"}],
        "pageInfo": {"endCursor": None},
    }
    route = respx.get(f"{VANTA_BASE}/frameworks").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await primed_connector.list_frameworks(page_size=5)
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("pageSize") == "5"
    assert result["results"][0]["name"] == "SOC 2 Type II"


@respx.mock
@pytest.mark.asyncio
async def test_get_framework_success(primed_connector):
    fid = "fr-99"
    respx.get(f"{VANTA_BASE}/frameworks/{fid}").mock(
        return_value=httpx.Response(200, json={"id": fid, "name": "ISO 27001"})
    )
    result = await primed_connector.get_framework(fid)
    assert result["id"] == fid


@respx.mock
@pytest.mark.asyncio
async def test_get_framework_not_found(primed_connector):
    fid = "missing"
    respx.get(f"{VANTA_BASE}/frameworks/{fid}").mock(
        return_value=httpx.Response(404, json={"message": "framework not found"})
    )
    with pytest.raises(VantaNotFound):
        await primed_connector.get_framework(fid)


# ═══════════════════════════════════════════════════════════════════════════
# Controls
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_controls_with_framework_filter(primed_connector):
    route = respx.get(f"{VANTA_BASE}/controls").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "c1"}]})
    )
    result = await primed_connector.list_controls(
        page_size=20, framework_id="fr-1"
    )
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("frameworkId") == "fr-1"
    assert qs.get("pageSize") == "20"
    assert result["results"][0]["id"] == "c1"


@respx.mock
@pytest.mark.asyncio
async def test_get_control_success(primed_connector):
    cid = "ctrl-1"
    respx.get(f"{VANTA_BASE}/controls/{cid}").mock(
        return_value=httpx.Response(200, json={"id": cid, "name": "Access Review"})
    )
    result = await primed_connector.get_control(cid)
    assert result["id"] == cid


# ═══════════════════════════════════════════════════════════════════════════
# Vendors
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_vendors_success(primed_connector):
    respx.get(f"{VANTA_BASE}/vendors").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "v1", "name": "Datadog"}]})
    )
    result = await primed_connector.list_vendors(page_size=10)
    assert result["results"][0]["name"] == "Datadog"


@respx.mock
@pytest.mark.asyncio
async def test_get_vendor_success(primed_connector):
    vid = "vendor-42"
    respx.get(f"{VANTA_BASE}/vendors/{vid}").mock(
        return_value=httpx.Response(200, json={"id": vid, "name": "Auth0"})
    )
    result = await primed_connector.get_vendor(vid)
    assert result["id"] == vid


# ═══════════════════════════════════════════════════════════════════════════
# Personnel
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_personnel_with_includes_inactive(primed_connector):
    route = respx.get(f"{VANTA_BASE}/personnel").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "p1", "email": "alice@ex.com"}]})
    )
    result = await primed_connector.list_personnel(page_size=25, includes_inactive=True)
    qs = route.calls[0].request.url.params
    assert qs.get("includesInactive") == "true"
    assert qs.get("pageSize") == "25"
    assert result["results"][0]["email"] == "alice@ex.com"


@respx.mock
@pytest.mark.asyncio
async def test_get_personnel_success(primed_connector):
    pid = "person-1"
    respx.get(f"{VANTA_BASE}/personnel/{pid}").mock(
        return_value=httpx.Response(200, json={"id": pid, "displayName": "Bob"})
    )
    result = await primed_connector.get_personnel(pid)
    assert result["id"] == pid


# ═══════════════════════════════════════════════════════════════════════════
# Risks / Incidents / Documents / Tests / Findings / Audits
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_risks_success(primed_connector):
    respx.get(f"{VANTA_BASE}/risks").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "r1"}]})
    )
    result = await primed_connector.list_risks()
    assert result["results"][0]["id"] == "r1"


@respx.mock
@pytest.mark.asyncio
async def test_list_incidents_with_filters(primed_connector):
    route = respx.get(f"{VANTA_BASE}/incidents").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await primed_connector.list_incidents(severity="high", status="open")
    qs = route.calls[0].request.url.params
    assert qs.get("severity") == "high"
    assert qs.get("status") == "open"


@respx.mock
@pytest.mark.asyncio
async def test_list_documents_success(primed_connector):
    respx.get(f"{VANTA_BASE}/documents").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "doc-1"}]})
    )
    result = await primed_connector.list_documents()
    assert result["results"][0]["id"] == "doc-1"


@respx.mock
@pytest.mark.asyncio
async def test_list_tests_with_status_filter(primed_connector):
    route = respx.get(f"{VANTA_BASE}/tests").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "t1"}]})
    )
    await primed_connector.list_tests(test_status="passing")
    qs = route.calls[0].request.url.params
    assert qs.get("status") == "passing"


@respx.mock
@pytest.mark.asyncio
async def test_list_findings_with_severity_filter(primed_connector):
    route = respx.get(f"{VANTA_BASE}/findings").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "f1"}]})
    )
    await primed_connector.list_findings(severity="critical", status="open")
    qs = route.calls[0].request.url.params
    assert qs.get("severity") == "critical"
    assert qs.get("status") == "open"


@respx.mock
@pytest.mark.asyncio
async def test_list_audits_success(primed_connector):
    respx.get(f"{VANTA_BASE}/audits").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "audit-1"}]})
    )
    result = await primed_connector.list_audits()
    assert result["results"][0]["id"] == "audit-1"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(primed_connector, no_retry_sleep):
    route = respx.get(f"{VANTA_BASE}/frameworks").mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(200, json={"results": [{"id": "after-retry"}]}),
        ]
    )
    result = await primed_connector.list_frameworks(page_size=1)
    assert route.call_count == 2
    assert result["results"][0]["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(primed_connector, no_retry_sleep):
    route = respx.get(f"{VANTA_BASE}/frameworks").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"results": []}),
        ]
    )
    result = await primed_connector.list_frameworks()
    assert route.call_count == 2
    assert result == {"results": []}


@respx.mock
@pytest.mark.asyncio
async def test_401_triggers_single_remint_then_success(primed_connector):
    """A single 401 forces one token re-mint, then the retried request returns 200."""
    _token_route()
    route = respx.get(f"{VANTA_BASE}/frameworks").mock(
        side_effect=[
            httpx.Response(401, json={"message": "expired"}),
            httpx.Response(200, json={"results": [{"id": "after-remint"}]}),
        ]
    )
    result = await primed_connector.list_frameworks()
    assert route.call_count == 2
    assert result["results"][0]["id"] == "after-remint"


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert VantaConnector.CONNECTOR_TYPE == "vanta"


def test_auth_type_class_attr():
    assert VantaConnector.AUTH_TYPE == "oauth2_client_credentials"


def test_required_config_keys_defined():
    assert hasattr(VantaConnector, "REQUIRED_CONFIG_KEYS")
    assert "client_id" in VantaConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in VantaConnector.REQUIRED_CONFIG_KEYS


def test_status_map_covers_401_403_429():
    sm = VantaConnector._STATUS_MAP
    assert 401 in sm and 403 in sm and 429 in sm


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = VantaConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = VantaConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    assert c1.http_client is not c2.http_client


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer sanity (NormalizedDocument id is connector-scoped)
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_vendor_id_is_connector_scoped():
    from helpers.normalizer import normalize_vendor

    raw = {"id": "v-77", "name": "Acme Inc", "description": "SaaS vendor"}
    doc = normalize_vendor(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{CONNECTOR_ID}_vendor_v-77"
    assert doc.source_id == "v-77"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["kind"] == "vanta.vendor"
