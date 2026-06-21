"""Unit tests for MattermostConnector — fully mocked via respx, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import MattermostConnector
from exceptions import (
    MattermostAuthError,
    MattermostBadRequestError,
    MattermostError,
    MattermostNotFound,
    MattermostRateLimitError,
)
from helpers.normalizer import normalize_channel, normalize_post, normalize_user
from helpers.utils import normalize_server_url

from tests.conftest import (
    ACCESS_TOKEN,
    API_BASE,
    CONNECTOR_ID,
    SAMPLE_CHANNEL,
    SAMPLE_POST,
    SAMPLE_TEAM,
    SAMPLE_USER,
    TENANT_ID,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# Class-level contract
# ═══════════════════════════════════════════════════════════════════════════

def test_class_attributes():
    assert MattermostConnector.CONNECTOR_TYPE == "mattermost"
    assert MattermostConnector.AUTH_TYPE == "api_key"
    assert MattermostConnector.VERSION == "1.0.0"


def test_required_config_keys_defined():
    assert hasattr(MattermostConnector, "REQUIRED_CONFIG_KEYS")
    assert "server_url" in MattermostConnector.REQUIRED_CONFIG_KEYS
    assert "personal_access_token" in MattermostConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(MattermostConnector, "_STATUS_MAP")
    assert 401 in MattermostConnector._STATUS_MAP
    assert 403 in MattermostConnector._STATUS_MAP
    assert 429 in MattermostConnector._STATUS_MAP


def test_independent_instances_per_tenant():
    c1 = MattermostConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = MattermostConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_install_success(connector):
    respx.get(f"{API_BASE}/users/me").mock(return_value=httpx.Response(200, json=SAMPLE_USER))
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID
    assert "alice" in (result.message or "")


@pytest.mark.asyncio
async def test_install_missing_credentials(connector):
    connector.config["personal_access_token"] = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_server_url(connector):
    connector.config["server_url"] = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_error(connector):
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(401, json={"message": "Invalid or missing session token"})
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# health_check() — now probes /system/ping
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{API_BASE}/system/ping").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_token_expired(connector):
    respx.get(f"{API_BASE}/system/ping").mock(
        return_value=httpx.Response(401, json={"message": "expired"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_endpoint_not_found(connector):
    respx.get(f"{API_BASE}/system/ping").mock(
        return_value=httpx.Response(404, json={"message": "no route"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# Bearer header
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_authorization_header_is_bearer(connector):
    route = respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(200, json=SAMPLE_USER)
    )
    await connector.get_current_user()
    assert route.called
    sent_auth = route.calls.last.request.headers.get("authorization")
    assert sent_auth == f"Bearer {ACCESS_TOKEN}"


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_current_user(connector):
    respx.get(f"{API_BASE}/users/me").mock(return_value=httpx.Response(200, json=SAMPLE_USER))
    user = await connector.get_current_user()
    assert user["id"] == SAMPLE_USER["id"]
    assert user["username"] == "alice"


@pytest.mark.asyncio
@respx.mock
async def test_get_me_alias(connector):
    respx.get(f"{API_BASE}/users/me").mock(return_value=httpx.Response(200, json=SAMPLE_USER))
    user = await connector.get_me()
    assert user["id"] == SAMPLE_USER["id"]


@pytest.mark.asyncio
@respx.mock
async def test_list_users(connector):
    route = respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(200, json=[SAMPLE_USER])
    )
    users = await connector.list_users(page=1, per_page=25, in_team_id="t1")
    assert users[0]["id"] == SAMPLE_USER["id"]
    qs = dict(route.calls.last.request.url.params)
    assert qs["page"] == "1"
    assert qs["per_page"] == "25"
    assert qs["in_team"] == "t1"


@pytest.mark.asyncio
@respx.mock
async def test_get_user(connector):
    uid = SAMPLE_USER["id"]
    respx.get(f"{API_BASE}/users/{uid}").mock(return_value=httpx.Response(200, json=SAMPLE_USER))
    u = await connector.get_user(uid)
    assert u["id"] == uid


@pytest.mark.asyncio
@respx.mock
async def test_create_user(connector):
    payload = {"username": "bob", "email": "bob@example.com", "password": "Hunter22!"}
    new_user = {**SAMPLE_USER, "username": "bob", "email": "bob@example.com"}
    route = respx.post(f"{API_BASE}/users").mock(
        return_value=httpx.Response(201, json=new_user)
    )
    out = await connector.create_user(payload)
    assert out["username"] == "bob"
    body = route.calls.last.request.content
    assert b"bob@example.com" in body


@pytest.mark.asyncio
@respx.mock
async def test_search_users(connector):
    route = respx.post(f"{API_BASE}/users/search").mock(
        return_value=httpx.Response(200, json=[SAMPLE_USER])
    )
    out = await connector.search_users(
        team_id=SAMPLE_TEAM["id"],
        term="ali",
        in_channel_id=SAMPLE_CHANNEL["id"],
    )
    assert out[0]["username"] == "alice"
    body = route.calls.last.request.content
    assert b"in_channel_id" in body
    assert b"ali" in body


# ═══════════════════════════════════════════════════════════════════════════
# Teams
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_teams(connector):
    route = respx.get(f"{API_BASE}/teams").mock(
        return_value=httpx.Response(200, json=[SAMPLE_TEAM])
    )
    teams = await connector.list_teams(page=0, per_page=60)
    assert teams[0]["id"] == SAMPLE_TEAM["id"]
    sent_params = dict(route.calls.last.request.url.params)
    assert sent_params["page"] == "0"
    assert sent_params["per_page"] == "60"


@pytest.mark.asyncio
@respx.mock
async def test_get_team(connector):
    tid = SAMPLE_TEAM["id"]
    respx.get(f"{API_BASE}/teams/{tid}").mock(return_value=httpx.Response(200, json=SAMPLE_TEAM))
    team = await connector.get_team(tid)
    assert team["name"] == "engineering"


# ═══════════════════════════════════════════════════════════════════════════
# Channels
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_channels(connector):
    tid = SAMPLE_TEAM["id"]
    respx.get(f"{API_BASE}/teams/{tid}/channels").mock(
        return_value=httpx.Response(200, json=[SAMPLE_CHANNEL])
    )
    channels = await connector.list_channels(team_id=tid)
    assert channels[0]["display_name"] == "General"


@pytest.mark.asyncio
@respx.mock
async def test_get_channel(connector):
    cid = SAMPLE_CHANNEL["id"]
    respx.get(f"{API_BASE}/channels/{cid}").mock(
        return_value=httpx.Response(200, json=SAMPLE_CHANNEL)
    )
    out = await connector.get_channel(cid)
    assert out["id"] == cid


@pytest.mark.asyncio
@respx.mock
async def test_create_channel_open(connector):
    route = respx.post(f"{API_BASE}/channels").mock(
        return_value=httpx.Response(201, json=SAMPLE_CHANNEL)
    )
    out = await connector.create_channel(
        team_id=SAMPLE_TEAM["id"],
        name="general",
        display_name="General",
        type="O",
        purpose="public",
    )
    assert out["id"] == SAMPLE_CHANNEL["id"]
    body = route.calls.last.request.content
    assert b'"type":"O"' in body or b'"type": "O"' in body


@pytest.mark.asyncio
@respx.mock
async def test_create_channel_private(connector):
    priv = {**SAMPLE_CHANNEL, "type": "P", "name": "secret"}
    route = respx.post(f"{API_BASE}/channels").mock(return_value=httpx.Response(201, json=priv))
    out = await connector.create_channel(
        team_id=SAMPLE_TEAM["id"],
        name="secret",
        display_name="Secret",
        type="P",
    )
    assert out["type"] == "P"
    body = route.calls.last.request.content
    assert b'"type":"P"' in body or b'"type": "P"' in body


@pytest.mark.asyncio
async def test_create_channel_rejects_invalid_type(connector):
    with pytest.raises(ValueError):
        await connector.create_channel(
            team_id=SAMPLE_TEAM["id"],
            name="general",
            display_name="General",
            type="X",
        )


@pytest.mark.asyncio
@respx.mock
async def test_delete_channel(connector):
    cid = SAMPLE_CHANNEL["id"]
    respx.delete(f"{API_BASE}/channels/{cid}").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    out = await connector.delete_channel(cid)
    assert out.get("status") == "OK"


@pytest.mark.asyncio
@respx.mock
async def test_add_user_to_channel(connector):
    cid = SAMPLE_CHANNEL["id"]
    route = respx.post(f"{API_BASE}/channels/{cid}/members").mock(
        return_value=httpx.Response(
            201,
            json={"channel_id": cid, "user_id": SAMPLE_USER["id"], "roles": "channel_user"},
        )
    )
    out = await connector.add_user_to_channel(cid, SAMPLE_USER["id"])
    assert out["user_id"] == SAMPLE_USER["id"]
    body = route.calls.last.request.content
    assert SAMPLE_USER["id"].encode() in body


# ═══════════════════════════════════════════════════════════════════════════
# Posts
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_post_message(connector):
    route = respx.post(f"{API_BASE}/posts").mock(
        return_value=httpx.Response(201, json=SAMPLE_POST)
    )
    out = await connector.post_message(
        channel_id=SAMPLE_CHANNEL["id"],
        message="hello world",
    )
    assert out["id"] == SAMPLE_POST["id"]
    body = route.calls.last.request.content
    assert b"hello world" in body


@pytest.mark.asyncio
@respx.mock
async def test_post_message_threaded(connector):
    route = respx.post(f"{API_BASE}/posts").mock(
        return_value=httpx.Response(201, json={**SAMPLE_POST, "root_id": "root123"})
    )
    out = await connector.post_message(
        channel_id=SAMPLE_CHANNEL["id"],
        message="reply",
        root_id="root123",
        file_ids=["f1"],
    )
    assert out["root_id"] == "root123"
    body = route.calls.last.request.content
    assert b"root_id" in body
    assert b"file_ids" in body


@pytest.mark.asyncio
@respx.mock
async def test_get_post(connector):
    pid = SAMPLE_POST["id"]
    respx.get(f"{API_BASE}/posts/{pid}").mock(return_value=httpx.Response(200, json=SAMPLE_POST))
    p = await connector.get_post(pid)
    assert p["message"] == "hello world"


@pytest.mark.asyncio
@respx.mock
async def test_update_post(connector):
    pid = SAMPLE_POST["id"]
    updated = {**SAMPLE_POST, "message": "edited"}
    respx.put(f"{API_BASE}/posts/{pid}").mock(return_value=httpx.Response(200, json=updated))
    p = await connector.update_post(post_id=pid, message="edited")
    assert p["message"] == "edited"


@pytest.mark.asyncio
@respx.mock
async def test_delete_post(connector):
    pid = SAMPLE_POST["id"]
    respx.delete(f"{API_BASE}/posts/{pid}").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    out = await connector.delete_post(pid)
    assert out["status"] == "OK"


@pytest.mark.asyncio
@respx.mock
async def test_get_post_not_found(connector):
    pid = "missing-id"
    respx.get(f"{API_BASE}/posts/{pid}").mock(
        return_value=httpx.Response(404, json={"message": "no such post"})
    )
    with pytest.raises(MattermostNotFound):
        await connector.get_post(pid)


@pytest.mark.asyncio
@respx.mock
async def test_post_message_bad_request_raises(connector):
    respx.post(f"{API_BASE}/posts").mock(
        return_value=httpx.Response(400, json={"message": "invalid channel"})
    )
    with pytest.raises(MattermostBadRequestError):
        await connector.post_message(channel_id="x", message="hi")


@pytest.mark.asyncio
@respx.mock
async def test_list_channel_posts_pagination(connector):
    cid = SAMPLE_CHANNEL["id"]
    route = respx.get(f"{API_BASE}/channels/{cid}/posts").mock(
        return_value=httpx.Response(
            200,
            json={"order": [SAMPLE_POST["id"]], "posts": {SAMPLE_POST["id"]: SAMPLE_POST}},
        )
    )
    out = await connector.list_channel_posts(
        channel_id=cid,
        page=2,
        per_page=30,
        since=1700000000000,
        before="postA",
        after="postB",
    )
    assert "posts" in out
    sent = dict(route.calls.last.request.url.params)
    assert sent["page"] == "2"
    assert sent["per_page"] == "30"
    assert sent["since"] == "1700000000000"
    assert sent["before"] == "postA"
    assert sent["after"] == "postB"


# ═══════════════════════════════════════════════════════════════════════════
# Files
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_upload_file(connector):
    route = respx.post(f"{API_BASE}/files").mock(
        return_value=httpx.Response(
            201,
            json={"file_infos": [{"id": "f-abc", "name": "report.txt"}]},
        )
    )
    out = await connector.upload_file(
        channel_id=SAMPLE_CHANNEL["id"],
        file_bytes=b"hello bytes",
        filename="report.txt",
    )
    assert out["file_infos"][0]["id"] == "f-abc"
    assert route.called
    # multipart sets a multipart/form-data content type with boundary
    sent_ct = route.calls.last.request.headers.get("content-type", "")
    assert sent_ct.startswith("multipart/form-data")


@pytest.mark.asyncio
@respx.mock
async def test_get_file_info(connector):
    respx.get(f"{API_BASE}/files/f-abc/info").mock(
        return_value=httpx.Response(200, json={"id": "f-abc", "name": "report.txt", "size": 11})
    )
    info = await connector.get_file_info("f-abc")
    assert info["size"] == 11


# ═══════════════════════════════════════════════════════════════════════════
# Webhooks (incoming + outgoing)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_create_incoming_webhook(connector):
    route = respx.post(f"{API_BASE}/hooks/incoming").mock(
        return_value=httpx.Response(
            201,
            json={"id": "hookA", "channel_id": SAMPLE_CHANNEL["id"], "display_name": "Alerts"},
        )
    )
    out = await connector.create_incoming_webhook(
        channel_id=SAMPLE_CHANNEL["id"],
        display_name="Alerts",
        description="prod alerts",
    )
    assert out["id"] == "hookA"
    body = route.calls.last.request.content
    assert b"Alerts" in body


@pytest.mark.asyncio
@respx.mock
async def test_list_incoming_webhooks(connector):
    respx.get(f"{API_BASE}/hooks/incoming").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "hookA", "channel_id": SAMPLE_CHANNEL["id"]}],
        )
    )
    out = await connector.list_incoming_webhooks(team_id=SAMPLE_TEAM["id"])
    assert out[0]["id"] == "hookA"


@pytest.mark.asyncio
@respx.mock
async def test_create_outgoing_webhook(connector):
    route = respx.post(f"{API_BASE}/hooks/outgoing").mock(
        return_value=httpx.Response(
            201,
            json={"id": "outA", "team_id": SAMPLE_TEAM["id"]},
        )
    )
    out = await connector.create_outgoing_webhook(
        team_id=SAMPLE_TEAM["id"],
        display_name="LookupBot",
        trigger_words=["!lookup"],
        callback_urls=["https://hooks.example.com/cb"],
    )
    assert out["id"] == "outA"
    body = route.calls.last.request.content
    assert b"!lookup" in body
    assert b"hooks.example.com" in body


@pytest.mark.asyncio
@respx.mock
async def test_list_outgoing_webhooks(connector):
    respx.get(f"{API_BASE}/hooks/outgoing").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "outA", "team_id": SAMPLE_TEAM["id"]}],
        )
    )
    out = await connector.list_outgoing_webhooks(team_id=SAMPLE_TEAM["id"])
    assert out[0]["id"] == "outA"


# ═══════════════════════════════════════════════════════════════════════════
# Bots + commands
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_bots(connector):
    respx.get(f"{API_BASE}/bots").mock(
        return_value=httpx.Response(200, json=[{"user_id": "botA", "display_name": "Bot A"}])
    )
    out = await connector.list_bots(page=0, per_page=60)
    assert out[0]["user_id"] == "botA"


@pytest.mark.asyncio
@respx.mock
async def test_list_team_commands(connector):
    respx.get(f"{API_BASE}/commands").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "cmdA", "trigger": "lookup", "team_id": SAMPLE_TEAM["id"]}],
        )
    )
    out = await connector.list_team_commands(team_id=SAMPLE_TEAM["id"], custom_only=True)
    assert out[0]["trigger"] == "lookup"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 500
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success(connector):
    responses = [
        httpx.Response(429, json={"message": "slow down"}, headers={"Retry-After": "0"}),
        httpx.Response(200, json=SAMPLE_USER),
    ]
    route = respx.get(f"{API_BASE}/users/me").mock(side_effect=responses)
    user = await connector.get_current_user()
    assert user["id"] == SAMPLE_USER["id"]
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_429_exhausts_retries(connector):
    connector.http_client._max_retries = 1  # speed up
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(429, json={"message": "limit"})
    )
    with pytest.raises(MattermostRateLimitError):
        await connector.get_current_user()


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{API_BASE}/users/me").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json=SAMPLE_USER),
        ]
    )
    user = await connector.get_current_user()
    assert user["id"] == SAMPLE_USER["id"]
    assert route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# sync() — enumerates teams + channels + recent posts
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_sync_enumerates_channels_and_posts(connector):
    respx.get(f"{API_BASE}/teams").mock(return_value=httpx.Response(200, json=[SAMPLE_TEAM]))
    respx.get(f"{API_BASE}/teams/{SAMPLE_TEAM['id']}/channels").mock(
        return_value=httpx.Response(200, json=[SAMPLE_CHANNEL])
    )
    respx.get(f"{API_BASE}/channels/{SAMPLE_CHANNEL['id']}/posts").mock(
        return_value=httpx.Response(
            200,
            json={"order": [SAMPLE_POST["id"]], "posts": {SAMPLE_POST["id"]: SAMPLE_POST}},
        )
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    # 1 channel + 1 post normalized & ingested
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
@respx.mock
async def test_sync_failure_returns_failed_status(connector):
    respx.get(f"{API_BASE}/teams").mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )
    connector.http_client._max_retries = 0
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# authorize() shim
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_authorize_no_op_probe(connector):
    respx.get(f"{API_BASE}/users/me").mock(return_value=httpx.Response(200, json=SAMPLE_USER))
    token = await connector.authorize()
    assert token.access_token == ACCESS_TOKEN
    assert token.token_type == "Bearer"
    assert token.metadata.get("user_id") == SAMPLE_USER["id"]


@pytest.mark.asyncio
async def test_authorize_without_token_raises(connector):
    connector.personal_access_token = ""
    with pytest.raises(MattermostAuthError):
        await connector.authorize()


# ═══════════════════════════════════════════════════════════════════════════
# normalize_server_url helper
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_server_url_strips_api_suffix():
    assert normalize_server_url("https://mm.acme.com/api/v4") == "https://mm.acme.com"


def test_normalize_server_url_strips_trailing_slash():
    assert normalize_server_url("https://mm.acme.com/") == "https://mm.acme.com"


def test_normalize_server_url_adds_scheme():
    assert normalize_server_url("mm.acme.com") == "https://mm.acme.com"


def test_normalize_server_url_empty_returns_empty():
    assert normalize_server_url("") == ""


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — id format and content_type
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_post_id_is_tenant_scoped():
    doc = normalize_post(SAMPLE_POST, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{TENANT_ID}_{SAMPLE_POST['id']}"
    assert doc.source_id == SAMPLE_POST["id"]
    assert doc.content == "hello world"
    assert doc.content_type == "text/markdown"
    assert doc.metadata["kind"] == "mattermost.post"


def test_normalize_channel_uses_display_name_as_title():
    doc = normalize_channel(SAMPLE_CHANNEL, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{TENANT_ID}_{SAMPLE_CHANNEL['id']}"
    assert doc.title == "General"
    assert "Public chatter" in doc.content
    assert doc.metadata["kind"] == "mattermost.channel"


def test_normalize_user_id_is_tenant_scoped():
    doc = normalize_user(SAMPLE_USER, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{TENANT_ID}_{SAMPLE_USER['id']}"
    assert doc.title == "alice"
    assert doc.author == "alice@example.com"
    assert doc.metadata["kind"] == "mattermost.user"


# ═══════════════════════════════════════════════════════════════════════════
# Server URL flexibility
# ═══════════════════════════════════════════════════════════════════════════

def test_init_accepts_url_with_api_suffix():
    c = MattermostConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "server_url": "https://mm.example.com/api/v4",
            "personal_access_token": ACCESS_TOKEN,
        },
    )
    assert c.server_url == "https://mm.example.com"
    assert c.http_client._base_url == f"{API_BASE}"
