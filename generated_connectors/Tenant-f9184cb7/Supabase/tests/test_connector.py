"""Unit tests for SupabaseConnector — respx-mocked, zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import SupabaseConnector
from exceptions import (
    SupabaseAuthError,
    SupabaseBadRequestError,
    SupabaseConflictError,
    SupabaseError,
    SupabaseNotFound,
    SupabaseRateLimitError,
)
from helpers.normalizer import (
    normalize_row,
    normalize_storage_object,
    normalize_user,
)
from helpers.utils import (
    _format_filter_value,
    build_filter_params,
    build_postgrest_params,
)

from tests.conftest import (
    CONNECTOR_ID,
    PROJECT_REF,
    PROJECT_URL,
    SERVICE_ROLE_KEY,
    TENANT_ID,
    TEST_CONFIG,
)

BASE_URL = PROJECT_URL


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
async def test_install_missing_project_url():
    cfg = dict(TEST_CONFIG)
    cfg.pop("project_url")
    conn = SupabaseConnector(tenant_id="t", connector_id="c", config=cfg)
    result = await conn.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_service_role_key():
    cfg = dict(TEST_CONFIG)
    cfg.pop("service_role_key")
    conn = SupabaseConnector(tenant_id="t", connector_id="c", config=cfg)
    result = await conn.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_accepts_legacy_project_ref():
    """Old configs passed ``project_ref`` — connector must build the URL itself."""
    cfg = {
        "project_ref": PROJECT_REF,
        "service_role_key": SERVICE_ROLE_KEY,
    }
    conn = SupabaseConnector(tenant_id="t", connector_id="c", config=cfg)
    assert conn.project_url == PROJECT_URL
    result = await conn.install()
    assert result.health == ConnectorHealth.HEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Auth headers — service_role key must land in BOTH apikey + Authorization
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_auth_headers_on_select(connector):
    route = respx.get(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(200, json=[{"id": 1}])
    )
    await connector.list_rows("posts")
    request = route.calls[0].request
    assert request.headers["apikey"] == SERVICE_ROLE_KEY
    assert request.headers["authorization"] == f"Bearer {SERVICE_ROLE_KEY}"
    assert request.headers["accept-profile"] == "public"


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{BASE_URL}/auth/v1/settings").mock(
        return_value=httpx.Response(200, json={"external": {"email": True}})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_invalid(connector):
    respx.get(f"{BASE_URL}/auth/v1/settings").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.DEGRADED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_forbidden(connector):
    respx.get(f"{BASE_URL}/auth/v1/settings").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# PostgREST: list_rows / select with filter / order / limit / offset
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_rows_filter_order_limit(connector):
    route = respx.get(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "title": "Hi"}])
    )
    rows = await connector.list_rows(
        "posts",
        columns="id,title",
        filter={"published": True, "id": [1, 2, 3], "rating": {"gt": 4}},
        order="created_at.desc",
        limit=10,
        offset=5,
    )
    assert rows == [{"id": 1, "title": "Hi"}]
    qs = dict(route.calls[0].request.url.params.multi_items())
    assert qs["select"] == "id,title"
    assert qs["published"] == "eq.true"
    assert qs["id"] == "in.(1,2,3)"
    assert qs["rating"] == "gt.4"
    assert qs["order"] == "created_at.desc"
    assert qs["limit"] == "10"
    assert qs["offset"] == "5"


@respx.mock
@pytest.mark.asyncio
async def test_select_alias_works(connector):
    respx.get(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(200, json=[{"id": 1}])
    )
    rows = await connector.select("posts", filter={"id": 1})
    assert rows == [{"id": 1}]


@respx.mock
@pytest.mark.asyncio
async def test_get_row_returns_first(connector):
    respx.get(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(200, json=[{"id": 42, "title": "X"}])
    )
    row = await connector.get_row("posts", 42)
    assert row == {"id": 42, "title": "X"}


@respx.mock
@pytest.mark.asyncio
async def test_get_row_none_when_empty(connector):
    respx.get(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(200, json=[])
    )
    row = await connector.get_row("posts", 999)
    assert row is None


# ═══════════════════════════════════════════════════════════════════════════
# insert / update / delete / upsert
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_insert_rows_sends_prefer_representation(connector):
    payload = [{"id": 1, "title": "A"}]
    route = respx.post(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(201, json=payload)
    )
    rows = await connector.insert_row("posts", [{"title": "A"}])
    assert rows == payload
    request = route.calls[0].request
    assert "return=representation" in request.headers["prefer"]
    assert _json.loads(request.content) == [{"title": "A"}]


@respx.mock
@pytest.mark.asyncio
async def test_insert_alias_works(connector):
    respx.post(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(201, json=[{"id": 1}])
    )
    rows = await connector.insert("posts", [{"title": "A"}])
    assert rows == [{"id": 1}]


@respx.mock
@pytest.mark.asyncio
async def test_update_rows_filter_translates_to_query(connector):
    payload = [{"id": 1, "title": "Renamed"}]
    route = respx.patch(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(200, json=payload)
    )
    rows = await connector.update_row(
        "posts", filter={"id": 1}, fields={"title": "Renamed"},
    )
    assert rows == payload
    qs = dict(route.calls[0].request.url.params.multi_items())
    assert qs["id"] == "eq.1"


@respx.mock
@pytest.mark.asyncio
async def test_delete_rows_filter_translates(connector):
    route = respx.delete(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(200, json=[{"id": 1}])
    )
    rows = await connector.delete_row("posts", filter={"id": 1})
    assert rows == [{"id": 1}]
    qs = dict(route.calls[0].request.url.params.multi_items())
    assert qs["id"] == "eq.1"


@respx.mock
@pytest.mark.asyncio
async def test_upsert_with_on_conflict(connector):
    route = respx.post(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "title": "Upserted"}])
    )
    rows = await connector.upsert(
        "posts", [{"id": 1, "title": "Upserted"}], on_conflict="id",
    )
    assert rows == [{"id": 1, "title": "Upserted"}]
    qs = dict(route.calls[0].request.url.params.multi_items())
    assert qs["on_conflict"] == "id"
    assert "resolution=merge-duplicates" in route.calls[0].request.headers["prefer"]


@respx.mock
@pytest.mark.asyncio
async def test_rpc_calls_function(connector):
    route = respx.post(f"{BASE_URL}/rest/v1/rpc/my_fn").mock(
        return_value=httpx.Response(200, json={"answer": 42})
    )
    result = await connector.rpc("my_fn", params={"x": 1})
    assert result == {"answer": 42}
    assert _json.loads(route.calls[0].request.content) == {"x": 1}


# ═══════════════════════════════════════════════════════════════════════════
# Auth Admin
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_users_paginated(connector):
    route = respx.get(f"{BASE_URL}/auth/v1/admin/users").mock(
        return_value=httpx.Response(200, json={"users": [{"id": "u1"}]})
    )
    result = await connector.list_users(page=2, per_page=25)
    assert result == {"users": [{"id": "u1"}]}
    qs = dict(route.calls[0].request.url.params.multi_items())
    assert qs["page"] == "2"
    assert qs["per_page"] == "25"


@respx.mock
@pytest.mark.asyncio
async def test_get_user(connector):
    respx.get(f"{BASE_URL}/auth/v1/admin/users/u1").mock(
        return_value=httpx.Response(200, json={"id": "u1", "email": "x@y.com"})
    )
    user = await connector.get_user("u1")
    assert user["email"] == "x@y.com"


@respx.mock
@pytest.mark.asyncio
async def test_create_user_full_payload(connector):
    route = respx.post(f"{BASE_URL}/auth/v1/admin/users").mock(
        return_value=httpx.Response(200, json={"id": "u1", "email": "x@y.com"})
    )
    result = await connector.create_user(
        email="x@y.com",
        password="pw",
        user_metadata={"role": "admin"},
        email_confirm=True,
    )
    assert result["id"] == "u1"
    body = _json.loads(route.calls[0].request.content)
    assert body["email"] == "x@y.com"
    assert body["password"] == "pw"
    assert body["user_metadata"] == {"role": "admin"}
    assert body["email_confirm"] is True


@respx.mock
@pytest.mark.asyncio
async def test_update_user(connector):
    route = respx.put(f"{BASE_URL}/auth/v1/admin/users/u1").mock(
        return_value=httpx.Response(200, json={"id": "u1", "email": "new@y.com"})
    )
    result = await connector.update_user("u1", {"email": "new@y.com"})
    assert result["email"] == "new@y.com"
    body = _json.loads(route.calls[0].request.content)
    assert body == {"email": "new@y.com"}


@respx.mock
@pytest.mark.asyncio
async def test_delete_user_not_found(connector):
    respx.delete(f"{BASE_URL}/auth/v1/admin/users/missing").mock(
        return_value=httpx.Response(404, json={"message": "no such user"})
    )
    with pytest.raises(SupabaseNotFound):
        await connector.delete_user("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Storage
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_buckets(connector):
    respx.get(f"{BASE_URL}/storage/v1/bucket").mock(
        return_value=httpx.Response(200, json=[{"id": "avatars", "name": "avatars"}])
    )
    buckets = await connector.list_buckets()
    assert buckets[0]["name"] == "avatars"


@respx.mock
@pytest.mark.asyncio
async def test_list_objects(connector):
    route = respx.post(f"{BASE_URL}/storage/v1/object/list/avatars").mock(
        return_value=httpx.Response(200, json=[{"name": "me.png"}])
    )
    objs = await connector.list_objects("avatars", prefix="me/", limit=20)
    assert objs[0]["name"] == "me.png"
    body = _json.loads(route.calls[0].request.content)
    assert body["prefix"] == "me/"
    assert body["limit"] == 20


@respx.mock
@pytest.mark.asyncio
async def test_upload_object(connector):
    route = respx.post(f"{BASE_URL}/storage/v1/object/avatars/me.png").mock(
        return_value=httpx.Response(200, json={"Key": "avatars/me.png"})
    )
    result = await connector.upload_object(
        bucket="avatars",
        path="me.png",
        content=b"\x89PNG...",
        content_type="image/png",
        upsert=True,
        cache_control="max-age=3600",
    )
    assert result["Key"] == "avatars/me.png"
    req = route.calls[0].request
    assert req.headers["content-type"] == "image/png"
    assert req.headers["x-upsert"] == "true"
    assert req.headers["cache-control"] == "max-age=3600"


@respx.mock
@pytest.mark.asyncio
async def test_download_object_returns_bytes(connector):
    respx.get(f"{BASE_URL}/storage/v1/object/avatars/me.png").mock(
        return_value=httpx.Response(200, content=b"\x89PNGdata")
    )
    body = await connector.download_object("avatars", "me.png")
    assert body == b"\x89PNGdata"


@respx.mock
@pytest.mark.asyncio
async def test_delete_object(connector):
    respx.delete(f"{BASE_URL}/storage/v1/object/avatars/me.png").mock(
        return_value=httpx.Response(200, json={"message": "Deleted"})
    )
    result = await connector.delete_object("avatars", "me.png")
    assert result["message"] == "Deleted"


@respx.mock
@pytest.mark.asyncio
async def test_create_signed_url(connector):
    route = respx.post(f"{BASE_URL}/storage/v1/object/sign/avatars/me.png").mock(
        return_value=httpx.Response(
            200, json={"signedURL": "/storage/v1/object/sign/avatars/me.png?token=abc"}
        )
    )
    result = await connector.create_signed_url("avatars", "me.png", expires_in=7200)
    assert "signedURL" in result
    body = _json.loads(route.calls[0].request.content)
    assert body["expiresIn"] == 7200


# ═══════════════════════════════════════════════════════════════════════════
# Edge Functions
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_invoke_edge_function(connector):
    route = respx.post(f"{BASE_URL}/functions/v1/hello").mock(
        return_value=httpx.Response(200, json={"greeting": "hi"})
    )
    result = await connector.invoke_function("hello", {"name": "Ada"})
    assert result == {"greeting": "hi"}
    body = _json.loads(route.calls[0].request.content)
    assert body == {"name": "Ada"}


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 + 5xx
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/rest/v1/posts").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow down"}),
            httpx.Response(200, json=[{"id": "after-retry"}]),
        ]
    )
    rows = await connector.list_rows("posts")
    assert route.call_count == 2
    assert rows[0]["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/rest/v1/posts").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json=[]),
        ]
    )
    rows = await connector.list_rows("posts")
    assert route.call_count == 2
    assert rows == []


# ═══════════════════════════════════════════════════════════════════════════
# Error-status mapping
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_400_raises_bad_request(connector):
    respx.post(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(400, json={"message": "malformed"})
    )
    with pytest.raises(SupabaseBadRequestError):
        await connector.insert_row("posts", [{"bad": "row"}])


@respx.mock
@pytest.mark.asyncio
async def test_409_raises_conflict(connector):
    respx.post(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(409, json={"message": "duplicate key"})
    )
    with pytest.raises(SupabaseConflictError):
        await connector.insert_row("posts", [{"id": 1}])


@respx.mock
@pytest.mark.asyncio
async def test_401_raises_auth_error(connector):
    respx.get(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    with pytest.raises(SupabaseAuthError):
        await connector.list_rows("posts")


# ═══════════════════════════════════════════════════════════════════════════
# Helper unit tests
# ═══════════════════════════════════════════════════════════════════════════

def test_format_filter_value_scalar():
    assert _format_filter_value(5) == "eq.5"
    assert _format_filter_value("x") == "eq.x"


def test_format_filter_value_bool_lowercase():
    # PostgREST expects lowercase true/false
    assert _format_filter_value(True) == "eq.true"
    assert _format_filter_value(False) == "eq.false"


def test_format_filter_value_list_in():
    assert _format_filter_value([1, 2, 3]) == "in.(1,2,3)"


def test_format_filter_value_dict_operator():
    assert _format_filter_value({"gt": 4}) == "gt.4"
    assert _format_filter_value({"like": "%foo%"}) == "like.%foo%"


def test_format_filter_value_in_dict():
    assert _format_filter_value({"in": [1, 2, 3]}) == "in.(1,2,3)"


def test_build_postgrest_params_full():
    params = build_postgrest_params(
        columns="id,name",
        filter={"status": "active", "id": [1, 2]},
        order="created_at.desc",
        limit=10,
        offset=20,
    )
    assert params["select"] == "id,name"
    assert params["status"] == "eq.active"
    assert params["id"] == "in.(1,2)"
    assert params["order"] == "created_at.desc"
    assert params["limit"] == "10"
    assert params["offset"] == "20"


def test_build_filter_params_only_filter():
    params = build_filter_params({"id": 5, "name": "ada"})
    assert params == {"id": "eq.5", "name": "eq.ada"}


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_row_with_id_uses_table_id():
    doc = normalize_row(
        "posts",
        {"id": 42, "title": "Hi", "created_at": "2026-01-01T00:00:00Z"},
        connector_id="c-1",
        tenant_id="t-A",
    )
    assert doc.id == "t-A_posts:42"
    assert doc.source_id == "posts:42"
    assert doc.title == "Hi"
    assert doc.tenant_id == "t-A"
    assert doc.metadata["table"] == "posts"


def test_normalize_row_without_id_falls_back_to_hash():
    doc = normalize_row(
        "events",
        {"name": "click", "user": "u1"},
        connector_id="c-1",
        tenant_id="t-B",
    )
    assert doc.source_id.startswith("events:")
    # SHA-256 truncated to 16 chars
    assert len(doc.source_id.split(":")[1]) == 16


def test_normalize_user_uses_email_as_title():
    doc = normalize_user(
        {"id": "u-1", "email": "ada@example.com",
         "user_metadata": {"role": "admin"}},
        connector_id="c-1",
        tenant_id="t-A",
    )
    assert doc.title == "ada@example.com"
    assert doc.id == "t-A_users:u-1"
    assert doc.metadata["kind"] == "supabase.user"


def test_normalize_storage_object():
    doc = normalize_storage_object(
        "avatars",
        {"name": "me.png", "metadata": {"size": 1024, "mimetype": "image/png"}},
        connector_id="c-1",
        tenant_id="t-A",
    )
    assert doc.id == "t-A_avatars/me.png"
    assert doc.metadata["bucket"] == "avatars"
    assert doc.content_type == "image/png"


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity + multi-tenant
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert SupabaseConnector.CONNECTOR_TYPE == "supabase"


def test_auth_type_class_attr():
    assert SupabaseConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert "project_url" in SupabaseConnector.REQUIRED_CONFIG_KEYS
    assert "service_role_key" in SupabaseConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert SupabaseConnector._STATUS_MAP[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert SupabaseConnector._STATUS_MAP[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert SupabaseConnector._STATUS_MAP[429] == ("DEGRADED", "CONNECTED")


def test_independent_instances_per_tenant():
    c1 = SupabaseConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = SupabaseConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# sync() — default no-op
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_default_noop(connector):
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — no OAuth exchange, returns TokenInfo with the api_key
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_apikey_tokeninfo(connector):
    token = await connector.authorize(auth_code="", state="")
    assert token.access_token == SERVICE_ROLE_KEY
    assert token.token_type == "api_key"
    assert token.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# get_document — fetches and normalizes
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_document_normalizes_row(connector):
    respx.get(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(200, json=[{"id": 7, "title": "Doc"}])
    )
    doc = await connector.get_document("posts", 7)
    assert doc is not None
    assert doc.title == "Doc"
    assert doc.id == f"{TENANT_ID}_posts:7"


@respx.mock
@pytest.mark.asyncio
async def test_get_document_none_when_missing(connector):
    respx.get(f"{BASE_URL}/rest/v1/posts").mock(
        return_value=httpx.Response(200, json=[])
    )
    doc = await connector.get_document("posts", 99)
    assert doc is None
