"""Respx-mocked unit tests for OneLoginConnector — zero real I/O."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from shared.base_connector import AuthStatus, ConnectorHealth

from connector import OneLoginConnector
from exceptions import OneLoginAuthError, OneLoginNotFound, OneLoginNotFoundError
from tests.conftest import (
    API_BASE,
    BASE_URL,
    CONNECTOR_ID,
    SUBDOMAIN,
    TENANT_ID,
    TEST_CLIENT_ID,
    TEST_CLIENT_SECRET,
    TEST_CONFIG,
    TOKEN_URL,
)


def _prime_token(connector: OneLoginConnector, ttl_s: int = 3600) -> None:
    """Pretend the http_client has a fresh access token already."""
    connector.http_client._access_token = "cached-access-token"  # type: ignore[attr-defined]
    connector.http_client._token_expires_at = (  # type: ignore[attr-defined]
        datetime.now(timezone.utc) + timedelta(seconds=ttl_s)
    )


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
async def test_install_missing_subdomain(connector):
    connector.config.pop("subdomain", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE
    assert "subdomain" in result.message


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


# ═══════════════════════════════════════════════════════════════════════════
# authenticate() — token endpoint
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_authenticate_success_uses_basic_auth(connector):
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "acc-tok-1",
                "token_type": "bearer",
                "expires_in": 3600,
                "account_id": 12345,
            },
        )
    )
    token = await connector.authenticate()
    assert token.access_token == "acc-tok-1"
    assert token.token_type == "bearer"
    assert connector.http_client._access_token == "acc-tok-1"
    # The token endpoint MUST receive HTTP Basic auth derived from client_id:client_secret.
    sent = route.calls.last.request
    auth_header = sent.headers.get("authorization", "")
    assert auth_header.lower().startswith("basic ")
    # And the body must be grant_type=client_credentials, x-www-form-urlencoded.
    assert sent.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    )
    assert b"grant_type=client_credentials" in sent.content


@pytest.mark.asyncio
@respx.mock
async def test_authenticate_401_raises(connector):
    """Bad credentials → 401 at /auth/oauth2/v2/token → OneLoginAuthError."""
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    with pytest.raises(OneLoginAuthError):
        await connector.authenticate()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 1}]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_token_expired(connector, no_retry_sleep):
    """A persistent 401 even after token refresh → DEGRADED + TOKEN_EXPIRED."""
    _prime_token(connector)
    respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(401, json={"error": "invalid_token"})
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "x", "token_type": "bearer", "expires_in": 3600}
        )
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_403_invalid_credentials(connector, no_retry_sleep):
    _prime_token(connector)
    respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# list_users() / get_user() / create_user() / delete_user()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_users_with_email_filter(connector):
    _prime_token(connector)
    route = respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": 42, "email": "a@example.com"}]}
        )
    )
    resp = await connector.list_users(limit=10, email="a@example.com")
    assert resp["data"][0]["id"] == 42
    sent = route.calls.last.request
    assert sent.url.params.get("email") == "a@example.com"
    assert sent.url.params.get("limit") == "10"
    # Authorization should be Bearer <token>.
    assert sent.headers.get("authorization") == "Bearer cached-access-token"


@pytest.mark.asyncio
@respx.mock
async def test_get_user_success(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/users/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "email": "u@x.com"})
    )
    user = await connector.get_user(7)
    assert user["id"] == 7


@pytest.mark.asyncio
@respx.mock
async def test_get_user_not_found_raises(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/users/999").mock(
        return_value=httpx.Response(404, json={"message": "User not found"})
    )
    with pytest.raises(OneLoginNotFound):
        await connector.get_user(999)
    # And the back-compat alias resolves to the same class.
    assert OneLoginNotFound is OneLoginNotFoundError


@pytest.mark.asyncio
@respx.mock
async def test_create_user_posts_body(connector):
    _prime_token(connector)
    route = respx.post(f"{API_BASE}/users").mock(
        return_value=httpx.Response(
            201, json={"id": 100, "email": "new@example.com"}
        )
    )
    user = await connector.create_user(
        email="new@example.com",
        firstname="New",
        lastname="User",
        role_ids=[1, 2],
    )
    assert user["id"] == 100
    body = route.calls.last.request.content.decode("utf-8")
    assert "new@example.com" in body
    assert '"role_ids"' in body


@pytest.mark.asyncio
@respx.mock
async def test_update_user_puts_fields(connector):
    _prime_token(connector)
    route = respx.put(f"{API_BASE}/users/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "department": "ops"})
    )
    resp = await connector.update_user(5, {"department": "ops"})
    assert resp["department"] == "ops"
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_delete_user(connector):
    _prime_token(connector)
    respx.delete(f"{API_BASE}/users/5").mock(
        return_value=httpx.Response(204)
    )
    resp = await connector.delete_user(5)
    assert resp == {}


# ═══════════════════════════════════════════════════════════════════════════
# search_users()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_search_users_email_branch(connector):
    _prime_token(connector)
    route = respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 1}]})
    )
    await connector.search_users("ada@example.com")
    sent = route.calls.last.request
    assert sent.url.params.get("email") == "ada@example.com"
    assert "username" not in dict(sent.url.params)


@pytest.mark.asyncio
@respx.mock
async def test_search_users_username_branch(connector):
    _prime_token(connector)
    route = respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 1}]})
    )
    await connector.search_users("ada")
    sent = route.calls.last.request
    assert sent.url.params.get("username") == "ada"
    assert "email" not in dict(sent.url.params)


# ═══════════════════════════════════════════════════════════════════════════
# set_user_state() + ValueError on invalid state
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_set_user_state_active(connector):
    _prime_token(connector)
    respx.put(f"{API_BASE}/users/5/state").mock(
        return_value=httpx.Response(200, json={"id": 5, "state": 1})
    )
    resp = await connector.set_user_state(5, 1)
    assert resp["state"] == 1


@pytest.mark.asyncio
async def test_set_user_state_invalid_value_raises(connector):
    with pytest.raises(ValueError):
        await connector.set_user_state(5, 2)


# ═══════════════════════════════════════════════════════════════════════════
# assign_role_to_user()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_assign_role_to_user(connector):
    _prime_token(connector)
    route = respx.post(f"{API_BASE}/users/5/add_roles").mock(
        return_value=httpx.Response(200, json={"id": 5, "roles": [9, 10]})
    )
    resp = await connector.assign_role_to_user(5, [9, 10])
    assert resp["roles"] == [9, 10]
    body = route.calls.last.request.content.decode("utf-8")
    assert "role_id_array" in body


# ═══════════════════════════════════════════════════════════════════════════
# list_user_apps / list_user_roles / set_user_roles
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_user_apps(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/users/5/apps").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 11}]})
    )
    resp = await connector.list_user_apps(5)
    assert resp["data"][0]["id"] == 11


@pytest.mark.asyncio
@respx.mock
async def test_list_user_roles(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/users/5/roles").mock(
        return_value=httpx.Response(200, json={"data": [22, 33]})
    )
    resp = await connector.list_user_roles(5)
    assert resp["data"] == [22, 33]


@pytest.mark.asyncio
@respx.mock
async def test_set_user_roles_puts_role_id_array(connector):
    _prime_token(connector)
    route = respx.put(f"{API_BASE}/users/5/roles").mock(
        return_value=httpx.Response(200, json={"id": 5})
    )
    await connector.set_user_roles(5, [22, 33])
    body = route.calls.last.request.content.decode("utf-8")
    assert "role_id_array" in body


# ═══════════════════════════════════════════════════════════════════════════
# list_apps / get_app / assign_app_to_user
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_apps(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/apps").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": 1, "name": "Salesforce"}]}
        )
    )
    apps = await connector.list_apps()
    assert apps["data"][0]["name"] == "Salesforce"


@pytest.mark.asyncio
@respx.mock
async def test_get_app(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/apps/12").mock(
        return_value=httpx.Response(200, json={"id": 12, "name": "Slack"})
    )
    resp = await connector.get_app(12)
    assert resp["id"] == 12


@pytest.mark.asyncio
@respx.mock
async def test_assign_app_to_user_posts_app_id(connector):
    _prime_token(connector)
    route = respx.post(f"{API_BASE}/users/5/apps").mock(
        return_value=httpx.Response(200, json={"id": 5})
    )
    await connector.assign_app_to_user(5, 12)
    body = route.calls.last.request.content.decode("utf-8")
    assert '"app_id": 12' in body or '"app_id":12' in body


# ═══════════════════════════════════════════════════════════════════════════
# Roles / Groups / Privileges / Mappings
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_roles(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/roles").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 1}]})
    )
    resp = await connector.list_roles()
    assert resp["data"][0]["id"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_get_role(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/roles/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "name": "Admin"})
    )
    resp = await connector.get_role(7)
    assert resp["id"] == 7


@pytest.mark.asyncio
@respx.mock
async def test_list_groups(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/groups").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 1}]})
    )
    resp = await connector.list_groups()
    assert resp["data"][0]["id"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_list_privileges(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/privileges").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "p1"}]})
    )
    resp = await connector.list_privileges()
    assert resp["data"][0]["id"] == "p1"


@pytest.mark.asyncio
@respx.mock
async def test_list_mappings(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/mappings").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 1}]})
    )
    resp = await connector.list_mappings()
    assert resp["data"][0]["id"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Events
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_events_with_filters(connector):
    _prime_token(connector)
    route = respx.get(f"{API_BASE}/events").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": 1, "event_type_id": 5, "notes": "login"}]}
        )
    )
    resp = await connector.list_events(
        limit=25, since="2026-06-01T00:00:00Z", event_type_id=5
    )
    assert resp["data"][0]["event_type_id"] == 5
    params = route.calls.last.request.url.params
    assert params.get("event_type_id") == "5"
    assert params.get("since") == "2026-06-01T00:00:00Z"


@pytest.mark.asyncio
@respx.mock
async def test_get_event(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/events/123").mock(
        return_value=httpx.Response(200, json={"id": 123, "notes": "event"})
    )
    resp = await connector.get_event(123)
    assert resp["id"] == 123


# ═══════════════════════════════════════════════════════════════════════════
# Retry behavior — 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    _prime_token(connector)
    route = respx.get(f"{API_BASE}/users").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"data": [{"id": 1}]}),
        ]
    )
    resp = await connector.list_users(limit=1)
    assert resp["data"][0]["id"] == 1
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    _prime_token(connector)
    route = respx.get(f"{API_BASE}/users").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    resp = await connector.list_users(limit=1)
    assert resp["data"] == []
    assert route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# 401 — silent token refresh + replay
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_refresh_on_401_retries_request(connector):
    _prime_token(connector, ttl_s=3600)
    user_route = respx.get(f"{API_BASE}/users").mock(
        side_effect=[
            httpx.Response(401, json={"error": "token expired"}),
            httpx.Response(200, json={"data": [{"id": 1, "email": "x@y.com"}]}),
        ]
    )
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-token",
                "token_type": "bearer",
                "expires_in": 3600,
            },
        )
    )
    resp = await connector.list_users(limit=1)
    assert resp["data"][0]["id"] == 1
    assert token_route.call_count == 1, "Connector should re-authenticate exactly once"
    assert user_route.call_count == 2, "Original request should be retried after refresh"
    assert connector.http_client._access_token == "new-token"


# ═══════════════════════════════════════════════════════════════════════════
# Sync — paginated user iteration
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_sync_no_documents(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    result = await connector.sync()
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
@respx.mock
async def test_sync_ingests_users(connector):
    _prime_token(connector)
    respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": 1, "email": "u1@x.com", "firstname": "A", "lastname": "B"},
                    {"id": 2, "email": "u2@x.com", "firstname": "C", "lastname": "D"},
                ]
            },
        )
    )
    result = await connector.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type(connector):
    assert connector.CONNECTOR_TYPE == "onelogin"


def test_auth_type(connector):
    assert connector.AUTH_TYPE == "oauth2_client_credentials"


def test_required_config_keys():
    assert "subdomain" in OneLoginConnector.REQUIRED_CONFIG_KEYS
    assert "client_id" in OneLoginConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in OneLoginConnector.REQUIRED_CONFIG_KEYS


def test_status_map_has_401_403_429():
    assert 401 in OneLoginConnector._STATUS_MAP
    assert 403 in OneLoginConnector._STATUS_MAP
    assert 429 in OneLoginConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_multi_tenant_isolation():
    c1 = OneLoginConnector(tenant_id="tenant-A", connector_id="c1", config=dict(TEST_CONFIG))
    c2 = OneLoginConnector(tenant_id="tenant-B", connector_id="c2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    # NormalizedDocument id contract is tenant-scoped — sanity-check the normalizer.
    from helpers.normalizer import normalize_user

    doc1 = normalize_user({"id": 99}, c1.connector_id, c1.tenant_id)
    doc2 = normalize_user({"id": 99}, c2.connector_id, c2.tenant_id)
    assert doc1.id == "tenant-A_99"
    assert doc2.id == "tenant-B_99"
    assert doc1.id != doc2.id


# ═══════════════════════════════════════════════════════════════════════════
# mock_OneLoginHTTPClient fixture — replace transport entirely
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_http_client_fixture(connector, mock_OneLoginHTTPClient):
    """Verify the mock_OneLoginHTTPClient fixture wires correctly."""
    mock_OneLoginHTTPClient.list_users.return_value = {"data": [{"id": 1}]}
    connector.http_client = mock_OneLoginHTTPClient
    resp = await connector.list_users(limit=5)
    assert resp["data"][0]["id"] == 1
    mock_OneLoginHTTPClient.list_users.assert_awaited_once_with(
        limit=5, after_cursor=None, email=None
    )
