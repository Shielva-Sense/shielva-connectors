"""Unit tests for AttioConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import AttioConnector
from exceptions import AttioAuthError, AttioError, AttioNotFoundError

from tests.conftest import (
    ATTIO_BASE,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
    TEST_WORKSPACE_SLUG,
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
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — API-key passthrough
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_passthrough_token(connector):
    token = await connector.authorize()
    assert token.access_token == TEST_API_KEY
    assert token.token_type == "api_key"
    assert token.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer prefix) + 401 path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer(connector):
    """Connector must send the api_key as `Bearer <key>`."""
    route = respx.get(f"{ATTIO_BASE}/self").mock(
        return_value=httpx.Response(200, json={"workspace_id": "ws-1"})
    )
    await connector.list_workspaces()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_API_KEY}"
    assert route.calls[0].request.headers.get("accept") == "application/json"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises(connector):
    respx.get(f"{ATTIO_BASE}/self").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    with pytest.raises(AttioAuthError):
        await connector.list_workspaces()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{ATTIO_BASE}/self").mock(
        return_value=httpx.Response(200, json={"workspace_id": "ws-1", "name": "Acme"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{ATTIO_BASE}/self").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_health_check_forbidden(connector):
    respx.get(f"{ATTIO_BASE}/self").mock(
        return_value=httpx.Response(403, json={"message": "Insufficient scope"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


@pytest.mark.asyncio
async def test_health_check_missing_api_key(connector):
    connector.config.pop("api_key", None)
    connector.api_key = ""
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Objects + attributes
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_objects_success(connector):
    payload = {"data": [{"id": {"object_id": "obj-people"}, "api_slug": "people"}]}
    respx.get(f"{ATTIO_BASE}/objects").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_objects()
    assert result == payload


@respx.mock
@pytest.mark.asyncio
async def test_list_attributes_success(connector):
    payload = {"data": [{"id": {"attribute_id": "attr-1"}, "title": "Name"}]}
    respx.get(f"{ATTIO_BASE}/objects/people/attributes").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_attributes("people")
    assert result == payload


# ═══════════════════════════════════════════════════════════════════════════
# Records — list / get / create / update / assert / delete
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_records_posts_query_body(connector):
    route = respx.post(f"{ATTIO_BASE}/objects/people/records/query").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": {"record_id": "r1"}}]}
        )
    )
    result = await connector.list_records("people", limit=25, offset=0)
    assert route.called
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["limit"] == 25
    assert body["offset"] == 0
    assert result["data"][0]["id"]["record_id"] == "r1"


@respx.mock
@pytest.mark.asyncio
async def test_list_records_with_filter_and_sorts(connector):
    route = respx.post(f"{ATTIO_BASE}/objects/people/records/query").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    await connector.list_records(
        "people",
        limit=10,
        filter={"name": {"$contains": "Ada"}},
        sorts=[{"direction": "desc", "attribute": "created_at"}],
    )
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["filter"] == {"name": {"$contains": "Ada"}}
    assert body["sorts"] == [{"direction": "desc", "attribute": "created_at"}]


@respx.mock
@pytest.mark.asyncio
async def test_get_record_success(connector):
    rid = "rec-42"
    respx.get(f"{ATTIO_BASE}/objects/people/records/{rid}").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"id": {"record_id": rid}, "values": {"name": [{"value": "Ada"}]}}},
        )
    )
    result = await connector.get_record("people", rid)
    assert result["data"]["id"]["record_id"] == rid


@respx.mock
@pytest.mark.asyncio
async def test_get_record_not_found(connector):
    respx.get(f"{ATTIO_BASE}/objects/people/records/missing").mock(
        return_value=httpx.Response(404, json={"message": "record not found"})
    )
    with pytest.raises(AttioNotFoundError):
        await connector.get_record("people", "missing")


@respx.mock
@pytest.mark.asyncio
async def test_create_record_posts_data_envelope(connector):
    route = respx.post(f"{ATTIO_BASE}/objects/people/records").mock(
        return_value=httpx.Response(
            201, json={"data": {"id": {"record_id": "new-rec"}}}
        )
    )
    values = {"name": [{"value": "Ada"}]}
    result = await connector.create_record("people", values)
    assert result["data"]["id"]["record_id"] == "new-rec"
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == {"data": {"values": values}}


@respx.mock
@pytest.mark.asyncio
async def test_update_record_patches(connector):
    rid = "rec-1"
    route = respx.patch(f"{ATTIO_BASE}/objects/people/records/{rid}").mock(
        return_value=httpx.Response(200, json={"data": {"id": {"record_id": rid}}})
    )
    await connector.update_record("people", rid, {"name": [{"value": "Lovelace"}]})
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_assert_record_puts_with_matching_attr(connector):
    route = respx.put(f"{ATTIO_BASE}/objects/people/records").mock(
        return_value=httpx.Response(200, json={"data": {"id": {"record_id": "x"}}})
    )
    await connector.assert_record(
        "people",
        matching_attribute="email",
        values={"email": [{"value": "ada@ex.com"}]},
    )
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("matching_attribute") == "email"


@respx.mock
@pytest.mark.asyncio
async def test_delete_record(connector):
    rid = "rec-9"
    respx.delete(f"{ATTIO_BASE}/objects/people/records/{rid}").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_record("people", rid)
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Lists + entries
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_lists_success(connector):
    respx.get(f"{ATTIO_BASE}/lists").mock(
        return_value=httpx.Response(200, json={"data": [{"id": {"list_id": "l1"}}]})
    )
    result = await connector.list_lists()
    assert result["data"][0]["id"]["list_id"] == "l1"


@respx.mock
@pytest.mark.asyncio
async def test_get_list_success(connector):
    respx.get(f"{ATTIO_BASE}/lists/l1").mock(
        return_value=httpx.Response(200, json={"data": {"id": {"list_id": "l1"}}})
    )
    result = await connector.get_list("l1")
    assert result["data"]["id"]["list_id"] == "l1"


@respx.mock
@pytest.mark.asyncio
async def test_list_list_entries(connector):
    route = respx.post(f"{ATTIO_BASE}/lists/l1/entries/query").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    await connector.list_list_entries("l1", limit=10, offset=0)
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == {"limit": 10, "offset": 0}


# ═══════════════════════════════════════════════════════════════════════════
# Notes
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_notes(connector):
    route = respx.get(f"{ATTIO_BASE}/notes").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    await connector.list_notes(parent_object="people", parent_record_id="rec-1")
    qs = route.calls[0].request.url.params
    assert qs.get("parent_object") == "people"
    assert qs.get("parent_record_id") == "rec-1"


@respx.mock
@pytest.mark.asyncio
async def test_create_note(connector):
    route = respx.post(f"{ATTIO_BASE}/notes").mock(
        return_value=httpx.Response(
            201, json={"data": {"id": {"note_id": "n1"}}}
        )
    )
    result = await connector.create_note(
        parent_object="people",
        parent_record_id="rec-1",
        title="My note",
        content="hi",
    )
    assert result["data"]["id"]["note_id"] == "n1"
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["data"]["parent_object"] == "people"
    assert body["data"]["title"] == "My note"


# ═══════════════════════════════════════════════════════════════════════════
# Tasks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_tasks(connector):
    respx.get(f"{ATTIO_BASE}/tasks").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    result = await connector.list_tasks()
    assert result == {"data": []}


@respx.mock
@pytest.mark.asyncio
async def test_create_task(connector):
    route = respx.post(f"{ATTIO_BASE}/tasks").mock(
        return_value=httpx.Response(
            201, json={"data": {"id": {"task_id": "t1"}}}
        )
    )
    await connector.create_task("Follow up", deadline_at="2026-07-01T00:00:00Z")
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["data"]["content"] == "Follow up"
    assert body["data"]["deadline_at"] == "2026-07-01T00:00:00Z"
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{ATTIO_BASE}/objects").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
            httpx.Response(200, json={"data": [{"id": "after-retry"}]}),
        ]
    )
    result = await connector.list_objects()
    assert route.call_count == 2
    assert result["data"][0]["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{ATTIO_BASE}/objects").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    result = await connector.list_objects()
    assert route.call_count == 2
    assert result == {"data": []}


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_sync_iterates_configured_objects(connector, no_retry_sleep):
    """sync() should query records for each slug in sync_objects."""
    respx.post(f"{ATTIO_BASE}/objects/people/records/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": {"record_id": "p1"},
                        "values": {"name": [{"full_name": "Ada Lovelace"}]},
                        "created_at": "2026-06-01T00:00:00Z",
                    }
                ]
            },
        )
    )
    respx.post(f"{ATTIO_BASE}/objects/companies/records/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": {"record_id": "c1"},
                        "values": {"name": [{"value": "Analytical Engines Co"}]},
                        "created_at": "2026-06-01T00:00:00Z",
                    }
                ]
            },
        )
    )
    result = await connector.sync()
    assert result.status == SyncStatus.SUCCESS
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_no_objects_returns_success(connector):
    connector.sync_objects = []
    result = await connector.sync()
    assert result.status == SyncStatus.SUCCESS
    assert result.documents_found == 0


@pytest.mark.asyncio
async def test_sync_missing_api_key(connector):
    connector.api_key = ""
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert AttioConnector.CONNECTOR_TYPE == "attio"


def test_auth_type_class_attr():
    assert AttioConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(AttioConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in AttioConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = AttioConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = AttioConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Mock-based instantiation smoke (no abstract TypeError)
# ═══════════════════════════════════════════════════════════════════════════

def test_can_instantiate_no_abstract_error():
    """Regression: previously raised `Can't instantiate abstract class` because
    sync() was missing. This test makes that failure mode explicit."""
    conn = AttioConnector(tenant_id="t", connector_id="c", config={"api_key": "k"})
    assert conn.CONNECTOR_TYPE == "attio"


def test_mocked_http_client_fixture(connector, mock_AttioHTTPClient):
    """The mock_AttioHTTPClient fixture patches connector.AttioHTTPClient."""
    fresh = AttioConnector(tenant_id="t", connector_id="c", config={"api_key": "k"})
    assert fresh.http_client is mock_AttioHTTPClient
