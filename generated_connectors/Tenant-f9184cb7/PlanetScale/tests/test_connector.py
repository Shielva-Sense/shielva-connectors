"""Unit tests for PlanetScaleConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import PlanetScaleConnector
from exceptions import (
    PlanetScaleAuthError,
    PlanetScaleBadRequestError,
    PlanetScaleConflictError,
    PlanetScaleError,
    PlanetScaleNotFoundError,
)

from tests.conftest import (
    CONNECTOR_ID,
    PLANETSCALE_BASE,
    TENANT_ID,
    TEST_CONFIG,
    TEST_DB,
    TEST_ORG,
    TEST_TOKEN,
    TEST_TOKEN_ID,
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
async def test_install_missing_service_token_id(connector):
    connector.config.pop("service_token_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_service_token(connector):
    connector.config.pop("service_token", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (literal id:token, no Bearer) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_id_colon_token_no_bearer(connector):
    """Connector must send `<id>:<token>` RAW in Authorization (no 'Bearer ' prefix)."""
    route = respx.get(f"{PLANETSCALE_BASE}/organizations").mock(
        return_value=httpx.Response(200, json={"data": []}),
    )
    await connector.list_organizations(page=1, per_page=1)
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"{TEST_TOKEN_ID}:{TEST_TOKEN}"
    assert not sent_auth.lower().startswith("bearer ")


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_planetscale_auth_error(connector):
    respx.get(f"{PLANETSCALE_BASE}/organizations").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    with pytest.raises(PlanetScaleAuthError):
        await connector.list_organizations()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{PLANETSCALE_BASE}/organizations").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "o1", "name": "org-1"}]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_is_token_expired(connector):
    respx.get(f"{PLANETSCALE_BASE}/organizations").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_is_invalid_credentials(connector):
    respx.get(f"{PLANETSCALE_BASE}/organizations").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Organizations
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_organizations_success(connector):
    payload = {"data": [{"id": "o1", "name": "test-org"}], "has_next": False}
    route = respx.get(f"{PLANETSCALE_BASE}/organizations").mock(
        return_value=httpx.Response(200, json=payload),
    )
    result = await connector.list_organizations(page=1, per_page=10)
    assert route.called
    assert result["data"][0]["name"] == "test-org"
    qs = route.calls[0].request.url.params
    assert qs.get("page") == "1"
    assert qs.get("per_page") == "10"


@respx.mock
@pytest.mark.asyncio
async def test_get_organization_success(connector):
    name = "acme"
    respx.get(f"{PLANETSCALE_BASE}/organizations/{name}").mock(
        return_value=httpx.Response(200, json={"id": "o9", "name": name})
    )
    result = await connector.get_organization(name)
    assert result["name"] == name


@respx.mock
@pytest.mark.asyncio
async def test_get_organization_not_found(connector):
    respx.get(f"{PLANETSCALE_BASE}/organizations/missing").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    with pytest.raises(PlanetScaleNotFoundError):
        await connector.get_organization("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Databases
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_databases_uses_default_org(connector):
    payload = {"data": [{"id": "db1", "name": "billing"}]}
    route = respx.get(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases"
    ).mock(return_value=httpx.Response(200, json=payload))
    result = await connector.list_databases()
    assert route.called
    assert result["data"][0]["name"] == "billing"


@respx.mock
@pytest.mark.asyncio
async def test_get_database(connector):
    respx.get(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/billing"
    ).mock(return_value=httpx.Response(200, json={"id": "db1", "name": "billing"}))
    result = await connector.get_database(name="billing")
    assert result["name"] == "billing"


@respx.mock
@pytest.mark.asyncio
async def test_create_database_sends_correct_body(connector):
    route = respx.post(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases"
    ).mock(return_value=httpx.Response(201, json={"id": "db1", "name": "new-db", "plan": "hobby"}))

    result = await connector.create_database(
        name="new-db",
        plan="hobby",
        cluster_size="PS_10",
        region={"slug": "us-east"},
    )
    body = json.loads(route.calls[0].request.content.decode())
    assert result["name"] == "new-db"
    assert body["name"] == "new-db"
    assert body["plan"] == "hobby"
    assert body["cluster_size"] == "PS_10"
    assert body["region"] == {"slug": "us-east"}


@respx.mock
@pytest.mark.asyncio
async def test_create_database_conflict(connector):
    respx.post(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases"
    ).mock(return_value=httpx.Response(409, json={"message": "name taken"}))
    with pytest.raises(PlanetScaleConflictError):
        await connector.create_database(name="dup")


@respx.mock
@pytest.mark.asyncio
async def test_delete_database(connector):
    route = respx.delete(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/old-db"
    ).mock(return_value=httpx.Response(204))
    result = await connector.delete_database(name="old-db")
    assert route.called
    assert result == {}


@pytest.mark.asyncio
async def test_get_database_requires_name(connector):
    with pytest.raises(PlanetScaleError, match="database name is required"):
        await connector.get_database(name="")


# ═══════════════════════════════════════════════════════════════════════════
# Branches
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_branches_uses_defaults(connector):
    payload = {"data": [{"id": "b1", "name": "main", "production": True}]}
    route = respx.get(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/{TEST_DB}/branches"
    ).mock(return_value=httpx.Response(200, json=payload))
    result = await connector.list_branches()
    assert route.called
    assert result["data"][0]["name"] == "main"


@respx.mock
@pytest.mark.asyncio
async def test_get_branch(connector):
    respx.get(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/{TEST_DB}/branches/feature-x"
    ).mock(return_value=httpx.Response(200, json={"id": "b1", "name": "feature-x", "parent_branch": "main"}))
    result = await connector.get_branch(name="feature-x")
    assert result["name"] == "feature-x"
    assert result["parent_branch"] == "main"


@respx.mock
@pytest.mark.asyncio
async def test_create_branch_sends_parent(connector):
    route = respx.post(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/{TEST_DB}/branches"
    ).mock(return_value=httpx.Response(201, json={"id": "b2", "name": "feature-x"}))
    await connector.create_branch(name="feature-x", parent_branch="main")
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"name": "feature-x", "parent_branch": "main"}


@respx.mock
@pytest.mark.asyncio
async def test_delete_branch(connector):
    route = respx.delete(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/{TEST_DB}/branches/temp"
    ).mock(return_value=httpx.Response(204))
    result = await connector.delete_branch(name="temp")
    assert route.called
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Deploy requests
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_deploy_requests_with_state(connector):
    payload = {"data": [{"id": "dr1", "number": 5, "state": "open"}]}
    route = respx.get(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/{TEST_DB}/deploy-requests"
    ).mock(return_value=httpx.Response(200, json=payload))
    result = await connector.list_deploy_requests(state="open")
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("state") == "open"
    assert result["data"][0]["number"] == 5


@respx.mock
@pytest.mark.asyncio
async def test_get_deploy_request(connector):
    respx.get(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/{TEST_DB}/deploy-requests/7"
    ).mock(return_value=httpx.Response(200, json={"id": "dr7", "number": 7, "state": "open"}))
    result = await connector.get_deploy_request(number=7)
    assert result["number"] == 7


@pytest.mark.asyncio
async def test_get_deploy_request_requires_number(connector):
    with pytest.raises(PlanetScaleError, match="deploy-request number is required"):
        await connector.get_deploy_request(number=0)


@respx.mock
@pytest.mark.asyncio
async def test_create_deploy_request(connector):
    route = respx.post(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/{TEST_DB}/deploy-requests"
    ).mock(return_value=httpx.Response(201, json={"id": "dr2", "number": 7, "state": "open"}))
    result = await connector.create_deploy_request(
        branch="feature-x", into_branch="main", notes="schema bump"
    )
    body = json.loads(route.calls[0].request.content.decode())
    assert result["number"] == 7
    assert body["branch"] == "feature-x"
    assert body["into_branch"] == "main"
    assert body["notes"] == "schema bump"


@pytest.mark.asyncio
async def test_create_deploy_request_requires_branch(connector):
    with pytest.raises(PlanetScaleError, match="branch is required"):
        await connector.create_deploy_request(branch="")


# ═══════════════════════════════════════════════════════════════════════════
# Backups + database tokens
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_backups(connector):
    payload = {"data": [{"id": "bk1", "name": "daily-2026-06-20", "state": "ready"}]}
    route = respx.get(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/{TEST_DB}/branches/main/backups"
    ).mock(return_value=httpx.Response(200, json=payload))
    result = await connector.list_backups(branch="main")
    assert route.called
    assert result["data"][0]["state"] == "ready"


@pytest.mark.asyncio
async def test_list_backups_requires_branch(connector):
    with pytest.raises(PlanetScaleError, match="branch is required"):
        await connector.list_backups(branch="")


@respx.mock
@pytest.mark.asyncio
async def test_list_database_tokens(connector):
    payload = {"data": [{"id": "pw1", "name": "app-reader", "role": "reader"}]}
    route = respx.get(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases/{TEST_DB}/branches/main/passwords"
    ).mock(return_value=httpx.Response(200, json=payload))
    result = await connector.list_database_tokens(branch="main")
    assert route.called
    assert result["data"][0]["role"] == "reader"


@pytest.mark.asyncio
async def test_list_database_tokens_requires_branch(connector):
    with pytest.raises(PlanetScaleError, match="branch is required"):
        await connector.list_database_tokens(branch="")


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    payload = {"data": [{"id": "o1", "name": "test-org"}]}
    route = respx.get(f"{PLANETSCALE_BASE}/organizations").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow"}),
            httpx.Response(200, json=payload),
        ]
    )
    result = await connector.list_organizations()
    assert route.call_count == 2
    assert result == payload


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{PLANETSCALE_BASE}/organizations").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    result = await connector.list_organizations()
    assert route.call_count == 2
    assert result == {"data": []}


@respx.mock
@pytest.mark.asyncio
async def test_health_check_persistent_429_is_degraded(connector, no_retry_sleep):
    respx.get(f"{PLANETSCALE_BASE}/organizations").mock(
        return_value=httpx.Response(429, json={"message": "limited"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# 400 / validation surfaces
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_bad_request_400_raises_bad_request_error(connector):
    respx.post(
        f"{PLANETSCALE_BASE}/organizations/{TEST_ORG}/databases"
    ).mock(return_value=httpx.Response(400, json={"message": "invalid plan"}))
    with pytest.raises(PlanetScaleBadRequestError):
        await connector.create_database(name="bad", plan="not-a-plan")


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity + multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert PlanetScaleConnector.CONNECTOR_TYPE == "planetscale"


def test_auth_type_class_attr():
    assert PlanetScaleConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(PlanetScaleConnector, "REQUIRED_CONFIG_KEYS")
    assert "service_token_id" in PlanetScaleConnector.REQUIRED_CONFIG_KEYS
    assert "service_token" in PlanetScaleConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(PlanetScaleConnector, "_STATUS_MAP")
    assert 401 in PlanetScaleConnector._STATUS_MAP
    assert 403 in PlanetScaleConnector._STATUS_MAP
    assert 429 in PlanetScaleConnector._STATUS_MAP


def test_independent_instances_per_tenant():
    c1 = PlanetScaleConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = PlanetScaleConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_connector_id_set(connector):
    assert connector.connector_id == CONNECTOR_ID
    assert connector.tenant_id == TENANT_ID


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — tenant-scoped id contract
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_database_id_is_tenant_scoped():
    from helpers.normalizer import normalize_database

    doc = normalize_database(
        {"id": "db-xyz", "name": "billing", "plan": "scaler", "region": {"slug": "us-east"}, "state": "ready"},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    assert doc.id == f"{TENANT_ID}_db-xyz"
    assert doc.source_id == "db-xyz"
    assert doc.source == "planetscale.databases"
    assert doc.metadata["kind"] == "planetscale.database"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_branch_id_is_tenant_scoped():
    from helpers.normalizer import normalize_branch

    doc = normalize_branch(
        {"id": "b1", "name": "feature-x", "parent_branch": "main", "production": False, "ready": True},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    assert doc.id == f"{TENANT_ID}_b1"
    assert doc.source == "planetscale.branches"
    assert doc.metadata["parent_branch"] == "main"
    assert doc.metadata["ready"] is True
