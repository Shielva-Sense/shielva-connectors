"""Unit tests for AlgoliaConnector — respx-mocked, zero real I/O.

Every HTTP request the connector issues is intercepted at the transport
layer by respx, so the tests verify the actual URLs, headers, methods,
and bodies the connector emits — not just method-level mocks.
"""
import json

import httpx
import pytest
import respx

from connector import AlgoliaConnector
from exceptions import (
    AlgoliaAuthError,
    AlgoliaBadRequestError,
    AlgoliaError,
    AlgoliaNetworkError,
    AlgoliaNotFound,
)
from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from tests.conftest import (
    API_KEY,
    APP_ID,
    CONNECTOR_ID,
    FALLBACK_1,
    FALLBACK_2,
    FALLBACK_3,
    READ_DSN,
    TENANT_ID,
    TEST_CONFIG,
    WRITE_HOST,
)


# ═══════════════════════════════════════════════════════════════════════════
# Class identity / contract
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_is_algolia():
    assert AlgoliaConnector.CONNECTOR_TYPE == "algolia"


def test_auth_type_is_api_key():
    assert AlgoliaConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(AlgoliaConnector, "REQUIRED_CONFIG_KEYS")
    assert AlgoliaConnector.REQUIRED_CONFIG_KEYS == ["app_id", "api_key"]


def test_status_map_present_and_typed():
    assert hasattr(AlgoliaConnector, "_STATUS_MAP")
    assert 401 in AlgoliaConnector._STATUS_MAP
    assert 403 in AlgoliaConnector._STATUS_MAP
    assert 429 in AlgoliaConnector._STATUS_MAP


def test_all_required_public_methods_exist():
    required = [
        "list_indexes",
        "create_index_settings",
        "get_index_settings",
        "save_object",
        "save_objects",
        "get_object",
        "delete_object",
        "partial_update_object",
        "browse_index",
        "search_index",
        "multi_search",
        "list_synonyms",
        "save_synonym",
        "list_rules",
        "save_rule",
        "install",
        "authorize",
        "health_check",
        "sync",
    ]
    for name in required:
        assert callable(getattr(AlgoliaConnector, name)), f"missing {name}"


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_install_success(connector):
    """install() probes list_indexes to verify the API key."""
    route = respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(200, json={"items": [], "nbPages": 1})
    )
    result = await connector.install()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_app_id():
    c = AlgoliaConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_api_key():
    c = AlgoliaConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"app_id": APP_ID},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_failure(connector):
    """401 from Algolia → INVALID_CREDENTIALS."""
    respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_api_key_token_info(connector):
    """API-key flow has no OAuth — authorize returns a TokenInfo with the key."""
    info = await connector.authorize()
    assert info.access_token == API_KEY
    assert info.token_type == "api_key"
    assert info.refresh_token is None
    assert info.expires_at is None


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error_401(connector):
    respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(401, json={"message": "bad key"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error_403(connector):
    respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


@pytest.mark.asyncio
async def test_health_check_missing_credentials_returns_offline():
    c = AlgoliaConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth headers — DSN routing
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_read_path_uses_dsn_host(connector):
    """list_indexes is a read → must hit -dsn.algolia.net first."""
    route = respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    await connector.list_indexes()
    assert route.called
    req = route.calls.last.request
    assert req.headers["X-Algolia-Application-Id"] == APP_ID
    assert req.headers["X-Algolia-API-Key"] == API_KEY


@pytest.mark.asyncio
@respx.mock
async def test_write_path_uses_write_host_not_dsn(connector):
    """save_object (write) → must hit .algolia.net (no -dsn)."""
    route = respx.post(f"{WRITE_HOST}/1/indexes/products").mock(
        return_value=httpx.Response(
            201, json={"taskID": 1, "objectID": "x", "createdAt": "now"}
        )
    )
    await connector.save_object("products", {"title": "Shoe"})
    assert route.called
    req = route.calls.last.request
    assert req.headers["X-Algolia-Application-Id"] == APP_ID
    assert req.headers["X-Algolia-API-Key"] == API_KEY


# ═══════════════════════════════════════════════════════════════════════════
# list_indexes
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_indexes_returns_payload(connector):
    payload = {
        "items": [{"name": "products", "entries": 42}, {"name": "users"}],
        "nbPages": 1,
    }
    respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_indexes()
    assert result == payload


# ═══════════════════════════════════════════════════════════════════════════
# create_index_settings / get_index_settings
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_create_index_settings_puts_to_write_host(connector):
    route = respx.put(f"{WRITE_HOST}/1/indexes/products/settings").mock(
        return_value=httpx.Response(200, json={"taskID": 11, "updatedAt": "now"})
    )
    settings = {"searchableAttributes": ["title", "description"]}
    result = await connector.create_index_settings("products", settings)
    assert route.called
    body = json.loads(route.calls.last.request.content.decode())
    assert body == settings
    assert result["taskID"] == 11


@pytest.mark.asyncio
@respx.mock
async def test_get_index_settings_reads_from_dsn(connector):
    route = respx.get(f"{READ_DSN}/1/indexes/products/settings").mock(
        return_value=httpx.Response(
            200, json={"searchableAttributes": ["title"], "minWordSizefor1Typo": 4}
        )
    )
    result = await connector.get_index_settings("products")
    assert route.called
    assert result["minWordSizefor1Typo"] == 4


# ═══════════════════════════════════════════════════════════════════════════
# save_object / save_objects / get_object / delete_object / partial_update_object
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_save_object_no_id_posts_to_write_host(connector):
    """No object_id → POST /1/indexes/{name} on the *write* host."""
    route = respx.post(f"{WRITE_HOST}/1/indexes/products").mock(
        return_value=httpx.Response(
            201, json={"taskID": 99, "objectID": "auto-1", "createdAt": "now"}
        )
    )
    result = await connector.save_object("products", {"title": "Shoe"})
    assert route.called
    assert result["taskID"] == 99


@pytest.mark.asyncio
@respx.mock
async def test_save_object_with_id_puts_to_write_host(connector):
    route = respx.put(f"{WRITE_HOST}/1/indexes/products/sku-123").mock(
        return_value=httpx.Response(
            200, json={"taskID": 100, "objectID": "sku-123", "updatedAt": "now"}
        )
    )
    result = await connector.save_object(
        "products", {"title": "Shoe"}, object_id="sku-123"
    )
    assert route.called
    assert result["objectID"] == "sku-123"


@pytest.mark.asyncio
@respx.mock
async def test_save_objects_batches_with_addObject(connector):
    route = respx.post(f"{WRITE_HOST}/1/indexes/products/batch").mock(
        return_value=httpx.Response(
            200, json={"taskID": 200, "objectIDs": ["a", "b"]}
        )
    )
    await connector.save_objects("products", [{"title": "A"}, {"title": "B"}])
    assert route.called
    body = json.loads(route.calls.last.request.content.decode())
    assert body["requests"][0]["action"] == "addObject"
    assert body["requests"][0]["body"] == {"title": "A"}
    assert body["requests"][1]["body"] == {"title": "B"}


@pytest.mark.asyncio
@respx.mock
async def test_save_objects_supports_partial_action(connector):
    """save_objects accepts other actions like partialUpdateObject."""
    route = respx.post(f"{WRITE_HOST}/1/indexes/products/batch").mock(
        return_value=httpx.Response(200, json={"taskID": 201, "objectIDs": ["a"]})
    )
    await connector.save_objects(
        "products", [{"objectID": "a", "price": 12}], action="partialUpdateObject"
    )
    body = json.loads(route.calls.last.request.content.decode())
    assert body["requests"][0]["action"] == "partialUpdateObject"


@pytest.mark.asyncio
@respx.mock
async def test_get_object_reads_from_dsn(connector):
    route = respx.get(f"{READ_DSN}/1/indexes/products/sku-1").mock(
        return_value=httpx.Response(200, json={"objectID": "sku-1", "title": "Shoe"})
    )
    result = await connector.get_object("products", "sku-1")
    assert route.called
    assert result["objectID"] == "sku-1"


@pytest.mark.asyncio
@respx.mock
async def test_get_object_with_attributes_filter(connector):
    route = respx.get(f"{READ_DSN}/1/indexes/products/sku-1").mock(
        return_value=httpx.Response(200, json={"objectID": "sku-1", "title": "Shoe"})
    )
    await connector.get_object("products", "sku-1", attributes=["title", "price"])
    assert route.called
    qs = route.calls.last.request.url.params
    assert qs.get("attributesToRetrieve") == "title,price"


@pytest.mark.asyncio
@respx.mock
async def test_delete_object_hits_write_host(connector):
    route = respx.delete(f"{WRITE_HOST}/1/indexes/products/sku-123").mock(
        return_value=httpx.Response(200, json={"taskID": 300, "deletedAt": "now"})
    )
    result = await connector.delete_object("products", "sku-123")
    assert route.called
    assert result["taskID"] == 300


@pytest.mark.asyncio
@respx.mock
async def test_partial_update_object_posts_to_partial_endpoint(connector):
    route = respx.post(f"{WRITE_HOST}/1/indexes/products/sku-1/partial").mock(
        return_value=httpx.Response(200, json={"taskID": 400, "updatedAt": "now"})
    )
    await connector.partial_update_object(
        "products", "sku-1", {"price": 99}, create_if_not_exists=False
    )
    assert route.called
    qs = route.calls.last.request.url.params
    assert qs.get("createIfNotExists") == "false"
    body = json.loads(route.calls.last.request.content.decode())
    assert body == {"price": 99}


# ═══════════════════════════════════════════════════════════════════════════
# browse_index / search_index / multi_search
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_browse_index_first_page_no_cursor(connector):
    route = respx.post(f"{READ_DSN}/1/indexes/products/browse").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": [{"objectID": "1"}, {"objectID": "2"}],
                "cursor": "abc",
            },
        )
    )
    result = await connector.browse_index("products")
    assert route.called
    body = json.loads(route.calls.last.request.content.decode())
    assert "cursor" not in body
    assert len(result["hits"]) == 2
    assert result["cursor"] == "abc"


@pytest.mark.asyncio
@respx.mock
async def test_browse_index_with_cursor_sends_it(connector):
    route = respx.post(f"{READ_DSN}/1/indexes/products/browse").mock(
        return_value=httpx.Response(200, json={"hits": []})
    )
    await connector.browse_index("products", cursor="abc")
    body = json.loads(route.calls.last.request.content.decode())
    assert body["cursor"] == "abc"


@pytest.mark.asyncio
@respx.mock
async def test_search_index_posts_query_body(connector):
    payload = {
        "hits": [{"objectID": "1", "name": "Shoe"}],
        "nbHits": 1,
        "page": 0,
        "nbPages": 1,
        "hitsPerPage": 20,
    }
    route = respx.post(f"{READ_DSN}/1/indexes/products/query").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.search_index(
        "products", "shoe", filters="category:running"
    )
    assert route.called
    body = json.loads(route.calls.last.request.content.decode())
    assert body["query"] == "shoe"
    assert body["filters"] == "category:running"
    assert body["hitsPerPage"] == 20
    assert body["page"] == 0
    assert result["hits"][0]["objectID"] == "1"


@pytest.mark.asyncio
@respx.mock
async def test_multi_search_posts_requests_array(connector):
    route = respx.post(f"{READ_DSN}/1/indexes/*/queries").mock(
        return_value=httpx.Response(200, json={"results": [{"hits": []}, {"hits": []}]})
    )
    result = await connector.multi_search(
        [
            {"indexName": "products", "query": "x"},
            {"indexName": "users", "query": "y"},
        ]
    )
    assert route.called
    body = json.loads(route.calls.last.request.content.decode())
    assert len(body["requests"]) == 2
    assert body["requests"][0]["indexName"] == "products"
    assert len(result["results"]) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Synonyms
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_synonyms_posts_to_search_endpoint(connector):
    payload = {"hits": [{"objectID": "syn1", "synonyms": ["car", "auto"]}], "nbHits": 1}
    route = respx.post(f"{READ_DSN}/1/indexes/products/synonyms/search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_synonyms("products", query="car")
    assert route.called
    body = json.loads(route.calls.last.request.content.decode())
    assert body["query"] == "car"
    assert result["hits"][0]["objectID"] == "syn1"


@pytest.mark.asyncio
@respx.mock
async def test_save_synonym_puts_with_replicas_param(connector):
    route = respx.put(f"{WRITE_HOST}/1/indexes/products/synonyms/syn-1").mock(
        return_value=httpx.Response(200, json={"taskID": 500, "updatedAt": "now"})
    )
    synonym = {"objectID": "syn-1", "type": "synonym", "synonyms": ["a", "b"]}
    await connector.save_synonym(
        "products", "syn-1", synonym, forward_to_replicas=True
    )
    assert route.called
    qs = route.calls.last.request.url.params
    assert qs.get("forwardToReplicas") == "true"


# ═══════════════════════════════════════════════════════════════════════════
# Rules
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_rules_posts_to_search_endpoint(connector):
    route = respx.post(f"{READ_DSN}/1/indexes/products/rules/search").mock(
        return_value=httpx.Response(200, json={"hits": [], "nbHits": 0})
    )
    result = await connector.list_rules("products", query="promo")
    assert route.called
    body = json.loads(route.calls.last.request.content.decode())
    assert body["query"] == "promo"
    assert result["nbHits"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_save_rule_puts_with_replicas_param(connector):
    route = respx.put(f"{WRITE_HOST}/1/indexes/products/rules/rule-1").mock(
        return_value=httpx.Response(200, json={"taskID": 600, "updatedAt": "now"})
    )
    rule = {
        "objectID": "rule-1",
        "conditions": [{"pattern": "shoe", "anchoring": "contains"}],
        "consequence": {"params": {"filters": "category:shoes"}},
    }
    await connector.save_rule("products", "rule-1", rule, forward_to_replicas=False)
    assert route.called
    qs = route.calls.last.request.url.params
    assert qs.get("forwardToReplicas") == "false"


# ═══════════════════════════════════════════════════════════════════════════
# Host fallback / network errors
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_host_fallback_on_503(connector):
    """503 on the primary read host → next host in the rotation is tried.

    The rotation always starts with -dsn and shuffles the three algolianet.com
    fallbacks — all three must be mocked because the order is non-deterministic.
    """
    respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(503, json={"message": "primary down"})
    )
    respx.get(f"{FALLBACK_1}/1/indexes").mock(
        return_value=httpx.Response(200, json={"items": [{"name": "ok"}]})
    )
    respx.get(f"{FALLBACK_2}/1/indexes").mock(
        return_value=httpx.Response(200, json={"items": [{"name": "ok"}]})
    )
    respx.get(f"{FALLBACK_3}/1/indexes").mock(
        return_value=httpx.Response(200, json={"items": [{"name": "ok"}]})
    )
    result = await connector.list_indexes()
    assert result["items"][0]["name"] == "ok"


@pytest.mark.asyncio
@respx.mock
async def test_all_hosts_fail_raises_network_error(connector, no_retry_sleep):
    """Every host returning 5xx → AlgoliaNetworkError (after retry exhaustion)."""
    respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(503)
    )
    respx.get(f"{FALLBACK_1}/1/indexes").mock(
        return_value=httpx.Response(503)
    )
    respx.get(f"{FALLBACK_2}/1/indexes").mock(
        return_value=httpx.Response(503)
    )
    respx.get(f"{FALLBACK_3}/1/indexes").mock(
        return_value=httpx.Response(503)
    )
    with pytest.raises(AlgoliaNetworkError):
        await connector.list_indexes()


# ═══════════════════════════════════════════════════════════════════════════
# Error mapping
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_404_raises_not_found(connector):
    respx.get(f"{READ_DSN}/1/indexes/nope/settings").mock(
        return_value=httpx.Response(404, json={"message": "Index does not exist"})
    )
    with pytest.raises(AlgoliaNotFound):
        await connector.get_index_settings("nope")


@pytest.mark.asyncio
@respx.mock
async def test_401_raises_auth_error_on_write(connector):
    respx.post(f"{WRITE_HOST}/1/indexes/products").mock(
        return_value=httpx.Response(401, json={"message": "Invalid key"})
    )
    with pytest.raises(AlgoliaAuthError):
        await connector.save_object("products", {"x": 1})


@pytest.mark.asyncio
@respx.mock
async def test_400_raises_bad_request(connector):
    respx.post(f"{READ_DSN}/1/indexes/products/query").mock(
        return_value=httpx.Response(400, json={"message": "Invalid filter syntax"})
    )
    with pytest.raises(AlgoliaBadRequestError):
        await connector.search_index("products", "x", filters="badbad")


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_sync_inventory_mode(connector):
    """Default sync (full=False) only enumerates indexes."""
    respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"name": "products", "entries": 42},
                    {"name": "users", "entries": 7},
                ]
            },
        )
    )
    result = await connector.sync(full=False)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
@respx.mock
async def test_sync_full_browses_each_index(connector):
    """sync(full=True) walks browse_index for every enumerated index."""
    respx.get(f"{READ_DSN}/1/indexes").mock(
        return_value=httpx.Response(
            200, json={"items": [{"name": "products", "entries": 2}]}
        )
    )
    respx.post(f"{READ_DSN}/1/indexes/products/browse").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": [
                    {"objectID": "p1", "title": "Shoe"},
                    {"objectID": "p2", "title": "Hat"},
                ]
            },
        )
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    # 1 index doc + 2 object docs
    assert result.documents_found == 3
    assert result.documents_synced == 3


@pytest.mark.asyncio
async def test_sync_missing_credentials_returns_failed():
    c = AlgoliaConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.sync()
    assert result.status == SyncStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_different_tenants_different_instances():
    c1 = AlgoliaConnector(tenant_id="tenant-A", connector_id="c1", config=TEST_CONFIG)
    c2 = AlgoliaConnector(tenant_id="tenant-B", connector_id="c2", config=TEST_CONFIG)
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — id = f"{tenant_id}_{source_id}" SOC enforcement
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_object_tenant_scoped_id():
    from helpers.normalizer import normalize_object

    doc = normalize_object(
        {"objectID": "p1", "title": "Shoe", "description": "Comfy"},
        tenant_id="tenant-A",
        connector_id="c1",
        index_name="products",
    )
    assert doc.id == "tenant-A_p1"
    assert doc.source_id == "p1"
    assert doc.title == "Shoe"
    assert doc.content == "Comfy"
    assert doc.source == "algolia.products"
    assert doc.tenant_id == "tenant-A"
    assert doc.metadata["index_name"] == "products"


def test_normalize_object_falls_back_to_object_id_when_no_title():
    from helpers.normalizer import normalize_object

    doc = normalize_object(
        {"objectID": "abc-123"},
        tenant_id="t",
        connector_id="c",
        index_name="ix",
    )
    assert doc.title == "abc-123"


def test_normalize_index_emits_kind_metadata():
    from helpers.normalizer import normalize_index

    doc = normalize_index(
        {"name": "products", "entries": 42, "dataSize": 100},
        tenant_id="t",
        connector_id="c",
    )
    assert doc.id == "t_products"
    assert doc.source_id == "products"
    assert doc.metadata["entries"] == 42
    assert doc.metadata["kind"] == "algolia.index"


# ═══════════════════════════════════════════════════════════════════════════
# helpers.utils.build_*_hosts
# ═══════════════════════════════════════════════════════════════════════════


def test_build_read_hosts_primary_first():
    from helpers.utils import build_read_hosts

    hosts = build_read_hosts(APP_ID)
    assert hosts[0] == f"https://{APP_ID}-dsn.algolia.net"
    assert len(hosts) == 4
    for fb in hosts[1:]:
        assert ".algolianet.com" in fb


def test_build_write_hosts_primary_first():
    from helpers.utils import build_write_hosts

    hosts = build_write_hosts(APP_ID)
    assert hosts[0] == f"https://{APP_ID}.algolia.net"
    assert "-dsn." not in hosts[0]
    assert len(hosts) == 4


def test_build_hosts_requires_app_id():
    from helpers.utils import build_read_hosts, build_write_hosts

    with pytest.raises(ValueError):
        build_read_hosts("")
    with pytest.raises(ValueError):
        build_write_hosts("")
