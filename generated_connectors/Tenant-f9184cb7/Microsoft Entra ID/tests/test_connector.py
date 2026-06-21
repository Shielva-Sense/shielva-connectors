"""Unit tests for EntraIDConnector — fully respx-mocked, zero real I/O."""
import json
import re
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth
from connector import EntraIDConnector
from exceptions import EntraIDAuthError, EntraIDError, EntraIDNotFound

from tests.conftest import (
    CONNECTOR_ID,
    ENTRA_TENANT_ID,
    GRAPH_BASE,
    TEST_CONFIG,
    TOKEN_RESPONSE,
    TOKEN_URL,
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
async def test_install_missing_tenant_id(connector):
    connector.config["tenant_id"] = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_client_secret(connector):
    connector.config["client_secret"] = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# authenticate() — also verifies install-time auth error surfacing
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_authenticate_posts_client_credentials_body(connector):
    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json=TOKEN_RESPONSE)

    respx.post(TOKEN_URL).mock(side_effect=_capture)

    token = await connector.authenticate()

    assert token.access_token == "fake-access-token"
    assert token.refresh_token is None  # client_credentials has no refresh_token
    # The token URL must be tenant-scoped
    assert ENTRA_TENANT_ID in captured["url"]
    # Body must be form-encoded with the four required client_credentials fields
    form = parse_qs(captured["body"])
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == ["test-client-id"]
    assert form["client_secret"] == ["test-client-secret"]
    assert form["scope"] == ["https://graph.microsoft.com/.default"]


@pytest.mark.asyncio
@respx.mock
async def test_authenticate_auth_error_surfaces(connector):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret provided.",
            },
        )
    )
    with pytest.raises(EntraIDAuthError) as excinfo:
        await connector.authenticate()
    assert "Invalid client secret" in str(excinfo.value) or "invalid_client" in str(
        excinfo.value
    )


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(authed):
    respx.get(f"{GRAPH_BASE}/organization").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "org-1"}]})
    )
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_token_expired_after_refresh_fails(connector):
    # First the token POST succeeds (auto-mint on demand)…
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    # …then /organization rejects the token TWICE (the client refreshes once, then surfaces).
    respx.get(f"{GRAPH_BASE}/organization").mock(
        return_value=httpx.Response(
            401, json={"error": {"code": "InvalidAuthenticationToken", "message": "expired"}}
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


# ═══════════════════════════════════════════════════════════════════════════
# list_users — verifies $filter pass-through
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_users_with_filter(authed):
    route = respx.get(f"{GRAPH_BASE}/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"id": "u1", "userPrincipalName": "alice@example.com"},
                    {"id": "u2", "userPrincipalName": "bob@example.com"},
                ]
            },
        )
    )
    result = await authed.list_users(top=50, filter="startswith(displayName,'A')")
    assert result["value"][0]["id"] == "u1"
    # Verify the request actually included the $filter and $top query params
    sent = route.calls.last.request
    qs = parse_qs(sent.url.query.decode("utf-8"))
    assert qs["$filter"] == ["startswith(displayName,'A')"]
    assert qs["$top"] == ["50"]


# ═══════════════════════════════════════════════════════════════════════════
# get_user
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_user_success(authed):
    respx.get(f"{GRAPH_BASE}/users/alice@example.com").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "u1",
                "userPrincipalName": "alice@example.com",
                "displayName": "Alice",
            },
        )
    )
    result = await authed.get_user("alice@example.com")
    assert result["id"] == "u1"
    assert result["displayName"] == "Alice"


@pytest.mark.asyncio
@respx.mock
async def test_get_user_not_found_raises(authed):
    respx.get(f"{GRAPH_BASE}/users/missing").mock(
        return_value=httpx.Response(
            404,
            json={"error": {"code": "Request_ResourceNotFound", "message": "missing"}},
        )
    )
    with pytest.raises(EntraIDNotFound):
        await authed.get_user("missing")


# ═══════════════════════════════════════════════════════════════════════════
# create_user — verifies passwordProfile body shape
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_create_user_body_shape(authed):
    route = respx.post(f"{GRAPH_BASE}/users").mock(
        return_value=httpx.Response(
            201,
            json={"id": "u9", "userPrincipalName": "newbie@example.com"},
        )
    )
    result = await authed.create_user(
        account_enabled=True,
        display_name="New Bie",
        mail_nickname="newbie",
        password="P@ssw0rd!",
        user_principal_name="newbie@example.com",
    )
    assert result["id"] == "u9"
    sent = json.loads(route.calls.last.request.content)
    assert sent["accountEnabled"] is True
    assert sent["displayName"] == "New Bie"
    assert sent["mailNickname"] == "newbie"
    assert sent["userPrincipalName"] == "newbie@example.com"
    assert sent["passwordProfile"]["password"] == "P@ssw0rd!"
    assert sent["passwordProfile"]["forceChangePasswordNextSignIn"] is True


# ═══════════════════════════════════════════════════════════════════════════
# update_user
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_update_user_patches_fields(authed):
    route = respx.patch(f"{GRAPH_BASE}/users/u1").mock(return_value=httpx.Response(204))
    result = await authed.update_user("u1", {"jobTitle": "VP Eng"})
    assert result == {"id": "u1", "updated": True}
    body = json.loads(route.calls.last.request.content)
    assert body == {"jobTitle": "VP Eng"}


@pytest.mark.asyncio
async def test_update_user_rejects_empty_fields(authed):
    with pytest.raises(ValueError):
        await authed.update_user("u1", {})


# ═══════════════════════════════════════════════════════════════════════════
# Groups
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_groups_success(authed):
    respx.get(f"{GRAPH_BASE}/groups").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "g1", "displayName": "Engineering"}]}
        )
    )
    result = await authed.list_groups(top=25)
    assert result["value"][0]["displayName"] == "Engineering"


@pytest.mark.asyncio
@respx.mock
async def test_create_group_body_shape(authed):
    route = respx.post(f"{GRAPH_BASE}/groups").mock(
        return_value=httpx.Response(201, json={"id": "g9", "displayName": "Ops"})
    )
    result = await authed.create_group(
        display_name="Ops",
        mail_nickname="ops",
        security_enabled=True,
        mail_enabled=False,
        description="Operations team",
    )
    assert result["id"] == "g9"
    sent = json.loads(route.calls.last.request.content)
    assert sent["displayName"] == "Ops"
    assert sent["mailNickname"] == "ops"
    assert sent["securityEnabled"] is True
    assert sent["mailEnabled"] is False
    assert sent["description"] == "Operations team"
    assert sent["groupTypes"] == []


@pytest.mark.asyncio
@respx.mock
async def test_list_group_members(authed):
    respx.get(f"{GRAPH_BASE}/groups/g1/members").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "u1"}, {"id": "u2"}]})
    )
    result = await authed.list_group_members("g1")
    assert len(result["value"]) == 2


@pytest.mark.asyncio
@respx.mock
async def test_add_group_member_ref_body(authed):
    route = respx.post(f"{GRAPH_BASE}/groups/g1/members/$ref").mock(
        return_value=httpx.Response(204)
    )
    result = await authed.add_group_member("g1", "u1")
    assert result == {"group_id": "g1", "user_id": "u1", "added": True}
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "@odata.id": f"{GRAPH_BASE}/directoryObjects/u1"
    }


# ═══════════════════════════════════════════════════════════════════════════
# Applications + audit logs
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_applications(authed):
    respx.get(f"{GRAPH_BASE}/applications").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "a1"}, {"id": "a2"}]})
    )
    result = await authed.list_applications(top=10)
    assert len(result["value"]) == 2


@pytest.mark.asyncio
@respx.mock
async def test_list_audit_logs(authed):
    respx.get(f"{GRAPH_BASE}/auditLogs/directoryAudits").mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": "ev1", "category": "RoleManagement"}]},
        )
    )
    result = await authed.list_audit_logs(top=5)
    assert result["value"][0]["category"] == "RoleManagement"


# ═══════════════════════════════════════════════════════════════════════════
# Retry / refresh behavior
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_succeeds(authed, monkeypatch):
    """First call returns 429 with Retry-After:0, second returns 200."""
    # Speed up sleeps so the test is instant
    import client.http_client as http_mod

    async def _noop(_):
        return None

    monkeypatch.setattr(http_mod.asyncio, "sleep", _noop)

    route = respx.get(f"{GRAPH_BASE}/users").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {"message": "throttled"}}),
            httpx.Response(200, json={"value": [{"id": "u1"}]}),
        ]
    )
    result = await authed.list_users()
    assert result["value"][0]["id"] == "u1"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_refresh_on_401_then_succeeds(authed):
    """A 401 on a Graph call triggers a token re-mint, then the request succeeds."""
    # invalidate the cached token so refresh is observable
    authed.http_client._access_token = "stale-token"

    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "expires_in": 3600,
                "access_token": "refreshed-token",
                "scope": "https://graph.microsoft.com/.default",
            },
        )
    )
    route = respx.get(f"{GRAPH_BASE}/users").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"message": "expired"}}),
            httpx.Response(200, json={"value": [{"id": "u1"}]}),
        ]
    )
    result = await authed.list_users()
    assert result["value"][0]["id"] == "u1"
    assert route.call_count == 2
    assert authed.http_client._access_token == "refreshed-token"


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_attribute():
    assert EntraIDConnector.CONNECTOR_TYPE == "entra_id"


def test_auth_type_attribute():
    assert EntraIDConnector.AUTH_TYPE == "oauth2"


def test_required_config_keys_defined():
    for key in ("tenant_id", "client_id", "client_secret"):
        assert key in EntraIDConnector.REQUIRED_CONFIG_KEYS
