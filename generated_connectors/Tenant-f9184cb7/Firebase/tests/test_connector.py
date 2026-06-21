"""Unit tests for FirebaseConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import FirebaseConnector
from exceptions import (
    FirebaseAuthError,
    FirebaseError,
    FirebaseNotFoundError,
)

from tests.conftest import (
    ACCOUNTS_CREATE_URL,
    CLIENT_EMAIL,
    CONNECTOR_ID,
    DATABASE_URL,
    FCM_URL,
    FIRESTORE_BASE,
    IDENTITY_BASE,
    OAUTH_URL,
    PROJECT_ID,
    STORAGE_BASE,
    STORAGE_BUCKET,
    TENANT_ID,
    TEST_CONFIG,
    TEST_SERVICE_ACCOUNT,
    TOKEN_RESPONSE,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_install_success_mints_initial_token(connector):
    """install() must mint a single OAuth2 access token and return HEALTHY."""
    route = respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    status = await connector.install()
    assert route.called
    assert status.health == ConnectorHealth.HEALTHY
    assert status.auth_status == AuthStatus.AUTHENTICATED
    assert status.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_service_account():
    cfg = dict(TEST_CONFIG)
    cfg.pop("service_account_json", None)
    c = FirebaseConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg
    )
    status = await c.install()
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert status.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_invalid_service_account_json():
    cfg = dict(TEST_CONFIG)
    cfg["service_account_json"] = "{not-valid-json"
    c = FirebaseConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg
    )
    status = await c.install()
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "invalid" in (status.message or "").lower() or "json" in (status.message or "").lower()


@pytest.mark.asyncio
async def test_install_service_account_missing_keys():
    cfg = dict(TEST_CONFIG)
    cfg["service_account_json"] = {"client_email": "x@y", "project_id": "p"}
    c = FirebaseConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg
    )
    status = await c.install()
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS


@respx.mock
@pytest.mark.asyncio
async def test_install_token_exchange_unauthorized(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            401, json={"error": "invalid_grant", "error_description": "bad key"}
        )
    )
    status = await connector.install()
    assert status.health == ConnectorHealth.OFFLINE
    assert status.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape + JWT-bearer wire format
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_token_exchange_posts_jwt_bearer_assertion(connector):
    """OAuth2 token call must use grant_type=jwt-bearer with an RS256 assertion."""
    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json=TOKEN_RESPONSE)

    respx.post(OAUTH_URL).mock(side_effect=_capture)
    await connector.http_client.get_access_token()

    body = captured["body"]
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in body
    assert "assertion=" in body


@respx.mock
@pytest.mark.asyncio
async def test_firestore_call_sends_bearer_token(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    route = respx.get(f"{FIRESTORE_BASE}/widgets/abc").mock(
        return_value=httpx.Response(200, json={"name": "x"})
    )
    await connector.get_document("widgets", "abc")
    assert route.called
    assert (
        route.calls[0].request.headers.get("authorization")
        == f"Bearer {TOKEN_RESPONSE['access_token']}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    respx.get(f"{FIRESTORE_BASE}/__shielva_health__").mock(
        return_value=httpx.Response(200, json={"documents": []})
    )
    status = await connector.health_check()
    assert status.health == ConnectorHealth.HEALTHY
    assert status.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_404_on_sentinel_still_healthy(connector):
    """A 404 on the sentinel collection is fine — only auth/network degrade."""
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    respx.get(f"{FIRESTORE_BASE}/__shielva_health__").mock(
        return_value=httpx.Response(404, json={"error": {"message": "missing"}})
    )
    status = await connector.health_check()
    assert status.health == ConnectorHealth.HEALTHY


@respx.mock
@pytest.mark.asyncio
async def test_health_check_token_unauthorized(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            401, json={"error": {"message": "invalid grant"}}
        )
    )
    status = await connector.health_check()
    assert status.auth_status == AuthStatus.TOKEN_EXPIRED
    assert status.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Token cache
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_token_cached_across_calls(connector):
    """Two sequential Firestore calls hit oauth2 ONCE — the token is cached."""
    token_route = respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    respx.get(f"{FIRESTORE_BASE}/widgets/a").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": f"projects/{PROJECT_ID}/databases/(default)/documents/widgets/a"
            },
        )
    )
    respx.get(f"{FIRESTORE_BASE}/widgets/b").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": f"projects/{PROJECT_ID}/databases/(default)/documents/widgets/b"
            },
        )
    )
    await connector.get_document("widgets", "a")
    await connector.get_document("widgets", "b")
    assert token_route.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════
# Firestore document CRUD
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_documents_uses_page_params(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    route = respx.get(f"{FIRESTORE_BASE}/widgets").mock(
        return_value=httpx.Response(
            200, json={"documents": [], "nextPageToken": "next-1"}
        )
    )
    result = await connector.list_documents("widgets", page_size=5, page_token="cur")
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("pageSize") == "5"
    assert qs.get("pageToken") == "cur"
    assert result["nextPageToken"] == "next-1"


@respx.mock
@pytest.mark.asyncio
async def test_get_document(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    expected = {
        "name": f"projects/{PROJECT_ID}/databases/(default)/documents/widgets/abc",
        "fields": {"name": {"stringValue": "Widget A"}},
    }
    respx.get(f"{FIRESTORE_BASE}/widgets/abc").mock(
        return_value=httpx.Response(200, json=expected)
    )
    result = await connector.get_document("widgets", "abc")
    assert result["name"].endswith("/widgets/abc")
    assert result["fields"]["name"]["stringValue"] == "Widget A"


@respx.mock
@pytest.mark.asyncio
async def test_create_document_encodes_firestore_values(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )

    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "name": f"projects/{PROJECT_ID}/databases/(default)/documents/widgets/new"
            },
        )

    respx.post(f"{FIRESTORE_BASE}/widgets").mock(side_effect=_capture)
    await connector.create_document(
        "widgets",
        {"name": "W", "count": 5, "active": True, "ratio": 1.5, "tags": ["a", "b"]},
        document_id="new",
    )

    assert captured["params"]["documentId"] == "new"
    fields = captured["body"]["fields"]
    assert fields["name"] == {"stringValue": "W"}
    assert fields["count"] == {"integerValue": "5"}
    assert fields["active"] == {"booleanValue": True}
    assert fields["ratio"] == {"doubleValue": 1.5}
    assert fields["tags"] == {
        "arrayValue": {
            "values": [{"stringValue": "a"}, {"stringValue": "b"}]
        }
    }


@respx.mock
@pytest.mark.asyncio
async def test_update_document_patches_fields(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )

    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "name": f"projects/{PROJECT_ID}/databases/(default)/documents/widgets/abc"
            },
        )

    respx.patch(f"{FIRESTORE_BASE}/widgets/abc").mock(side_effect=_capture)
    await connector.update_document("widgets", "abc", {"name": "W", "count": 5})

    fields = captured["body"]["fields"]
    assert fields["name"] == {"stringValue": "W"}
    assert fields["count"] == {"integerValue": "5"}


@respx.mock
@pytest.mark.asyncio
async def test_delete_document(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    route = respx.delete(f"{FIRESTORE_BASE}/widgets/abc").mock(
        return_value=httpx.Response(200, json={})
    )
    result = await connector.delete_document("widgets", "abc")
    assert route.called
    assert result == {}


@respx.mock
@pytest.mark.asyncio
async def test_get_document_not_found_raises(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    respx.get(f"{FIRESTORE_BASE}/widgets/missing").mock(
        return_value=httpx.Response(404, json={"error": {"message": "not found"}})
    )
    with pytest.raises(FirebaseNotFoundError):
        await connector.get_document("widgets", "missing")


# ═══════════════════════════════════════════════════════════════════════════
# Identity Toolkit users
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_users(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )

    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200, json={"users": [{"localId": "u1"}], "nextPageToken": "tok-2"}
        )

    respx.post(f"{IDENTITY_BASE}/accounts:batchGet").mock(side_effect=_capture)
    result = await connector.list_users(page_size=500, next_page_token="tok-1")
    assert captured["body"]["maxResults"] == 500
    assert captured["body"]["nextPageToken"] == "tok-1"
    assert result["users"][0]["localId"] == "u1"
    assert result["nextPageToken"] == "tok-2"


@respx.mock
@pytest.mark.asyncio
async def test_get_user_lookup(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )

    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"users": [{"localId": "u-1", "email": "a@b.com"}]},
        )

    respx.post(f"{IDENTITY_BASE}/accounts:lookup").mock(side_effect=_capture)
    result = await connector.get_user("u-1")
    assert captured["body"] == {"localId": ["u-1"]}
    assert result["users"][0]["email"] == "a@b.com"


@respx.mock
@pytest.mark.asyncio
async def test_create_user_serializes_camel_case_alias(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )

    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200, json={"localId": "u-new", "email": "new@example.com"}
        )

    respx.post(ACCOUNTS_CREATE_URL).mock(side_effect=_capture)
    result = await connector.create_user(
        email="new@example.com",
        password="Sup3r!",
        display_name="New User",
        phone_number="+15551234567",
    )
    body = captured["body"]
    assert body["email"] == "new@example.com"
    assert body["password"] == "Sup3r!"
    assert body["displayName"] == "New User"  # camelCase via pydantic alias
    assert body["phoneNumber"] == "+15551234567"
    assert result["localId"] == "u-new"


@respx.mock
@pytest.mark.asyncio
async def test_update_user_partial(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )

    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"localId": "u-1"})

    respx.post(f"{IDENTITY_BASE}/accounts:update").mock(side_effect=_capture)
    await connector.update_user("u-1", display_name="Updated", disabled=True)
    body = captured["body"]
    assert body["localId"] == "u-1"
    assert body["displayName"] == "Updated"
    assert body["disabled"] is True
    # None fields excluded by exclude_none=True
    assert "email" not in body
    assert "password" not in body


@respx.mock
@pytest.mark.asyncio
async def test_delete_user(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )

    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"kind": "deleted"})

    respx.post(f"{IDENTITY_BASE}/accounts:delete").mock(side_effect=_capture)
    await connector.delete_user("u-1")
    assert captured["body"] == {"localId": "u-1"}


# ═══════════════════════════════════════════════════════════════════════════
# FCM
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_send_fcm_notification_token(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )

    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200, json={"name": f"projects/{PROJECT_ID}/messages/0:42"}
        )

    respx.post(FCM_URL).mock(side_effect=_capture)
    result = await connector.send_fcm_notification(
        token="device-token-xyz",
        notification={"title": "Hi", "body": "Hello"},
        data={"k": "v"},
    )
    msg = captured["body"]["message"]
    assert msg["token"] == "device-token-xyz"
    assert msg["notification"] == {"title": "Hi", "body": "Hello"}
    assert msg["data"] == {"k": "v"}
    assert result["name"].endswith("messages/0:42")


@respx.mock
@pytest.mark.asyncio
async def test_send_fcm_notification_topic(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )

    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200, json={"name": f"projects/{PROJECT_ID}/messages/0:99"}
        )

    respx.post(FCM_URL).mock(side_effect=_capture)
    await connector.send_fcm_notification(
        topic="news", notification={"title": "Headline"}
    )
    msg = captured["body"]["message"]
    assert msg["topic"] == "news"
    assert "token" not in msg


@pytest.mark.asyncio
async def test_send_fcm_notification_requires_target(connector):
    with pytest.raises(ValueError):
        await connector.send_fcm_notification()


@pytest.mark.asyncio
async def test_send_fcm_notification_rejects_both_token_and_topic(connector):
    with pytest.raises(ValueError):
        await connector.send_fcm_notification(token="t", topic="x")


# ═══════════════════════════════════════════════════════════════════════════
# RTDB
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_realtime_db(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    respx.get(f"{DATABASE_URL}/users/u1.json").mock(
        return_value=httpx.Response(200, json={"name": "Alice"})
    )
    result = await connector.get_realtime_db("users/u1")
    assert result == {"name": "Alice"}


@respx.mock
@pytest.mark.asyncio
async def test_set_realtime_db(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )

    captured = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        captured["method"] = request.method
        return httpx.Response(200, json={"name": "Bob"})

    respx.put(f"{DATABASE_URL}/users/u2.json").mock(side_effect=_capture)
    result = await connector.set_realtime_db("users/u2", {"name": "Bob"})
    assert captured["method"] == "PUT"
    assert captured["body"] == {"name": "Bob"}
    assert result == {"name": "Bob"}


@pytest.mark.asyncio
async def test_rtdb_requires_database_url():
    cfg = dict(TEST_CONFIG)
    cfg.pop("database_url", None)
    c = FirebaseConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg
    )
    with pytest.raises(FirebaseError):
        await c.get_realtime_db("any/path")


# ═══════════════════════════════════════════════════════════════════════════
# Cloud Storage
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_storage_objects(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    route = respx.get(STORAGE_BASE).mock(
        return_value=httpx.Response(
            200, json={"items": [{"name": "a.txt"}], "nextPageToken": "p2"}
        )
    )
    result = await connector.list_storage_objects(prefix="logs/", page_size=20)
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("maxResults") == "20"
    assert qs.get("prefix") == "logs/"
    assert result["items"][0]["name"] == "a.txt"


@respx.mock
@pytest.mark.asyncio
async def test_upload_storage_object(connector):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    route = respx.post(STORAGE_BASE).mock(
        return_value=httpx.Response(
            200,
            json={"name": "report.txt", "bucket": STORAGE_BUCKET, "size": "12"},
        )
    )
    result = await connector.upload_storage_object(
        "report.txt", b"hello-bytes", content_type="text/plain"
    )
    assert route.called
    sent = route.calls[0].request
    assert sent.url.params.get("name") == "report.txt"
    assert sent.url.params.get("uploadType") == "media"
    assert sent.content == b"hello-bytes"
    assert sent.headers.get("content-type") == "text/plain"
    assert result["name"] == "report.txt"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 + 5xx
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    route = respx.get(f"{FIRESTORE_BASE}/widgets/abc").mock(
        side_effect=[
            httpx.Response(429, json={"error": {"message": "rate limited"}}),
            httpx.Response(200, json={"name": "after-retry"}),
        ]
    )
    result = await connector.get_document("widgets", "abc")
    assert route.call_count == 2
    assert result["name"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    route = respx.get(f"{FIRESTORE_BASE}/widgets/abc").mock(
        side_effect=[
            httpx.Response(500, json={"error": {"message": "boom"}}),
            httpx.Response(200, json={"name": "ok"}),
        ]
    )
    result = await connector.get_document("widgets", "abc")
    assert route.call_count == 2
    assert result["name"] == "ok"


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_no_collections_completes_empty(connector):
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@respx.mock
@pytest.mark.asyncio
async def test_sync_streams_firestore_collection(connector):
    """When sync_collections is set, sync pages list_documents + ingests each row."""
    connector.config["sync_collections"] = ["widgets"]
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    respx.get(f"{FIRESTORE_BASE}/widgets").mock(
        return_value=httpx.Response(
            200,
            json={
                "documents": [
                    {
                        "name": f"projects/{PROJECT_ID}/databases/(default)/documents/widgets/w1",
                        "fields": {"name": {"stringValue": "Widget 1"}},
                    },
                    {
                        "name": f"projects/{PROJECT_ID}/databases/(default)/documents/widgets/w2",
                        "fields": {"name": {"stringValue": "Widget 2"}},
                    },
                ]
            },
        )
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert connector.ingest_document.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type():
    assert FirebaseConnector.CONNECTOR_TYPE == "firebase"


def test_auth_type():
    assert FirebaseConnector.AUTH_TYPE == "service_account"


def test_required_config_keys():
    assert FirebaseConnector.REQUIRED_CONFIG_KEYS == ["service_account_json"]


def test_status_map_defined():
    assert 401 in FirebaseConnector._STATUS_MAP
    assert 403 in FirebaseConnector._STATUS_MAP
    assert 429 in FirebaseConnector._STATUS_MAP


def test_project_id_derived_from_service_account(connector):
    assert connector.project_id == PROJECT_ID
    assert connector.client_email == CLIENT_EMAIL


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    a = FirebaseConnector(
        tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    b = FirebaseConnector(
        tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert a.tenant_id != b.tenant_id
    assert a.connector_id != b.connector_id
    assert a.http_client is not b.http_client
