"""Unit tests for CrispConnector — respx-mocked, zero real I/O."""
import base64
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import CrispConnector
from exceptions import (
    CrispAuthError,
    CrispError,
    CrispNotFoundError,
    CrispRateLimitError,
)

from tests.conftest import (
    CONNECTOR_ID,
    CRISP_BASE,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
    TEST_IDENTIFIER,
    TEST_SESSION_ID,
    TEST_TIER,
    TEST_WEBSITE_ID,
)


def _expected_basic_token() -> str:
    raw = f"{TEST_IDENTIFIER}:{TEST_API_KEY}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _assert_basic_and_tier(request: httpx.Request) -> None:
    """Verify HTTP Basic + X-Crisp-Tier headers on every Crisp call."""
    assert request.headers["Authorization"] == f"Basic {_expected_basic_token()}"
    assert request.headers["X-Crisp-Tier"] == TEST_TIER
    assert request.headers["Content-Type"] == "application/json"


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert CrispConnector.CONNECTOR_TYPE == "crisp"


def test_auth_type_class_attr():
    assert CrispConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(CrispConnector, "REQUIRED_CONFIG_KEYS")
    assert "identifier" in CrispConnector.REQUIRED_CONFIG_KEYS
    assert "api_key" in CrispConnector.REQUIRED_CONFIG_KEYS
    assert "website_id" in CrispConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(CrispConnector, "_STATUS_MAP")
    assert 401 in CrispConnector._STATUS_MAP
    assert 403 in CrispConnector._STATUS_MAP
    assert 429 in CrispConnector._STATUS_MAP


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
async def test_install_missing_identifier():
    c = CrispConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={**TEST_CONFIG, "identifier": ""},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_api_key():
    c = CrispConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={**TEST_CONFIG, "api_key": ""},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_website_id():
    c = CrispConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={**TEST_CONFIG, "website_id": ""},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — synthesised TokenInfo with Basic credential
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_basic_token(connector):
    info = await connector.authorize()
    assert info.token_type == "Basic"
    assert info.access_token == _expected_basic_token()
    assert info.scopes == [TEST_TIER]


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape — Basic + X-Crisp-Tier + Content-Type
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_basic(connector):
    route = respx.get(f"{CRISP_BASE}/user/account").mock(
        return_value=httpx.Response(200, json={"data": {"user_id": "u1"}})
    )
    await connector.get_account_profile()
    assert route.called
    _assert_basic_and_tier(route.calls[0].request)


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    route = respx.get(f"{CRISP_BASE}/user/account").mock(
        return_value=httpx.Response(200, json={"data": {"user_id": "u1"}})
    )
    result = await connector.health_check()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_offline_token_expired(connector):
    respx.get(f"{CRISP_BASE}/user/account").mock(
        return_value=httpx.Response(401, json={"reason": "invalid_credentials"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_invalid_credentials(connector):
    respx.get(f"{CRISP_BASE}/user/account").mock(
        return_value=httpx.Response(403, json={"reason": "forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.DEGRADED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_429_degraded(connector, no_retry_sleep):
    respx.get(f"{CRISP_BASE}/user/account").mock(
        return_value=httpx.Response(429, json={"reason": "rate_limited"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# get_account_profile / list_websites / get_website
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_get_account_profile_success(connector):
    respx.get(f"{CRISP_BASE}/user/account").mock(
        return_value=httpx.Response(
            200, json={"data": {"user_id": "u1", "email": "ops@example.com"}}
        )
    )
    result = await connector.get_account_profile()
    assert result["data"]["email"] == "ops@example.com"


@respx.mock
@pytest.mark.asyncio
async def test_get_account_profile_raises_on_401(connector):
    respx.get(f"{CRISP_BASE}/user/account").mock(
        return_value=httpx.Response(401, json={"reason": "unauthorized"})
    )
    with pytest.raises(CrispAuthError):
        await connector.get_account_profile()


@respx.mock
@pytest.mark.asyncio
async def test_list_websites_success(connector):
    respx.get(f"{CRISP_BASE}/user/websites").mock(
        return_value=httpx.Response(
            200, json={"data": [{"website_id": TEST_WEBSITE_ID, "name": "Acme"}]}
        )
    )
    result = await connector.list_websites()
    assert result["data"][0]["website_id"] == TEST_WEBSITE_ID


@respx.mock
@pytest.mark.asyncio
async def test_get_website_success(connector):
    respx.get(f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}").mock(
        return_value=httpx.Response(200, json={"data": {"name": "Acme"}})
    )
    result = await connector.get_website(TEST_WEBSITE_ID)
    assert result["data"]["name"] == "Acme"


@respx.mock
@pytest.mark.asyncio
async def test_get_website_not_found(connector):
    respx.get(f"{CRISP_BASE}/website/missing").mock(
        return_value=httpx.Response(404, json={"reason": "not_found"})
    )
    with pytest.raises(CrispNotFoundError):
        await connector.get_website("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Conversations
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_conversations_with_pagination_and_search(connector):
    route = respx.get(f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/conversations/2").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"session_id": TEST_SESSION_ID, "state": "pending"}]},
        )
    )
    result = await connector.list_conversations(
        TEST_WEBSITE_ID,
        page=2,
        per_page=25,
        search_query="invoice",
        search_filter_type="text",
    )
    assert route.called
    qs = dict(route.calls.last.request.url.params)
    assert qs["per_page"] == "25"
    assert qs["search_query"] == "invoice"
    assert qs["search_type"] == "text"
    assert result["data"][0]["session_id"] == TEST_SESSION_ID


@respx.mock
@pytest.mark.asyncio
async def test_get_conversation_success(connector):
    respx.get(
        f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/conversation/{TEST_SESSION_ID}"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": {"session_id": TEST_SESSION_ID, "state": "resolved"}}
        )
    )
    result = await connector.get_conversation(TEST_WEBSITE_ID, TEST_SESSION_ID)
    assert result["data"]["session_id"] == TEST_SESSION_ID


@respx.mock
@pytest.mark.asyncio
async def test_send_message_body_renames_from(connector):
    route = respx.post(
        f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/conversation/{TEST_SESSION_ID}/message"
    ).mock(return_value=httpx.Response(200, json={"data": {"fingerprint": 999}}))

    result = await connector.send_message(
        TEST_WEBSITE_ID,
        TEST_SESSION_ID,
        type="text",
        from_="operator",
        origin="chat",
        content="Hello!",
    )

    assert route.called
    payload = json.loads(route.calls.last.request.read())
    # `from_` MUST be renamed to `from` on the wire
    assert payload == {
        "type": "text",
        "from": "operator",
        "origin": "chat",
        "content": "Hello!",
    }
    assert result["data"]["fingerprint"] == 999


# ═══════════════════════════════════════════════════════════════════════════
# People
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_people_paginated(connector):
    route = respx.get(
        f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/people/profiles/3"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": [{"people_id": "p1", "email": "a@b.com"}]}
        )
    )
    result = await connector.list_people(
        TEST_WEBSITE_ID, page=3, per_page=10, search_text="acme"
    )
    assert route.called
    qs = dict(route.calls.last.request.url.params)
    assert qs["per_page"] == "10"
    assert qs["search_text"] == "acme"
    assert result["data"][0]["people_id"] == "p1"


@respx.mock
@pytest.mark.asyncio
async def test_get_person_success(connector):
    respx.get(
        f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/people/profile/p1"
    ).mock(return_value=httpx.Response(200, json={"data": {"people_id": "p1"}}))
    result = await connector.get_person(TEST_WEBSITE_ID, "p1")
    assert result["data"]["people_id"] == "p1"


@respx.mock
@pytest.mark.asyncio
async def test_create_person_with_email_and_segments(connector):
    route = respx.post(f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/people/profile").mock(
        return_value=httpx.Response(200, json={"data": {"people_id": "p-new"}})
    )
    result = await connector.create_person(
        TEST_WEBSITE_ID,
        email="new@example.com",
        person={"nickname": "New"},
        segments=["lead"],
    )
    assert route.called
    body = json.loads(route.calls.last.request.read())
    assert body == {
        "email": "new@example.com",
        "person": {"nickname": "New"},
        "segments": ["lead"],
    }
    assert result["data"]["people_id"] == "p-new"


@respx.mock
@pytest.mark.asyncio
async def test_create_person_omits_none_fields(connector):
    route = respx.post(f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/people/profile").mock(
        return_value=httpx.Response(200, json={"data": {"people_id": "p"}})
    )
    await connector.create_person(TEST_WEBSITE_ID, email="x@y.com")
    body = json.loads(route.calls.last.request.read())
    assert body == {"email": "x@y.com"}
    assert "person" not in body
    assert "segments" not in body


@respx.mock
@pytest.mark.asyncio
async def test_update_person_patch_envelope(connector):
    route = respx.patch(
        f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/people/profile/p1"
    ).mock(return_value=httpx.Response(200, json={"data": {}}))
    await connector.update_person(
        TEST_WEBSITE_ID, "p1", {"nickname": "Updated"}
    )
    assert route.called
    body = json.loads(route.calls.last.request.read())
    assert body == {"person": {"nickname": "Updated"}}


# ═══════════════════════════════════════════════════════════════════════════
# Helpdesk + Campaigns
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_helpdesks_success(connector):
    respx.get(
        f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/helpdesk/locale/en/articles/1"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"article_id": "a1", "title": "Reset password"}]},
        )
    )
    result = await connector.list_helpdesks(TEST_WEBSITE_ID)
    assert result["data"][0]["article_id"] == "a1"


@respx.mock
@pytest.mark.asyncio
async def test_list_campaigns_success(connector):
    respx.get(
        f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/campaigns/list/1"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": [{"campaign_id": "camp1", "name": "Launch"}]}
        )
    )
    result = await connector.list_campaigns(TEST_WEBSITE_ID)
    assert result["data"][0]["campaign_id"] == "camp1"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — the HTTP client must retry and return the eventual payload."""
    route = respx.get(f"{CRISP_BASE}/user/account").mock(
        side_effect=[
            httpx.Response(429, json={"reason": "rate_limited"}),
            httpx.Response(200, json={"data": {"user_id": "u1"}}),
        ]
    )
    result = await connector.get_account_profile()
    assert route.call_count == 2
    assert result["data"]["user_id"] == "u1"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{CRISP_BASE}/user/account").mock(
        side_effect=[
            httpx.Response(500, json={"reason": "boom"}),
            httpx.Response(200, json={"data": {"user_id": "u1"}}),
        ]
    )
    result = await connector.get_account_profile()
    assert route.call_count == 2
    assert result["data"]["user_id"] == "u1"


@respx.mock
@pytest.mark.asyncio
async def test_retry_exhausted_raises_rate_limit(connector, no_retry_sleep):
    respx.get(f"{CRISP_BASE}/user/account").mock(
        return_value=httpx.Response(429, json={"reason": "rate_limited"})
    )
    with pytest.raises(CrispRateLimitError):
        await connector.get_account_profile()


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer / SyncResult
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_normalize_conversation_doc_id_is_tenant_scoped(connector):
    from helpers.normalizer import normalize_conversation

    doc = normalize_conversation(
        {
            "session_id": "sess-1",
            "website_id": TEST_WEBSITE_ID,
            "state": "resolved",
            "meta": {"subject": "Order #42", "email": "a@b.com"},
            "last_message": "Thanks!",
            "created_at": 1717000000000,
        },
        connector.connector_id,
        connector.tenant_id,
    )
    # tenant-scoped id per the plan: f"{tenant_id}_{source_id}"
    assert doc.id == f"{TENANT_ID}_sess-1"
    assert doc.source_id == "sess-1"
    assert doc.title == "Order #42"
    assert doc.author == "a@b.com"
    assert doc.metadata["tenant_id"] == TENANT_ID
    assert doc.metadata["kind"] == "crisp.conversation"


@pytest.mark.asyncio
async def test_normalize_person_handles_envelope(connector):
    from helpers.normalizer import normalize_person

    doc = normalize_person(
        {"data": {"people_id": "p1", "email": "x@y.com", "segments": ["lead"]}},
        connector.connector_id,
        connector.tenant_id,
    )
    assert doc.id == f"{TENANT_ID}_p1"
    assert doc.metadata["segments"] == ["lead"]
    assert doc.metadata["kind"] == "crisp.person"


@pytest.mark.asyncio
async def test_normalize_helpdesk_article(connector):
    from helpers.normalizer import normalize_helpdesk_article

    doc = normalize_helpdesk_article(
        {"article_id": "a1", "title": "How to", "content": "...", "locale": "en"},
        connector.connector_id,
        connector.tenant_id,
    )
    assert doc.id == f"{TENANT_ID}_a1"
    assert doc.metadata["kind"] == "crisp.helpdesk"
    assert doc.metadata["locale"] == "en"


@respx.mock
@pytest.mark.asyncio
async def test_sync_iterates_three_surfaces(connector):
    # Helpdesk: page 1 -> 1 item, page 2 -> empty
    respx.get(
        f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/helpdesk/locale/en/articles/1"
    ).mock(return_value=httpx.Response(200, json={"data": [{"article_id": "a1", "title": "T"}]}))
    respx.get(
        f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/helpdesk/locale/en/articles/2"
    ).mock(return_value=httpx.Response(200, json={"data": []}))
    # Conversations: page 1 -> 1 item (<50 stops loop)
    respx.get(f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/conversations/1").mock(
        return_value=httpx.Response(
            200, json={"data": [{"session_id": "s1", "state": "resolved"}]}
        )
    )
    # People: page 1 -> 1 item (<50 stops loop)
    respx.get(f"{CRISP_BASE}/website/{TEST_WEBSITE_ID}/people/profiles/1").mock(
        return_value=httpx.Response(
            200, json={"data": [{"people_id": "p1", "email": "x@y.com"}]}
        )
    )

    result = await connector.sync()
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_fails_without_website_id():
    c = CrispConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={**TEST_CONFIG, "website_id": ""},
    )
    result = await c.sync()
    assert result.status.value == "failed"
    assert "website_id" in (result.message or "")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    a = CrispConnector(tenant_id="tenant-A", connector_id="c-A", config=dict(TEST_CONFIG))
    b = CrispConnector(tenant_id="tenant-B", connector_id="c-B", config=dict(TEST_CONFIG))
    assert a.tenant_id != b.tenant_id
    assert a.connector_id != b.connector_id


def test_tenant_id_propagated(connector):
    assert connector.tenant_id == TENANT_ID
