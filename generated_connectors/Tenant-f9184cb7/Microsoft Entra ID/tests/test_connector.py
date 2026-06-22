"""Unit tests for EntraIdConnector — fully respx-mocked, zero real I/O."""
import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import EntraIdConnector
from exceptions import EntraIdAuthError, EntraIdError, EntraIdNotFound

from tests.conftest import (
    AZURE_TENANT_ID,
    CONNECTOR_ID,
    GRAPH_BASE,
    TEST_CLIENT_ID,
    TEST_CLIENT_SECRET,
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
async def test_install_missing_azure_tenant_id(connector):
    connector.config["azure_tenant_id"] = ""
    connector.config.pop("tenant_id", None)
    connector.azure_tenant_id = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_client_secret(connector):
    connector.config["client_secret"] = ""
    connector.client_secret = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_client_id(connector):
    connector.config["client_id"] = ""
    connector.client_id = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — client-credentials grant
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_authorize_posts_client_credentials_body(connector):
    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json=TOKEN_RESPONSE)

    respx.post(TOKEN_URL).mock(side_effect=_capture)

    token = await connector.authorize()

    assert token.access_token == "fake-access-token"
    assert token.refresh_token is None  # client_credentials has no refresh_token
    # Token URL must be tenant-scoped
    assert AZURE_TENANT_ID in captured["url"]
    # Body must be form-encoded with the four required client_credentials fields
    form = parse_qs(captured["body"])
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == [TEST_CLIENT_ID]
    assert form["client_secret"] == [TEST_CLIENT_SECRET]
    assert form["scope"] == ["https://graph.microsoft.com/.default"]


@pytest.mark.asyncio
@respx.mock
async def test_authorize_auth_error_surfaces(connector):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret provided.",
            },
        )
    )
    with pytest.raises(EntraIdAuthError) as excinfo:
        await connector.authorize()
    assert (
        "Invalid client secret" in str(excinfo.value)
        or "invalid_client" in str(excinfo.value)
    )


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(authed):
    respx.get(f"{GRAPH_BASE}/users").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "u1"}]})
    )
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_token_expired(connector):
    # The token POST succeeds (auto-mint on demand).
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    # …then /users rejects twice (client refreshes once, then surfaces).
    respx.get(f"{GRAPH_BASE}/users").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"code": "InvalidAuthenticationToken", "message": "expired"}},
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


# ═══════════════════════════════════════════════════════════════════════════
# list_users — verifies $filter + $top pass-through
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
    sent = route.calls.last.request
    qs = parse_qs(sent.url.query.decode("utf-8"))
    assert qs["$filter"] == ["startswith(displayName,'A')"]
    assert qs["$top"] == ["50"]


@pytest.mark.asyncio
@respx.mock
async def test_list_users_with_select(authed):
    route = respx.get(f"{GRAPH_BASE}/users").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    await authed.list_users(top=10, select=["id", "displayName", "mail"])
    sent = route.calls.last.request
    qs = parse_qs(sent.url.query.decode("utf-8"))
    assert qs["$select"] == ["id,displayName,mail"]


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
    with pytest.raises(EntraIdNotFound):
        await authed.get_user("missing")


# ═══════════════════════════════════════════════════════════════════════════
# create_user / update_user / delete_user
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


@pytest.mark.asyncio
@respx.mock
async def test_update_user_patches_fields(authed):
    route = respx.patch(f"{GRAPH_BASE}/users/u1").mock(
        return_value=httpx.Response(204)
    )
    result = await authed.update_user("u1", {"jobTitle": "VP Eng"})
    assert result == {"id": "u1", "updated": True}
    body = json.loads(route.calls.last.request.content)
    assert body == {"jobTitle": "VP Eng"}


@pytest.mark.asyncio
async def test_update_user_rejects_empty_fields(authed):
    with pytest.raises(ValueError):
        await authed.update_user("u1", {})


@pytest.mark.asyncio
@respx.mock
async def test_delete_user_success(authed):
    respx.delete(f"{GRAPH_BASE}/users/u1").mock(return_value=httpx.Response(204))
    result = await authed.delete_user("u1")
    assert result == {"id": "u1", "deleted": True}


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
async def test_get_group_success(authed):
    respx.get(f"{GRAPH_BASE}/groups/g1").mock(
        return_value=httpx.Response(200, json={"id": "g1", "displayName": "Eng"})
    )
    result = await authed.get_group("g1")
    assert result["id"] == "g1"


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
    assert body == {"@odata.id": f"{GRAPH_BASE}/directoryObjects/u1"}


@pytest.mark.asyncio
@respx.mock
async def test_remove_group_member(authed):
    respx.delete(f"{GRAPH_BASE}/groups/g1/members/u1/$ref").mock(
        return_value=httpx.Response(204)
    )
    result = await authed.remove_group_member("g1", "u1")
    assert result == {"group_id": "g1", "user_id": "u1", "removed": True}


# ═══════════════════════════════════════════════════════════════════════════
# Applications + service principals + roles
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
async def test_list_service_principals(authed):
    respx.get(f"{GRAPH_BASE}/servicePrincipals").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "sp1"}]})
    )
    result = await authed.list_service_principals(top=10)
    assert result["value"][0]["id"] == "sp1"


@pytest.mark.asyncio
@respx.mock
async def test_list_directory_roles(authed):
    respx.get(f"{GRAPH_BASE}/directoryRoles").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "r1", "displayName": "Global Reader"}]}
        )
    )
    result = await authed.list_directory_roles()
    assert result["value"][0]["displayName"] == "Global Reader"


@pytest.mark.asyncio
@respx.mock
async def test_list_role_assignments(authed):
    respx.get(f"{GRAPH_BASE}/roleManagement/directory/roleAssignments").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "ra1"}]})
    )
    result = await authed.list_role_assignments(top=20)
    assert result["value"][0]["id"] == "ra1"


# ═══════════════════════════════════════════════════════════════════════════
# Audit + sign-in logs
# ═══════════════════════════════════════════════════════════════════════════

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


@pytest.mark.asyncio
@respx.mock
async def test_list_signin_logs(authed):
    respx.get(f"{GRAPH_BASE}/auditLogs/signIns").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "s1", "appDisplayName": "Outlook"}]}
        )
    )
    result = await authed.list_signin_logs(top=5)
    assert result["value"][0]["appDisplayName"] == "Outlook"


# ═══════════════════════════════════════════════════════════════════════════
# Devices, CA, domains
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_devices(authed):
    respx.get(f"{GRAPH_BASE}/devices").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "d1"}]})
    )
    result = await authed.list_devices(top=10)
    assert result["value"][0]["id"] == "d1"


@pytest.mark.asyncio
@respx.mock
async def test_list_conditional_access_policies(authed):
    respx.get(f"{GRAPH_BASE}/identity/conditionalAccess/policies").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "ca1", "displayName": "Block legacy auth"}]}
        )
    )
    result = await authed.list_conditional_access_policies()
    assert result["value"][0]["displayName"] == "Block legacy auth"


@pytest.mark.asyncio
@respx.mock
async def test_list_domains(authed):
    respx.get(f"{GRAPH_BASE}/domains").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "contoso.com", "isVerified": True}]}
        )
    )
    result = await authed.list_domains()
    assert result["value"][0]["id"] == "contoso.com"


# ═══════════════════════════════════════════════════════════════════════════
# Retry / refresh behavior
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_succeeds(authed, no_retry_sleep):
    """First call returns 429 with Retry-After:0, second returns 200."""
    route = respx.get(f"{GRAPH_BASE}/users").mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"error": {"message": "throttled"}},
            ),
            httpx.Response(200, json={"value": [{"id": "u1"}]}),
        ]
    )
    result = await authed.list_users()
    assert result["value"][0]["id"] == "u1"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500_then_succeeds(authed, no_retry_sleep):
    route = respx.get(f"{GRAPH_BASE}/users").mock(
        side_effect=[
            httpx.Response(500, json={"error": {"message": "boom"}}),
            httpx.Response(200, json={"value": []}),
        ]
    )
    result = await authed.list_users()
    assert route.call_count == 2
    assert result == {"value": []}


@pytest.mark.asyncio
@respx.mock
async def test_refresh_on_401_then_succeeds(authed):
    """A 401 on a Graph call triggers a token re-mint, then the request succeeds."""
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
    assert EntraIdConnector.CONNECTOR_TYPE == "entra_id"


def test_auth_type_attribute():
    assert EntraIdConnector.AUTH_TYPE == "oauth2_client_credentials"


def test_required_config_keys_defined():
    for key in ("azure_tenant_id", "client_id", "client_secret"):
        assert key in EntraIdConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert 401 in EntraIdConnector._STATUS_MAP
    assert 403 in EntraIdConnector._STATUS_MAP
    assert 429 in EntraIdConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = EntraIdConnector(
        tenant_id="shielva-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = EntraIdConnector(
        tenant_id="shielva-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    # Both share the same Azure tenant config (this is correct — Shielva
    # multi-tenant identity is separate from the Entra tenant the app
    # registration lives in).
    assert c1.azure_tenant_id == c2.azure_tenant_id


def test_normalized_document_id_is_shielva_tenant_scoped(authed):
    """NormalizedDocument.id must be `f'{shielva_tenant_id}_{source_id}'` — not
    azure_tenant_id."""
    from helpers.normalizer import normalize_user

    raw = {
        "id": "graph-user-uuid-123",
        "userPrincipalName": "alice@contoso.com",
        "displayName": "Alice",
    }
    doc = normalize_user(raw, authed.connector_id, authed.tenant_id)
    assert doc.id == f"{authed.tenant_id}_graph-user-uuid-123"
    # The azure tenant id MUST NOT leak into the doc id
    assert AZURE_TENANT_ID not in doc.id
