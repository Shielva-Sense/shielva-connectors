"""Unit tests for WeaviateConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import WeaviateConnector
from exceptions import (
    WeaviateAuthError,
    WeaviateBadRequestError,
    WeaviateConflictError,
    WeaviateError,
    WeaviateNotFoundError,
    WeaviateValidationError,
)

from tests.conftest import (
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
    WEAVIATE_BASE,
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
async def test_install_missing_base_url(connector):
    connector.config.pop("base_url", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_invalid_base_url(connector):
    connector.config["base_url"] = "not-a-url"
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "http" in (result.message or "").lower()


@pytest.mark.asyncio
async def test_install_allows_anonymous_self_hosted(connector):
    """Anonymous self-hosted clusters omit api_key."""
    connector.config["api_key"] = ""
    connector.api_key = ""
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer prefix) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer_prefixed(connector):
    """Connector must send api_key as `Bearer <key>` in Authorization."""
    route = respx.get(f"{WEAVIATE_BASE}/v1/schema").mock(
        return_value=httpx.Response(200, json={"classes": []})
    )
    await connector.list_classes()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_API_KEY}"


@respx.mock
@pytest.mark.asyncio
async def test_anonymous_cluster_omits_authorization_header():
    """When api_key is blank, no Authorization header is sent."""
    cfg = dict(TEST_CONFIG)
    cfg["api_key"] = ""
    c = WeaviateConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    route = respx.get(f"{WEAVIATE_BASE}/v1/schema").mock(
        return_value=httpx.Response(200, json={"classes": []})
    )
    await c.list_classes()
    assert route.called
    assert route.calls[0].request.headers.get("authorization") is None


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_weaviate_auth_error(connector):
    respx.get(f"{WEAVIATE_BASE}/v1/schema").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    with pytest.raises(WeaviateAuthError):
        await connector.list_classes()


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_403_raises_weaviate_auth_error(connector):
    respx.get(f"{WEAVIATE_BASE}/v1/schema").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    with pytest.raises(WeaviateAuthError):
        await connector.list_classes()


# ═══════════════════════════════════════════════════════════════════════════
# health_check
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{WEAVIATE_BASE}/v1/.well-known/ready").mock(
        return_value=httpx.Response(200, json={})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{WEAVIATE_BASE}/v1/.well-known/ready").mock(
        return_value=httpx.Response(401, json={"message": "Invalid key"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Cluster meta
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_meta_success(connector):
    payload = {"version": "1.24.0", "hostname": "weaviate-0", "modules": {}}
    respx.get(f"{WEAVIATE_BASE}/v1/meta").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.get_meta()
    assert result["version"] == "1.24.0"


# ═══════════════════════════════════════════════════════════════════════════
# Schema (classes)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_classes_success(connector):
    payload = {"classes": [{"class": "Article", "properties": []}]}
    route = respx.get(f"{WEAVIATE_BASE}/v1/schema").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_classes()
    assert route.called
    assert result["classes"][0]["class"] == "Article"


@respx.mock
@pytest.mark.asyncio
async def test_create_class_posts_raw_body(connector):
    class_body = {
        "class": "Article",
        "vectorizer": "text2vec-openai",
        "properties": [{"name": "title", "dataType": ["text"]}],
    }
    route = respx.post(f"{WEAVIATE_BASE}/v1/schema").mock(
        return_value=httpx.Response(200, json=class_body)
    )
    result = await connector.create_class(class_body)
    assert route.called
    body = json.loads(route.calls[0].request.content.decode())
    assert body == class_body
    assert result["class"] == "Article"


@respx.mock
@pytest.mark.asyncio
async def test_get_class_success(connector):
    respx.get(f"{WEAVIATE_BASE}/v1/schema/Article").mock(
        return_value=httpx.Response(200, json={"class": "Article"})
    )
    result = await connector.get_class("Article")
    assert result["class"] == "Article"


@respx.mock
@pytest.mark.asyncio
async def test_get_class_not_found(connector):
    respx.get(f"{WEAVIATE_BASE}/v1/schema/Missing").mock(
        return_value=httpx.Response(404, json={"message": "class not found"})
    )
    with pytest.raises(WeaviateNotFoundError):
        await connector.get_class("Missing")


@respx.mock
@pytest.mark.asyncio
async def test_delete_class_success(connector):
    route = respx.delete(f"{WEAVIATE_BASE}/v1/schema/Article").mock(
        return_value=httpx.Response(200, json={})
    )
    result = await connector.delete_class("Article")
    assert route.called
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Objects
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_objects_passes_query_params(connector):
    payload = {"objects": [{"id": "11111111-1111-1111-1111-111111111111", "class": "Article"}]}
    route = respx.get(f"{WEAVIATE_BASE}/v1/objects").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_objects(class_name="Article", limit=50, include="vector")
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("class") == "Article"
    assert qs.get("limit") == "50"
    assert qs.get("include") == "vector"
    assert result["objects"][0]["class"] == "Article"


@respx.mock
@pytest.mark.asyncio
async def test_create_object_posts_full_body(connector):
    route = respx.post(f"{WEAVIATE_BASE}/v1/objects").mock(
        return_value=httpx.Response(200, json={"id": "new-uuid", "class": "Article"})
    )
    properties = {"title": "Hello", "body": "world"}
    result = await connector.create_object(
        "Article",
        properties,
        vector=[0.1, 0.2, 0.3],
        object_id="new-uuid",
        tenant="tenantA",
    )
    body = json.loads(route.calls[0].request.content.decode())
    assert body["class"] == "Article"
    assert body["properties"] == properties
    assert body["vector"] == [0.1, 0.2, 0.3]
    assert body["id"] == "new-uuid"
    assert body["tenant"] == "tenantA"
    assert result["id"] == "new-uuid"


@respx.mock
@pytest.mark.asyncio
async def test_get_object_success(connector):
    oid = "abc-123"
    respx.get(f"{WEAVIATE_BASE}/v1/objects/Article/{oid}").mock(
        return_value=httpx.Response(200, json={"id": oid, "class": "Article"})
    )
    result = await connector.get_object("Article", oid)
    assert result["id"] == oid


@respx.mock
@pytest.mark.asyncio
async def test_update_object_uses_patch(connector):
    oid = "abc-123"
    route = respx.patch(f"{WEAVIATE_BASE}/v1/objects/Article/{oid}").mock(
        return_value=httpx.Response(200, json={"id": oid})
    )
    properties = {"title": "Updated"}
    await connector.update_object("Article", oid, properties)
    assert route.called
    body = json.loads(route.calls[0].request.content.decode())
    assert body["properties"] == properties
    assert body["class"] == "Article"
    assert body["id"] == oid


@respx.mock
@pytest.mark.asyncio
async def test_delete_object_success(connector):
    oid = "abc-123"
    route = respx.delete(f"{WEAVIATE_BASE}/v1/objects/Article/{oid}").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_object("Article", oid)
    assert route.called
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Batch
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_batch_create_objects_posts_envelope(connector):
    payload = [{"class": "Article", "id": "o1", "result": {"status": "SUCCESS"}}]
    route = respx.post(f"{WEAVIATE_BASE}/v1/batch/objects").mock(
        return_value=httpx.Response(200, json=payload)
    )
    objects = [
        {"class": "Article", "properties": {"title": "a"}},
        {"class": "Article", "properties": {"title": "b"}},
    ]
    result = await connector.batch_create_objects(objects, consistency_level="QUORUM")
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"objects": objects}
    qs = route.calls[0].request.url.params
    assert qs.get("consistency_level") == "QUORUM"
    assert result == payload


# ═══════════════════════════════════════════════════════════════════════════
# GraphQL
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_graphql_query_posts_body(connector):
    payload = {"data": {"Get": {"Article": [{"title": "hello"}]}}}
    route = respx.post(f"{WEAVIATE_BASE}/v1/graphql").mock(
        return_value=httpx.Response(200, json=payload)
    )
    query = "{ Get { Article { title } } }"
    result = await connector.graphql_query(query)
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"query": query}
    assert result["data"]["Get"]["Article"][0]["title"] == "hello"


@respx.mock
@pytest.mark.asyncio
async def test_graphql_query_with_variables(connector):
    route = respx.post(f"{WEAVIATE_BASE}/v1/graphql").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )
    await connector.graphql_query("query Q($l:Int){...}", variables={"l": 5})
    body = json.loads(route.calls[0].request.content.decode())
    assert body["variables"] == {"l": 5}


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenancy
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_tenants_success(connector):
    respx.get(f"{WEAVIATE_BASE}/v1/schema/Article/tenants").mock(
        return_value=httpx.Response(200, json=[{"name": "tA"}, {"name": "tB"}])
    )
    result = await connector.list_tenants("Article")
    assert isinstance(result, list)
    assert result[0]["name"] == "tA"


@respx.mock
@pytest.mark.asyncio
async def test_create_tenant_posts_list_body(connector):
    route = respx.post(f"{WEAVIATE_BASE}/v1/schema/Article/tenants").mock(
        return_value=httpx.Response(200, json=[{"name": "tNew"}])
    )
    tenants = [{"name": "tNew", "activityStatus": "HOT"}]
    await connector.create_tenant("Article", tenants)
    body = json.loads(route.calls[0].request.content.decode())
    assert body == tenants


@respx.mock
@pytest.mark.asyncio
async def test_delete_tenant_sends_name_list(connector):
    route = respx.delete(f"{WEAVIATE_BASE}/v1/schema/Article/tenants").mock(
        return_value=httpx.Response(200, json={})
    )
    await connector.delete_tenant("Article", ["tOld"])
    body = json.loads(route.calls[0].request.content.decode())
    assert body == ["tOld"]


# ═══════════════════════════════════════════════════════════════════════════
# Backups
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_create_backup_posts_envelope(connector):
    route = respx.post(f"{WEAVIATE_BASE}/v1/backups/s3").mock(
        return_value=httpx.Response(200, json={"id": "bk1", "status": "STARTED"})
    )
    result = await connector.create_backup("s3", "bk1", include=["Article"])
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"id": "bk1", "include": ["Article"]}
    assert result["status"] == "STARTED"


@respx.mock
@pytest.mark.asyncio
async def test_get_backup_status_success(connector):
    respx.get(f"{WEAVIATE_BASE}/v1/backups/s3/bk1").mock(
        return_value=httpx.Response(200, json={"status": "SUCCESS"})
    )
    result = await connector.get_backup_status("s3", "bk1")
    assert result["status"] == "SUCCESS"


# ═══════════════════════════════════════════════════════════════════════════
# Error mapping
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_400_raises_bad_request(connector):
    respx.post(f"{WEAVIATE_BASE}/v1/schema").mock(
        return_value=httpx.Response(400, json={"message": "bad schema"})
    )
    with pytest.raises(WeaviateBadRequestError):
        await connector.create_class({"class": ""})


@respx.mock
@pytest.mark.asyncio
async def test_409_raises_conflict(connector):
    respx.post(f"{WEAVIATE_BASE}/v1/schema").mock(
        return_value=httpx.Response(409, json={"message": "class exists"})
    )
    with pytest.raises(WeaviateConflictError):
        await connector.create_class({"class": "Article"})


@respx.mock
@pytest.mark.asyncio
async def test_422_raises_validation(connector):
    respx.post(f"{WEAVIATE_BASE}/v1/objects").mock(
        return_value=httpx.Response(422, json={"message": "invalid property"})
    )
    with pytest.raises(WeaviateValidationError):
        await connector.create_object("Article", {"x": 1})


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{WEAVIATE_BASE}/v1/schema").mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(200, json={"classes": [{"class": "after-retry"}]}),
        ]
    )
    result = await connector.list_classes()
    assert route.call_count == 2
    assert result["classes"][0]["class"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{WEAVIATE_BASE}/v1/schema").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"classes": []}),
        ]
    )
    result = await connector.list_classes()
    assert route.call_count == 2
    assert result == {"classes": []}


# ═══════════════════════════════════════════════════════════════════════════
# Sync flow
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_sync_pages_through_objects(connector):
    respx.get(f"{WEAVIATE_BASE}/v1/schema").mock(
        return_value=httpx.Response(200, json={"classes": [{"class": "Article"}]})
    )
    page1 = [
        {
            "id": "u1",
            "class": "Article",
            "properties": {"title": "First"},
            "creationTimeUnix": 1700000000000,
            "lastUpdateTimeUnix": 1700000001000,
        }
    ]
    respx.get(f"{WEAVIATE_BASE}/v1/objects").mock(
        return_value=httpx.Response(200, json={"objects": page1})
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_object_basic():
    from helpers.normalizer import normalize_object

    raw = {
        "id": "11111111-1111-1111-1111-111111111111",
        "class": "Article",
        "properties": {"title": "Hello World", "body": "lorem"},
        "creationTimeUnix": 1700000000000,
        "lastUpdateTimeUnix": 1700000001000,
        "tenant": "tA",
    }
    doc = normalize_object(raw, "conn-1", "tenant-A")
    assert doc.source_id == "11111111-1111-1111-1111-111111111111"
    assert doc.id.startswith("tenant-A_")
    assert doc.title == "Hello World"
    assert "lorem" in doc.content
    assert doc.metadata["class"] == "Article"
    assert doc.metadata["tenant"] == "tA"
    assert doc.source == "weaviate.Article"


def test_normalize_object_falls_back_to_class_id_title():
    from helpers.normalizer import normalize_object

    raw = {
        "id": "xyz",
        "class": "Thing",
        "properties": {"random": 1},
    }
    doc = normalize_object(raw, "conn-1", "tenant-A")
    assert doc.title == "Thing:xyz"


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert WeaviateConnector.CONNECTOR_TYPE == "weaviate"


def test_auth_type_class_attr():
    assert WeaviateConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(WeaviateConnector, "REQUIRED_CONFIG_KEYS")
    assert "base_url" in WeaviateConnector.REQUIRED_CONFIG_KEYS
    assert "api_key" in WeaviateConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert 401 in WeaviateConnector._STATUS_MAP
    assert 403 in WeaviateConnector._STATUS_MAP
    assert 429 in WeaviateConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = WeaviateConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = WeaviateConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_base_url_is_per_tenant():
    """Critical: base_url is install_field, not a class constant.

    Different tenants point at different Weaviate clusters.
    """
    c1 = WeaviateConnector(
        tenant_id="t-A",
        connector_id="conn-1",
        config={"base_url": "https://cluster-a.weaviate.network", "api_key": "k1"},
    )
    c2 = WeaviateConnector(
        tenant_id="t-B",
        connector_id="conn-2",
        config={"base_url": "https://cluster-b.weaviate.network", "api_key": "k2"},
    )
    assert c1.base_url != c2.base_url
    assert c1.http_client._base_url != c2.http_client._base_url
