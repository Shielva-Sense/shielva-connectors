"""Unit tests for DropboxConnector — respx-mocked, zero real I/O.

Mirrors the Wix gold-standard suite: instance identity, auth-header shape,
per-endpoint smoke tests, retry policy on 429/5xx, error classification, and
the multi-tenant isolation invariant.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime

import httpx
import pytest
import respx

from shared.base_connector import (
    AuthStatus,
    ConnectorHealth,
    NormalizedDocument,
    SyncStatus,
    TokenInfo,
)

from connector import DropboxConnector
from exceptions import (
    DropboxAuthError,
    DropboxBadRequestError,
    DropboxConflictError,
    DropboxError,
    DropboxNetworkError,
    DropboxNotFoundError,
    DropboxRateLimitError,
    DropboxServerError,
)
from helpers.normalizer import normalize_entry, normalize_file, normalize_folder
from helpers.utils import (
    normalize_dropbox_path,
    parse_dt,
    safe_get,
    utcnow,
    with_retry,
)

from tests.conftest import (
    API_BASE,
    CONNECTOR_ID,
    CONTENT_BASE,
    TENANT_ID,
    TEST_ACCESS_TOKEN,
    TEST_CLIENT_ID,
    TEST_CLIENT_SECRET,
    TEST_CONFIG,
    TEST_REDIRECT_URI,
    TEST_REFRESH_TOKEN,
    TOKEN_URL,
)


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert DropboxConnector.CONNECTOR_TYPE == "dropbox"


def test_auth_type_class_attr():
    assert DropboxConnector.AUTH_TYPE == "oauth2_code"


def test_required_config_keys_defined():
    assert hasattr(DropboxConnector, "REQUIRED_CONFIG_KEYS")
    assert "client_id" in DropboxConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in DropboxConnector.REQUIRED_CONFIG_KEYS


def test_oauth_constants_defined():
    assert DropboxConnector.AUTH_URI.startswith("https://www.dropbox.com")
    assert DropboxConnector.TOKEN_URI.startswith("https://api.dropboxapi.com")
    assert "files.metadata.read" in DropboxConnector.REQUIRED_SCOPES
    assert "account_info.read" in DropboxConnector.REQUIRED_SCOPES


def test_status_map_has_required_codes():
    keys = DropboxConnector._STATUS_MAP.keys()
    assert 401 in keys and 403 in keys and 429 in keys


def test_independent_instances_per_tenant():
    c1 = DropboxConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = DropboxConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    assert c1.http_client is not c2.http_client


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    # Until OAuth completes the auth status is PENDING, not AUTHENTICATED.
    assert result.auth_status == AuthStatus.PENDING
    assert result.connector_id == CONNECTOR_ID
    assert "authorize" in (result.message or "").lower()


@pytest.mark.asyncio
async def test_install_missing_client_id(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE
    assert "client_id" in (result.message or "")


@pytest.mark.asyncio
async def test_install_missing_client_secret(connector):
    connector.config.pop("client_secret", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_secret" in (result.message or "")


@pytest.mark.asyncio
async def test_install_persists_config(connector, mocker):
    spy = mocker.patch.object(DropboxConnector, "save_config", new_callable=mocker.AsyncMock)
    await connector.install()
    spy.assert_awaited_once()
    saved = spy.await_args.args[0]
    assert saved["client_id"] == TEST_CLIENT_ID
    assert saved["client_secret"] == TEST_CLIENT_SECRET


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — OAuth code exchange
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorize_exchanges_code_for_token(connector):
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 14400,
                "token_type": "bearer",
                "scope": "files.metadata.read files.content.read",
                "account_id": "dbid:1234",
            },
        )
    )
    token = await connector.authorize(auth_code="auth-code-123", state="some-state")
    assert route.called
    assert isinstance(token, TokenInfo)
    assert token.access_token == "new-access-token"
    assert token.refresh_token == "new-refresh-token"
    assert "files.metadata.read" in token.scopes
    # Verify form-encoded body has the right grant + redirect_uri
    sent_body = route.calls[0].request.content.decode()
    assert "grant_type=authorization_code" in sent_body
    assert "code=auth-code-123" in sent_body
    assert "redirect_uri=" in sent_body


@pytest.mark.asyncio
async def test_authorize_rejects_empty_code(connector):
    with pytest.raises(DropboxAuthError):
        await connector.authorize(auth_code="", state=None)


@pytest.mark.asyncio
async def test_authorize_requires_redirect_uri(connector):
    connector.config["redirect_uri"] = ""
    connector.redirect_uri = ""
    with pytest.raises(DropboxAuthError):
        await connector.authorize(auth_code="code", state=None)


@respx.mock
@pytest.mark.asyncio
async def test_authorize_propagates_dropbox_error(connector):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(DropboxAuthError):
        await connector.authorize(auth_code="bad", state=None)


@respx.mock
@pytest.mark.asyncio
async def test_on_token_refresh_uses_refresh_token(connector):
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed-access",
                "expires_in": 14400,
                "token_type": "bearer",
            },
        )
    )
    token = await connector.on_token_refresh()
    assert route.called
    assert token is not None
    assert token.access_token == "refreshed-access"
    # Dropbox refresh response often omits a new refresh_token — we should
    # preserve the existing one.
    assert token.refresh_token == TEST_REFRESH_TOKEN
    sent_body = route.calls[0].request.content.decode()
    assert "grant_type=refresh_token" in sent_body


@pytest.mark.asyncio
async def test_on_token_refresh_returns_none_when_no_refresh_token(connector, mocker):
    mocker.patch.object(
        DropboxConnector,
        "get_token",
        new_callable=mocker.AsyncMock,
        return_value=TokenInfo(access_token="x", refresh_token=None),
    )
    result = await connector.on_token_refresh()
    assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.post(f"{API_BASE}/users/get_current_account").mock(
        return_value=httpx.Response(
            200,
            json={
                "account_id": "dbid:abc",
                "email": "user@example.com",
                "name": {"display_name": "Test User"},
            },
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.metadata.get("email") == "user@example.com"
    assert result.metadata.get("display_name") == "Test User"


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.post(f"{API_BASE}/users/get_current_account").mock(
        return_value=httpx.Response(401, json={"error_summary": "expired_access_token/"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_rate_limited(connector, no_retry_sleep):
    respx.post(f"{API_BASE}/users/get_current_account").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "1"},
            json={"error_summary": "too_many_requests/"},
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.CONNECTED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape — Bearer prefix
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_has_bearer_prefix(connector):
    route = respx.post(f"{API_BASE}/users/get_current_account").mock(
        return_value=httpx.Response(200, json={})
    )
    await connector.get_current_account()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization", "")
    assert sent_auth == f"Bearer {TEST_ACCESS_TOKEN}"


@respx.mock
@pytest.mark.asyncio
async def test_empty_body_endpoints_send_null(connector):
    """``/users/get_current_account`` body must be JSON ``null``, not ``{}``."""
    route = respx.post(f"{API_BASE}/users/get_current_account").mock(
        return_value=httpx.Response(200, json={})
    )
    await connector.get_current_account()
    sent_body = route.calls[0].request.content.decode()
    assert sent_body == "null"


# ═══════════════════════════════════════════════════════════════════════════
# Files RPC endpoints
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_folder_success(connector):
    route = respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(
            200,
            json={
                "entries": [
                    {".tag": "file", "id": "id:1", "name": "a.txt", "path_display": "/a.txt"},
                ],
                "cursor": "cur1",
                "has_more": False,
            },
        )
    )
    result = await connector.list_folder(path="/x", recursive=True, limit=50)
    assert route.called
    body = json.loads(route.calls[0].request.content.decode())
    assert body["path"] == "/x"
    assert body["recursive"] is True
    assert body["limit"] == 50
    assert result["entries"][0]["id"] == "id:1"


@respx.mock
@pytest.mark.asyncio
async def test_list_folder_continue_success(connector):
    route = respx.post(f"{API_BASE}/files/list_folder/continue").mock(
        return_value=httpx.Response(
            200,
            json={"entries": [], "cursor": "cur2", "has_more": False},
        )
    )
    result = await connector.list_folder_continue("cur-abc")
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"cursor": "cur-abc"}
    assert result["cursor"] == "cur2"


@respx.mock
@pytest.mark.asyncio
async def test_get_metadata_success(connector):
    route = respx.post(f"{API_BASE}/files/get_metadata").mock(
        return_value=httpx.Response(
            200,
            json={".tag": "file", "id": "id:x", "name": "z.pdf", "path_display": "/z.pdf"},
        )
    )
    result = await connector.get_metadata("/z.pdf")
    body = json.loads(route.calls[0].request.content.decode())
    assert body["path"] == "/z.pdf"
    assert result["name"] == "z.pdf"


@respx.mock
@pytest.mark.asyncio
async def test_get_metadata_not_found_raises(connector):
    """Dropbox returns 409 with a not_found tag for missing paths."""
    respx.post(f"{API_BASE}/files/get_metadata").mock(
        return_value=httpx.Response(
            409,
            json={
                "error_summary": "path/not_found/..",
                "error": {".tag": "path", "path": {".tag": "not_found"}},
            },
        )
    )
    with pytest.raises(DropboxNotFoundError):
        await connector.get_metadata("/missing.txt")


@respx.mock
@pytest.mark.asyncio
async def test_copy_file_success(connector):
    route = respx.post(f"{API_BASE}/files/copy_v2").mock(
        return_value=httpx.Response(200, json={"metadata": {"id": "id:dup"}})
    )
    result = await connector.copy_file("/a", "/b", autorename=True)
    body = json.loads(route.calls[0].request.content.decode())
    assert body["from_path"] == "/a"
    assert body["to_path"] == "/b"
    assert body["autorename"] is True
    assert result["metadata"]["id"] == "id:dup"


@respx.mock
@pytest.mark.asyncio
async def test_move_file_success(connector):
    route = respx.post(f"{API_BASE}/files/move_v2").mock(
        return_value=httpx.Response(200, json={"metadata": {"id": "id:moved"}})
    )
    await connector.move_file("/a", "/b")
    body = json.loads(route.calls[0].request.content.decode())
    assert body["from_path"] == "/a"
    assert body["to_path"] == "/b"


@respx.mock
@pytest.mark.asyncio
async def test_delete_file_success(connector):
    route = respx.post(f"{API_BASE}/files/delete_v2").mock(
        return_value=httpx.Response(200, json={"metadata": {".tag": "file"}})
    )
    await connector.delete_file("/old.txt")
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"path": "/old.txt"}


@respx.mock
@pytest.mark.asyncio
async def test_create_folder_success(connector):
    route = respx.post(f"{API_BASE}/files/create_folder_v2").mock(
        return_value=httpx.Response(200, json={"metadata": {".tag": "folder"}})
    )
    await connector.create_folder("/newfolder", autorename=False)
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"path": "/newfolder", "autorename": False}


@respx.mock
@pytest.mark.asyncio
async def test_search_success(connector):
    route = respx.post(f"{API_BASE}/files/search_v2").mock(
        return_value=httpx.Response(200, json={"matches": [], "has_more": False})
    )
    await connector.search("invoice", max_results=25, path="/inbox")
    body = json.loads(route.calls[0].request.content.decode())
    assert body["query"] == "invoice"
    assert body["options"]["max_results"] == 25
    assert body["options"]["path"] == "/inbox"
    assert body["options"]["file_status"] == "active"


@respx.mock
@pytest.mark.asyncio
async def test_list_revisions_success(connector):
    route = respx.post(f"{API_BASE}/files/list_revisions").mock(
        return_value=httpx.Response(
            200, json={"is_deleted": False, "entries": [{"rev": "r1"}]}
        )
    )
    result = await connector.list_revisions("/doc.txt", limit=5)
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"path": "/doc.txt", "mode": "path", "limit": 5}
    assert result["entries"][0]["rev"] == "r1"


@respx.mock
@pytest.mark.asyncio
async def test_restore_revision_success(connector):
    route = respx.post(f"{API_BASE}/files/restore").mock(
        return_value=httpx.Response(200, json={".tag": "file", "rev": "r1"})
    )
    await connector.restore_revision("/doc.txt", "r1")
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"path": "/doc.txt", "rev": "r1"}


# ═══════════════════════════════════════════════════════════════════════════
# Content endpoints (upload / download)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_download_file_routes_to_content_host(connector):
    """download uses the content host, args go in Dropbox-API-Arg header, body is binary."""
    payload = b"hello dropbox"
    route = respx.post(f"{CONTENT_BASE}/files/download").mock(
        return_value=httpx.Response(
            200,
            headers={
                "Dropbox-API-Result": json.dumps(
                    {"name": "hello.txt", "id": "id:42", "size": len(payload)}
                ),
                "Content-Type": "application/octet-stream",
            },
            content=payload,
        )
    )
    result = await connector.download_file("/hello.txt")
    assert route.called
    sent = route.calls[0].request
    api_arg = json.loads(sent.headers["dropbox-api-arg"])
    assert api_arg == {"path": "/hello.txt"}
    assert result["metadata"]["name"] == "hello.txt"
    assert base64.b64decode(result["content_b64"]) == payload
    assert result["size"] == len(payload)


@respx.mock
@pytest.mark.asyncio
async def test_upload_file_routes_to_content_host(connector):
    payload = b"file body bytes"
    route = respx.post(f"{CONTENT_BASE}/files/upload").mock(
        return_value=httpx.Response(
            200,
            json={"name": "up.txt", "id": "id:up", "size": len(payload)},
        )
    )
    result = await connector.upload_file("/up.txt", payload, mode="overwrite", autorename=False)
    sent = route.calls[0].request
    api_arg = json.loads(sent.headers["dropbox-api-arg"])
    assert api_arg["path"] == "/up.txt"
    assert api_arg["mode"] == "overwrite"
    assert api_arg["autorename"] is False
    assert sent.headers.get("content-type") == "application/octet-stream"
    assert sent.content == payload
    assert result["id"] == "id:up"


# ═══════════════════════════════════════════════════════════════════════════
# Sharing
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_create_shared_link_success(connector):
    route = respx.post(f"{API_BASE}/sharing/create_shared_link_with_settings").mock(
        return_value=httpx.Response(200, json={"url": "https://www.dropbox.com/s/abc"})
    )
    result = await connector.create_shared_link("/share.txt", settings={"requested_visibility": "public"})
    body = json.loads(route.calls[0].request.content.decode())
    assert body["path"] == "/share.txt"
    assert body["settings"]["requested_visibility"] == "public"
    assert "url" in result


@respx.mock
@pytest.mark.asyncio
async def test_list_shared_links_success(connector):
    route = respx.post(f"{API_BASE}/sharing/list_shared_links").mock(
        return_value=httpx.Response(200, json={"links": [{"url": "u1"}], "has_more": False})
    )
    await connector.list_shared_links(path="/x", cursor="c1", direct_only=False)
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"direct_only": False, "path": "/x", "cursor": "c1"}


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_get_current_account_success(connector):
    respx.post(f"{API_BASE}/users/get_current_account").mock(
        return_value=httpx.Response(200, json={"email": "u@example.com"})
    )
    result = await connector.get_current_account()
    assert result["email"] == "u@example.com"


@respx.mock
@pytest.mark.asyncio
async def test_get_account_success(connector):
    route = respx.post(f"{API_BASE}/users/get_account").mock(
        return_value=httpx.Response(200, json={"account_id": "dbid:other"})
    )
    await connector.get_account("dbid:other")
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"account_id": "dbid:other"}


@respx.mock
@pytest.mark.asyncio
async def test_get_space_usage_success(connector):
    respx.post(f"{API_BASE}/users/get_space_usage").mock(
        return_value=httpx.Response(
            200,
            json={"used": 1024, "allocation": {".tag": "individual", "allocated": 2000000000}},
        )
    )
    result = await connector.get_space_usage()
    assert result["used"] == 1024


# ═══════════════════════════════════════════════════════════════════════════
# Error classification — http_client → typed exceptions
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_400_raises_bad_request(connector):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(400, json={"error_summary": "bad input/"})
    )
    with pytest.raises(DropboxBadRequestError):
        await connector.list_folder()


@respx.mock
@pytest.mark.asyncio
async def test_401_raises_auth_error(connector):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(401, json={"error_summary": "expired_access_token/"})
    )
    with pytest.raises(DropboxAuthError):
        await connector.list_folder()


@respx.mock
@pytest.mark.asyncio
async def test_403_raises_auth_error(connector):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(403, json={"error_summary": "missing_scope/"})
    )
    with pytest.raises(DropboxAuthError):
        await connector.list_folder()


@respx.mock
@pytest.mark.asyncio
async def test_404_raises_not_found(connector):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(404, json={"error_summary": "not_found/"})
    )
    with pytest.raises(DropboxNotFoundError):
        await connector.list_folder()


@respx.mock
@pytest.mark.asyncio
async def test_409_conflict_raises_conflict(connector):
    respx.post(f"{API_BASE}/files/move_v2").mock(
        return_value=httpx.Response(
            409,
            json={
                "error_summary": "to/conflict/file",
                "error": {".tag": "to", "to": {".tag": "conflict"}},
            },
        )
    )
    with pytest.raises(DropboxConflictError):
        await connector.move_file("/a", "/b")


@respx.mock
@pytest.mark.asyncio
async def test_500_after_retries_raises_server_error(connector, no_retry_sleep):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(500, json={"error_summary": "boom"})
    )
    with pytest.raises(DropboxServerError):
        await connector.list_folder()


# ═══════════════════════════════════════════════════════════════════════════
# Retry policy — 429 / 5xx / transport recovery
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.post(f"{API_BASE}/files/list_folder").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}, json={"error_summary": "rl"}),
            httpx.Response(200, json={"entries": [], "cursor": "x", "has_more": False}),
        ]
    )
    result = await connector.list_folder()
    assert route.call_count == 2
    assert result["entries"] == []


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.post(f"{API_BASE}/users/get_current_account").mock(
        side_effect=[
            httpx.Response(500, json={"error_summary": "boom"}),
            httpx.Response(200, json={"email": "u@example.com"}),
        ]
    )
    result = await connector.get_current_account()
    assert route.call_count == 2
    assert result["email"] == "u@example.com"


@respx.mock
@pytest.mark.asyncio
async def test_429_rate_limit_exhausts_to_typed_error(connector, no_retry_sleep):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"}, json={"error_summary": "rl"})
    )
    with pytest.raises(DropboxRateLimitError) as exc_info:
        await connector.list_folder()
    assert exc_info.value.retry_after_s >= 0


@respx.mock
@pytest.mark.asyncio
async def test_transport_error_retries_then_raises_network(connector, no_retry_sleep):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(DropboxNetworkError):
        await connector.list_folder()


# ═══════════════════════════════════════════════════════════════════════════
# Sync — end-to-end with mocked Dropbox
# ═══════════════════════════════════════════════════════════════════════════


SAMPLE_FILE_ENTRY = {
    ".tag": "file",
    "name": "report.pdf",
    "path_lower": "/docs/report.pdf",
    "path_display": "/Docs/report.pdf",
    "id": "id:abc123",
    "client_modified": "2024-03-15T10:00:00Z",
    "server_modified": "2024-03-15T11:00:00Z",
    "rev": "0123456789abcdef",
    "size": 204800,
    "is_downloadable": True,
}

SAMPLE_FOLDER_ENTRY = {
    ".tag": "folder",
    "name": "Docs",
    "path_lower": "/docs",
    "path_display": "/Docs",
    "id": "id:folderXYZ",
}


@respx.mock
@pytest.mark.asyncio
async def test_sync_single_page_completed(connector, mocker):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(
            200,
            json={
                "entries": [SAMPLE_FILE_ENTRY, SAMPLE_FOLDER_ENTRY],
                "cursor": "c",
                "has_more": False,
            },
        )
    )
    ingest = mocker.patch.object(
        DropboxConnector, "ingest_document", new_callable=mocker.AsyncMock,
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0
    assert ingest.await_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_sync_pagination(connector, mocker):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(
            200,
            json={"entries": [SAMPLE_FILE_ENTRY], "cursor": "c1", "has_more": True},
        )
    )
    respx.post(f"{API_BASE}/files/list_folder/continue").mock(
        return_value=httpx.Response(
            200,
            json={"entries": [SAMPLE_FOLDER_ENTRY], "cursor": "c2", "has_more": False},
        )
    )
    mocker.patch.object(DropboxConnector, "ingest_document", new_callable=mocker.AsyncMock)
    result = await connector.sync()
    assert result.documents_found == 2
    assert result.status == SyncStatus.COMPLETED


@respx.mock
@pytest.mark.asyncio
async def test_sync_empty_returns_completed(connector):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(
            200, json={"entries": [], "cursor": "", "has_more": False}
        )
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0


@respx.mock
@pytest.mark.asyncio
async def test_sync_fetch_failure_returns_failed(connector, no_retry_sleep):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(401, json={"error_summary": "expired/"})
    )
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert result.documents_synced == 0


@respx.mock
@pytest.mark.asyncio
async def test_sync_partial_when_ingest_fails(connector, mocker):
    respx.post(f"{API_BASE}/files/list_folder").mock(
        return_value=httpx.Response(
            200,
            json={
                "entries": [SAMPLE_FILE_ENTRY, SAMPLE_FOLDER_ENTRY],
                "cursor": "c",
                "has_more": False,
            },
        )
    )

    call_count = {"n": 0}

    async def _ingest(self, doc, *, kb_id="", webhook_url=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("kb down")

    mocker.patch.object(DropboxConnector, "ingest_document", _ingest)
    result = await connector.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1
    assert result.documents_failed == 1


# ═══════════════════════════════════════════════════════════════════════════
# disconnect()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_disconnect_revokes_and_clears(connector, mocker):
    respx.post(f"{API_BASE}/auth/token/revoke").mock(
        return_value=httpx.Response(200, json={})
    )
    clear = mocker.patch.object(DropboxConnector, "clear_token", new_callable=mocker.AsyncMock)
    status = await connector.disconnect()
    assert status.auth_status == AuthStatus.UNAUTHENTICATED
    clear.assert_awaited_once()


@respx.mock
@pytest.mark.asyncio
async def test_disconnect_swallows_auth_error(connector, mocker):
    """Already-invalid token must NOT prevent local cleanup."""
    respx.post(f"{API_BASE}/auth/token/revoke").mock(
        return_value=httpx.Response(401, json={"error_summary": "expired/"})
    )
    clear = mocker.patch.object(DropboxConnector, "clear_token", new_callable=mocker.AsyncMock)
    status = await connector.disconnect()
    assert status.auth_status == AuthStatus.UNAUTHENTICATED
    clear.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Helpers — normalizer + utils
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_file_full():
    doc = normalize_file(SAMPLE_FILE_ENTRY, "conn-1", "tenant-A")
    assert isinstance(doc, NormalizedDocument)
    assert doc.id == "tenant-A_id:abc123"
    assert doc.source_id == "id:abc123"
    assert doc.title == "report.pdf"
    assert "report.pdf" in doc.content
    assert doc.source == "dropbox"
    assert doc.metadata["kind"] == "dropbox.file"
    assert doc.metadata["size"] == 204800
    assert doc.metadata["rev"] == "0123456789abcdef"
    assert doc.source_url.startswith("https://www.dropbox.com/home")


def test_normalize_folder_full():
    doc = normalize_folder(SAMPLE_FOLDER_ENTRY, "conn-1", "tenant-A")
    assert doc.id == "tenant-A_id:folderXYZ"
    assert doc.metadata["kind"] == "dropbox.folder"
    assert doc.content.startswith("Folder:")


def test_normalize_entry_dispatches_by_tag():
    f = normalize_entry(SAMPLE_FILE_ENTRY, "c", "t")
    d = normalize_entry(SAMPLE_FOLDER_ENTRY, "c", "t")
    assert f.metadata["kind"] == "dropbox.file"
    assert d.metadata["kind"] == "dropbox.folder"


def test_normalize_file_minimal_entry():
    minimal = {".tag": "file", "name": "x"}
    doc = normalize_file(minimal, "c", "t")
    # source_id falls back to name when both id and path_lower are absent.
    assert doc.source_id == "x"
    assert doc.title == "x"


def test_normalize_dropbox_path_root_and_relative():
    assert normalize_dropbox_path("") == ""
    assert normalize_dropbox_path("/") == ""
    assert normalize_dropbox_path("foo") == "/foo"
    assert normalize_dropbox_path("/foo") == "/foo"


def test_parse_dt_iso():
    dt = parse_dt("2024-03-15T10:00:00Z")
    assert isinstance(dt, datetime)
    assert dt.year == 2024


def test_parse_dt_invalid_returns_none():
    assert parse_dt("not-a-date") is None
    assert parse_dt(None) is None
    assert parse_dt("") is None


def test_safe_get_walks_nested():
    d = {"a": {"b": {"c": 1}}}
    assert safe_get(d, "a", "b", "c") == 1
    assert safe_get(d, "a", "x", default="fallback") == "fallback"
    assert safe_get(None, "a", default="fb") == "fb"


def test_utcnow_is_timezone_aware():
    now = utcnow()
    assert now.tzinfo is not None


@pytest.mark.asyncio
async def test_with_retry_returns_on_first_success():
    calls = {"n": 0}

    async def ok():
        calls["n"] += 1
        return "value"

    out = await with_retry(ok, max_retries=3, base_delay=0)
    assert out == "value"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_with_retry_eventually_raises():
    async def boom():
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        await with_retry(boom, max_retries=2, base_delay=0)


# ═══════════════════════════════════════════════════════════════════════════
# Exception identity
# ═══════════════════════════════════════════════════════════════════════════


def test_exception_hierarchy_rooted_at_dropbox_error():
    for cls in (
        DropboxAuthError,
        DropboxBadRequestError,
        DropboxConflictError,
        DropboxNotFoundError,
        DropboxRateLimitError,
        DropboxServerError,
        DropboxNetworkError,
    ):
        assert issubclass(cls, DropboxError)


def test_rate_limit_error_carries_retry_after():
    exc = DropboxRateLimitError("rl", retry_after_s=12.0)
    assert exc.retry_after_s == 12.0
    assert exc.status_code == 429


def test_dropbox_error_carries_status_and_body():
    exc = DropboxError("x", status_code=418, response_body={"why": "tea"})
    assert exc.status_code == 418
    assert exc.response_body == {"why": "tea"}
