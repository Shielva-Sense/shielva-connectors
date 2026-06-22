"""Unit tests for DiscordConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import DiscordConnector
from exceptions import DiscordAuthError, DiscordNotFound, DiscordRateLimitError

from tests.conftest import (
    CONNECTOR_ID,
    DISCORD_BASE,
    TENANT_ID,
    TEST_BOT_TOKEN,
    TEST_CHANNEL_ID,
    TEST_CONFIG,
    TEST_GUILD_ID,
    TEST_MESSAGE_ID,
    TEST_OAUTH_TOKEN,
    TEST_ROLE_ID,
    TEST_USER_ID,
)


# ═══════════════════════════════════════════════════════════════════════════
# Identity / class constants
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert DiscordConnector.CONNECTOR_TYPE == "discord"


def test_auth_type_class_attr():
    assert DiscordConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(DiscordConnector, "REQUIRED_CONFIG_KEYS")
    assert DiscordConnector.REQUIRED_CONFIG_KEYS == ["bot_token"]


def test_status_map_defined():
    assert 401 in DiscordConnector._STATUS_MAP
    assert 403 in DiscordConnector._STATUS_MAP
    assert 429 in DiscordConnector._STATUS_MAP


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
async def test_install_missing_bot_token(connector):
    connector.config.pop("bot_token", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_oauth_only_also_ok(connector):
    """oauth_token alone (no bot_token) is a valid install."""
    connector.config.pop("bot_token", None)
    connector.config["oauth_token"] = TEST_OAUTH_TOKEN
    # Re-evaluate the install gate
    result = await connector.install()
    assert result.auth_status == AuthStatus.AUTHENTICATED


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — api_key path returns a synthetic TokenInfo
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_bot_token_info(connector):
    token = await connector.authorize()
    assert token.access_token == TEST_BOT_TOKEN
    assert token.token_type == "Bot"


@pytest.mark.asyncio
async def test_authorize_oauth_override(oauth_connector):
    token = await oauth_connector.authorize()
    assert token.access_token == TEST_OAUTH_TOKEN
    assert token.token_type == "Bearer"


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bot vs Bearer) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bot_prefixed(connector):
    """Bot-token connector must send ``Authorization: Bot <token>``."""
    route = respx.get(f"{DISCORD_BASE}/users/@me").mock(
        return_value=httpx.Response(200, json={"id": "1", "username": "bot"})
    )
    await connector.health_check()
    assert route.called
    sent = route.calls[0].request
    assert sent.headers.get("authorization") == f"Bot {TEST_BOT_TOKEN}"


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer_when_oauth_set(oauth_connector):
    """oauth_token override flips header to ``Authorization: Bearer ...``."""
    route = respx.get(f"{DISCORD_BASE}/users/@me").mock(
        return_value=httpx.Response(200, json={"id": "1", "username": "u"})
    )
    await oauth_connector.health_check()
    assert route.called
    sent = route.calls[0].request
    assert sent.headers.get("authorization") == f"Bearer {TEST_OAUTH_TOKEN}"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_discord_auth_error(connector):
    respx.get(f"{DISCORD_BASE}/users/@me").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    with pytest.raises(DiscordAuthError):
        # Call the underlying client to bypass health_check's catch
        await connector.http_client.get_current_user()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{DISCORD_BASE}/users/@me").mock(
        return_value=httpx.Response(200, json={"id": "1", "username": "bot"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error_401(connector):
    respx.get(f"{DISCORD_BASE}/users/@me").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_health_check_forbidden_403(connector):
    respx.get(f"{DISCORD_BASE}/users/@me").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Guilds
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_guilds_success(connector):
    payload = [
        {"id": "g1", "name": "Engineering"},
        {"id": "g2", "name": "Design"},
    ]
    route = respx.get(f"{DISCORD_BASE}/users/@me/guilds").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_guilds(limit=25)
    assert route.called
    assert isinstance(result, list)
    assert result[0]["name"] == "Engineering"
    qs = route.calls[0].request.url.params
    assert qs.get("limit") == "25"


@respx.mock
@pytest.mark.asyncio
async def test_get_guild_success(connector):
    respx.get(f"{DISCORD_BASE}/guilds/{TEST_GUILD_ID}").mock(
        return_value=httpx.Response(200, json={"id": TEST_GUILD_ID, "name": "G"})
    )
    result = await connector.get_guild(TEST_GUILD_ID)
    assert result["id"] == TEST_GUILD_ID


@respx.mock
@pytest.mark.asyncio
async def test_get_guild_not_found(connector):
    respx.get(f"{DISCORD_BASE}/guilds/missing").mock(
        return_value=httpx.Response(404, json={"message": "Unknown Guild"})
    )
    with pytest.raises(DiscordNotFound):
        await connector.get_guild("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Channels
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_channels_success(connector):
    respx.get(f"{DISCORD_BASE}/guilds/{TEST_GUILD_ID}/channels").mock(
        return_value=httpx.Response(
            200, json=[{"id": "c1", "name": "general", "type": 0}],
        )
    )
    result = await connector.list_channels(TEST_GUILD_ID)
    assert isinstance(result, list)
    assert result[0]["name"] == "general"


@respx.mock
@pytest.mark.asyncio
async def test_get_channel_success(connector):
    respx.get(f"{DISCORD_BASE}/channels/{TEST_CHANNEL_ID}").mock(
        return_value=httpx.Response(200, json={"id": TEST_CHANNEL_ID, "name": "x"})
    )
    result = await connector.get_channel(TEST_CHANNEL_ID)
    assert result["id"] == TEST_CHANNEL_ID


# ═══════════════════════════════════════════════════════════════════════════
# Messages
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_send_message_posts_body(connector):
    route = respx.post(
        f"{DISCORD_BASE}/channels/{TEST_CHANNEL_ID}/messages",
    ).mock(
        return_value=httpx.Response(
            200, json={"id": "msg-1", "channel_id": TEST_CHANNEL_ID, "content": "hi"},
        )
    )
    result = await connector.send_message(TEST_CHANNEL_ID, "hi")
    assert route.called
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"content": "hi"}
    assert result["id"] == "msg-1"


@respx.mock
@pytest.mark.asyncio
async def test_send_message_with_embeds(connector):
    route = respx.post(
        f"{DISCORD_BASE}/channels/{TEST_CHANNEL_ID}/messages",
    ).mock(return_value=httpx.Response(200, json={"id": "msg-2"}))
    embeds = [{"title": "Build green"}]
    await connector.send_message(TEST_CHANNEL_ID, "see embed", embeds=embeds)
    body = json.loads(route.calls[0].request.content.decode())
    assert body["embeds"][0]["title"] == "Build green"


@respx.mock
@pytest.mark.asyncio
async def test_get_message_success(connector):
    respx.get(
        f"{DISCORD_BASE}/channels/{TEST_CHANNEL_ID}/messages/{TEST_MESSAGE_ID}",
    ).mock(
        return_value=httpx.Response(
            200, json={"id": TEST_MESSAGE_ID, "content": "hi"},
        )
    )
    result = await connector.get_message(TEST_CHANNEL_ID, TEST_MESSAGE_ID)
    assert result["id"] == TEST_MESSAGE_ID


@respx.mock
@pytest.mark.asyncio
async def test_list_messages_passes_query_params(connector):
    route = respx.get(
        f"{DISCORD_BASE}/channels/{TEST_CHANNEL_ID}/messages",
    ).mock(return_value=httpx.Response(200, json=[{"id": "m1"}]))
    result = await connector.list_messages(
        TEST_CHANNEL_ID, limit=10, before="snowflake-1",
    )
    assert isinstance(result, list)
    qs = route.calls[0].request.url.params
    assert qs.get("limit") == "10"
    assert qs.get("before") == "snowflake-1"


@respx.mock
@pytest.mark.asyncio
async def test_edit_message_patches_content(connector):
    route = respx.patch(
        f"{DISCORD_BASE}/channels/{TEST_CHANNEL_ID}/messages/{TEST_MESSAGE_ID}",
    ).mock(
        return_value=httpx.Response(
            200, json={"id": TEST_MESSAGE_ID, "content": "edited"},
        )
    )
    result = await connector.edit_message(
        TEST_CHANNEL_ID, TEST_MESSAGE_ID, "edited",
    )
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"content": "edited"}
    assert result["content"] == "edited"


@respx.mock
@pytest.mark.asyncio
async def test_delete_message_returns_empty(connector):
    respx.delete(
        f"{DISCORD_BASE}/channels/{TEST_CHANNEL_ID}/messages/{TEST_MESSAGE_ID}",
    ).mock(return_value=httpx.Response(204))
    result = await connector.delete_message(TEST_CHANNEL_ID, TEST_MESSAGE_ID)
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Members + Roles + Users
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_guild_members_success(connector):
    respx.get(
        f"{DISCORD_BASE}/guilds/{TEST_GUILD_ID}/members",
    ).mock(
        return_value=httpx.Response(
            200, json=[{"user": {"id": "u1", "username": "alice"}}],
        )
    )
    result = await connector.list_guild_members(TEST_GUILD_ID, limit=20)
    assert isinstance(result, list)
    assert result[0]["user"]["username"] == "alice"


@respx.mock
@pytest.mark.asyncio
async def test_get_user_success(connector):
    respx.get(f"{DISCORD_BASE}/users/{TEST_USER_ID}").mock(
        return_value=httpx.Response(200, json={"id": TEST_USER_ID, "username": "u"})
    )
    result = await connector.get_user(TEST_USER_ID)
    assert result["id"] == TEST_USER_ID


@respx.mock
@pytest.mark.asyncio
async def test_add_role_put(connector):
    route = respx.put(
        f"{DISCORD_BASE}/guilds/{TEST_GUILD_ID}/members/"
        f"{TEST_USER_ID}/roles/{TEST_ROLE_ID}",
    ).mock(return_value=httpx.Response(204))
    result = await connector.add_role(TEST_GUILD_ID, TEST_USER_ID, TEST_ROLE_ID)
    assert route.called
    assert result == {}


@respx.mock
@pytest.mark.asyncio
async def test_remove_role_delete(connector):
    route = respx.delete(
        f"{DISCORD_BASE}/guilds/{TEST_GUILD_ID}/members/"
        f"{TEST_USER_ID}/roles/{TEST_ROLE_ID}",
    ).mock(return_value=httpx.Response(204))
    result = await connector.remove_role(
        TEST_GUILD_ID, TEST_USER_ID, TEST_ROLE_ID,
    )
    assert route.called
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Webhooks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_create_webhook_success(connector):
    route = respx.post(
        f"{DISCORD_BASE}/channels/{TEST_CHANNEL_ID}/webhooks",
    ).mock(
        return_value=httpx.Response(200, json={"id": "w1", "name": "shielva-hook"})
    )
    result = await connector.create_webhook(TEST_CHANNEL_ID, "shielva-hook")
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"name": "shielva-hook"}
    assert result["id"] == "w1"


@respx.mock
@pytest.mark.asyncio
async def test_execute_webhook_success(connector):
    webhook_id = "wh-1"
    webhook_token = "wh-token"
    route = respx.post(
        f"{DISCORD_BASE}/webhooks/{webhook_id}/{webhook_token}",
    ).mock(return_value=httpx.Response(204))
    result = await connector.execute_webhook(
        webhook_id, webhook_token, "broadcast",
    )
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"content": "broadcast"}
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — Discord-supplied retry_after honoured, then success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once with retry_after, then 200 — connector must retry and return."""
    route = respx.get(f"{DISCORD_BASE}/users/@me/guilds").mock(
        side_effect=[
            httpx.Response(
                429,
                json={"message": "rate limited", "retry_after": 0.01, "global": False},
            ),
            httpx.Response(200, json=[{"id": "after-retry", "name": "G"}]),
        ]
    )
    result = await connector.list_guilds(limit=1)
    assert route.call_count == 2
    assert result[0]["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{DISCORD_BASE}/users/@me/guilds").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json=[]),
        ]
    )
    result = await connector.list_guilds()
    assert route.call_count == 2
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = DiscordConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = DiscordConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer SOC — NormalizedDocument id is f"{tenant_id}_{source_id}"
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_message_id_format():
    from helpers.normalizer import normalize_message

    raw = {
        "id": "src-1",
        "channel_id": "c1",
        "content": "hi",
        "author": {"id": "u1", "username": "alice"},
        "timestamp": "2026-06-21T10:00:00Z",
    }
    doc = normalize_message(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{TENANT_ID}_src-1"
    assert doc.source_id == "src-1"
    assert doc.author == "alice"
    assert doc.metadata["kind"] == "discord.message"


def test_normalize_guild_id_format():
    from helpers.normalizer import normalize_guild

    raw = {"id": "g-1", "name": "My Server", "owner_id": "o-1"}
    doc = normalize_guild(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{TENANT_ID}_g-1"
    assert doc.title == "My Server"
    assert doc.metadata["kind"] == "discord.guild"
