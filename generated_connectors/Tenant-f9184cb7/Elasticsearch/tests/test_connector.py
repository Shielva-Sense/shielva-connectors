"""Unit tests for ElasticsearchConnector — respx-mocked, zero real I/O.

Every HTTP request the connector issues is intercepted at the transport
layer by respx, so the tests verify the actual URLs, headers, methods, and
bodies the connector emits — not just method-level mocks.
"""
import base64

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import ElasticsearchConnector
from exceptions import (
    ElasticsearchAuthError,
    ElasticsearchError,
    ElasticsearchNetworkError,
    ElasticsearchNotFound,
    ElasticsearchRateLimitError,
)
from helpers.utils import serialize_ndjson

from tests.conftest import (
    CONNECTOR_ID,
    HOST,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
    TEST_PASSWORD,
    TEST_USERNAME,
)


# ═══════════════════════════════════════════════════════════════════════════
# Identity / class-attribute contracts
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_is_elasticsearch():
    assert ElasticsearchConnector.CONNECTOR_TYPE == "elasticsearch"


def test_auth_type_is_api_key():
    assert ElasticsearchConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_only_base_url():
    assert ElasticsearchConnector.REQUIRED_CONFIG_KEYS == ["base_url"]


def test_status_map_is_public_class_attr():
    sm = ElasticsearchConnector._STATUS_MAP
    assert sm[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert sm[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert sm[429] == ("DEGRADED", "CONNECTED")


def test_independent_instances_per_tenant():
    c1 = ElasticsearchConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = ElasticsearchConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_host_alias_accepted_for_base_url():
    """Legacy `host` config key still configures the cluster URL."""
    c = ElasticsearchConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"host": HOST, "api_key": TEST_API_KEY},
    )
    assert c.base_url == HOST


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_install_success_with_api_key(connector):
    route = respx.get(f"{HOST}/").mock(
        return_value=httpx.Response(
            200,
            json={"name": "node-1", "cluster_name": "es", "version": {"number": "8.13.0"}},
        )
    )
    result = await connector.install()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    # Auth header is ApiKey (NOT Bearer)
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"ApiKey {TEST_API_KEY}"
    assert not req.headers["Authorization"].lower().startswith("bearer ")


@pytest.mark.asyncio
@respx.mock
async def test_install_success_with_basic_auth():
    c = ElasticsearchConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "base_url": HOST,
            "username": TEST_USERNAME,
            "password": TEST_PASSWORD,
            "verify_ssl": True,
        },
    )
    route = respx.get(f"{HOST}/").mock(
        return_value=httpx.Response(200, json={"name": "node-1"})
    )
    result = await c.install()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY
    expected = base64.b64encode(
        f"{TEST_USERNAME}:{TEST_PASSWORD}".encode("utf-8"),
    ).decode("ascii")
    assert route.calls.last.request.headers["Authorization"] == f"Basic {expected}"


@pytest.mark.asyncio
async def test_install_missing_base_url():
    c = ElasticsearchConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": TEST_API_KEY},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
@respx.mock
async def test_install_anonymous_self_hosted_allowed():
    """api_key/username/password all blank → anonymous mode, install probes /."""
    c = ElasticsearchConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"base_url": HOST},
    )
    respx.get(f"{HOST}/").mock(
        return_value=httpx.Response(200, json={"name": "anon-cluster"})
    )
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_failure(connector):
    respx.get(f"{HOST}/").mock(
        return_value=httpx.Response(401, json={"error": {"reason": "bad key"}})
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Authorize (TokenInfo ABI shim)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_token_info(connector):
    info = await connector.authorize()
    assert info.access_token == TEST_API_KEY
    assert info.token_type == "api_key"


# ═══════════════════════════════════════════════════════════════════════════
# health_check() — probes /_cluster/health
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy_when_green(connector):
    route = respx.get(f"{HOST}/_cluster/health").mock(
        return_value=httpx.Response(200, json={"status": "green", "cluster_name": "es"})
    )
    result = await connector.health_check()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_degraded_when_red(connector):
    respx.get(f"{HOST}/_cluster/health").mock(
        return_value=httpx.Response(200, json={"status": "red"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_failure(connector):
    respx.get(f"{HOST}/_cluster/health").mock(
        return_value=httpx.Response(401, json={"error": {"reason": "bad key"}})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Cluster health user-facing method
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_cluster_health_returns_payload(connector):
    payload = {
        "cluster_name": "es",
        "status": "green",
        "number_of_nodes": 3,
        "active_primary_shards": 8,
    }
    route = respx.get(f"{HOST}/_cluster/health").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.get_cluster_health(level="cluster")
    assert route.called
    assert result == payload
    assert route.calls.last.request.url.params["level"] == "cluster"


@pytest.mark.asyncio
@respx.mock
async def test_cluster_health_backcompat_shim(connector):
    """Older callers using cluster_health() should still work."""
    respx.get(f"{HOST}/_cluster/health").mock(
        return_value=httpx.Response(200, json={"status": "green"})
    )
    result = await connector.cluster_health()
    assert result["status"] == "green"


# ═══════════════════════════════════════════════════════════════════════════
# Indices: list / get / create / delete
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_indices_returns_array(connector):
    payload = [
        {"index": "products", "health": "green", "docs.count": "42"},
        {"index": "users", "health": "yellow", "docs.count": "10"},
    ]
    route = respx.get(f"{HOST}/_cat/indices/*").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_indices()
    assert route.called
    assert result == payload
    assert route.calls.last.request.url.params["format"] == "json"


@pytest.mark.asyncio
@respx.mock
async def test_get_index_returns_full_info(connector):
    payload = {
        "products": {
            "settings": {"index": {"number_of_shards": "1"}},
            "mappings": {"properties": {"name": {"type": "text"}}},
            "aliases": {},
        }
    }
    route = respx.get(f"{HOST}/products").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.get_index("products")
    assert route.called
    assert result["products"]["settings"]["index"]["number_of_shards"] == "1"


@pytest.mark.asyncio
@respx.mock
async def test_create_index_with_settings_and_mappings(connector):
    payload = {"acknowledged": True, "shards_acknowledged": True, "index": "products"}
    route = respx.put(f"{HOST}/products").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.create_index(
        index="products",
        settings={"number_of_shards": 1, "number_of_replicas": 1},
        mappings={"properties": {"name": {"type": "text"}}},
    )
    assert route.called
    assert result["acknowledged"] is True
    body = route.calls.last.request.content
    assert b"number_of_shards" in body
    assert b"properties" in body


@pytest.mark.asyncio
@respx.mock
async def test_delete_index(connector):
    route = respx.delete(f"{HOST}/products").mock(
        return_value=httpx.Response(200, json={"acknowledged": True})
    )
    result = await connector.delete_index("products")
    assert route.called
    assert result["acknowledged"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Documents: index_document / get / update / delete
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_index_document_auto_id_posts(connector):
    payload = {"_index": "products", "_id": "auto-1", "_version": 1, "result": "created"}
    route = respx.post(f"{HOST}/products/_doc").mock(
        return_value=httpx.Response(201, json=payload)
    )
    result = await connector.index_document("products", {"name": "Shoe"})
    assert route.called
    assert result["result"] == "created"
    assert route.calls.last.request.url.params["refresh"] == "false"


@pytest.mark.asyncio
@respx.mock
async def test_index_document_with_id_puts(connector):
    payload = {"_index": "products", "_id": "sku-42", "_version": 1, "result": "created"}
    route = respx.put(f"{HOST}/products/_doc/sku-42").mock(
        return_value=httpx.Response(201, json=payload)
    )
    result = await connector.index_document(
        index="products",
        document={"name": "Shoe"},
        doc_id="sku-42",
        refresh="wait_for",
    )
    assert route.called
    assert result["_id"] == "sku-42"
    assert route.calls.last.request.url.params["refresh"] == "wait_for"


@pytest.mark.asyncio
@respx.mock
async def test_get_document(connector):
    payload = {
        "_index": "products",
        "_id": "sku-42",
        "_version": 1,
        "found": True,
        "_source": {"name": "Shoe"},
    }
    route = respx.get(f"{HOST}/products/_doc/sku-42").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.get_document("products", "sku-42")
    assert route.called
    assert result["_source"]["name"] == "Shoe"


@pytest.mark.asyncio
@respx.mock
async def test_update_document_with_doc_as_upsert(connector):
    payload = {"_index": "products", "_id": "sku-42", "result": "updated"}
    route = respx.post(f"{HOST}/products/_update/sku-42").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.update_document(
        index="products",
        doc_id="sku-42",
        doc={"price": 99},
        doc_as_upsert=True,
    )
    assert route.called
    assert result["result"] == "updated"
    body = route.calls.last.request.content
    assert b"doc_as_upsert" in body


@pytest.mark.asyncio
@respx.mock
async def test_delete_document(connector):
    route = respx.delete(f"{HOST}/products/_doc/sku-42").mock(
        return_value=httpx.Response(200, json={"_index": "products", "result": "deleted"})
    )
    result = await connector.delete_document("products", "sku-42")
    assert route.called
    assert result["result"] == "deleted"


# ═══════════════════════════════════════════════════════════════════════════
# Search / Count
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_search_with_query_sort_and_aggs(connector):
    payload = {
        "took": 5,
        "timed_out": False,
        "hits": {"total": {"value": 1}, "hits": [{"_id": "1", "_source": {"name": "Shoe"}}]},
        "aggregations": {"by_brand": {"buckets": []}},
    }
    route = respx.post(f"{HOST}/products/_search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.search(
        index="products",
        query={"match": {"name": "shoe"}},
        size=20,
        from_=10,
        sort=[{"name": "asc"}],
        aggs={"by_brand": {"terms": {"field": "brand"}}},
    )
    assert route.called
    assert result["hits"]["total"]["value"] == 1
    body = route.calls.last.request.content
    assert b'"size":20' in body
    assert b'"from":10' in body
    assert b'"sort"' in body
    assert b'"aggs"' in body


@pytest.mark.asyncio
@respx.mock
async def test_count_with_query(connector):
    route = respx.post(f"{HOST}/products/_count").mock(
        return_value=httpx.Response(200, json={"count": 42})
    )
    result = await connector.count("products", query={"match_all": {}})
    assert route.called
    assert result["count"] == 42


# ═══════════════════════════════════════════════════════════════════════════
# Bulk — NDJSON body format
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_bulk_sends_ndjson_body(connector):
    payload = {
        "took": 7,
        "errors": False,
        "items": [
            {"index": {"_index": "products", "_id": "1", "status": 201}},
            {"index": {"_index": "products", "_id": "2", "status": 201}},
        ],
    }
    route = respx.post(f"{HOST}/_bulk").mock(
        return_value=httpx.Response(200, json=payload)
    )
    operations = [
        {"index": {"_index": "products", "_id": "1"}},
        {"name": "Shoe"},
        {"index": {"_index": "products", "_id": "2"}},
        {"name": "Hat"},
    ]
    result = await connector.bulk(operations)
    assert route.called
    assert result["errors"] is False
    req = route.calls.last.request
    assert req.headers["Content-Type"] == "application/x-ndjson"
    body = req.content
    assert body == serialize_ndjson(operations)
    assert body.count(b"\n") == 4
    assert body.endswith(b"\n")


# ═══════════════════════════════════════════════════════════════════════════
# Mapping
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_mapping(connector):
    payload = {"products": {"mappings": {"properties": {"name": {"type": "text"}}}}}
    route = respx.get(f"{HOST}/products/_mapping").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.get_mapping("products")
    assert route.called
    assert "products" in result


@pytest.mark.asyncio
@respx.mock
async def test_put_mapping(connector):
    route = respx.put(f"{HOST}/products/_mapping").mock(
        return_value=httpx.Response(200, json={"acknowledged": True})
    )
    result = await connector.put_mapping(
        "products", {"price": {"type": "double"}}
    )
    assert route.called
    assert result["acknowledged"] is True
    body = route.calls.last.request.content
    assert b"properties" in body
    assert b"double" in body


# ═══════════════════════════════════════════════════════════════════════════
# Aliases + Snapshots
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_aliases_all(connector):
    payload = [
        {"alias": "current", "index": "products-v3", "filter": "-", "routing.index": "-"},
    ]
    route = respx.get(f"{HOST}/_cat/aliases").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_aliases()
    assert route.called
    assert result == payload
    assert route.calls.last.request.url.params["format"] == "json"


@pytest.mark.asyncio
@respx.mock
async def test_list_snapshots(connector):
    payload = {"snapshots": [{"snapshot": "s1", "state": "SUCCESS"}]}
    route = respx.get(f"{HOST}/_snapshot/backups/_all").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_snapshots(repository="backups")
    assert route.called
    assert result["snapshots"][0]["snapshot"] == "s1"


# ═══════════════════════════════════════════════════════════════════════════
# Sync — produces NormalizedDocuments for each index
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_sync_ingests_one_doc_per_index(connector):
    rows = [
        {"index": "products", "health": "green", "status": "open",
         "uuid": "u1", "pri": "1", "rep": "1",
         "docs.count": "42", "docs.deleted": "0", "store.size": "12.3kb"},
        {"index": "users", "health": "yellow", "status": "open",
         "uuid": "u2", "pri": "1", "rep": "0",
         "docs.count": "10", "docs.deleted": "0", "store.size": "4kb"},
    ]
    respx.get(f"{HOST}/_cat/indices/*").mock(
        return_value=httpx.Response(200, json=rows)
    )
    result = await connector.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0
    # ingest_document was called twice via the AsyncMock storage patch.
    assert connector.ingest_document.await_count == 2  # type: ignore[attr-defined]
    # First call's first positional arg is the NormalizedDocument.
    first_call = connector.ingest_document.await_args_list[0]  # type: ignore[attr-defined]
    doc = first_call.args[0]
    assert doc.source_id == "products"
    assert doc.id == f"{TENANT_ID}_products"
    assert doc.metadata["kind"] == "elasticsearch.index"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 + 404 + auth-header shape
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_eventually_succeeds(connector):
    payload_ok = {"count": 0}
    route = respx.post(f"{HOST}/products/_count").mock(
        side_effect=[
            httpx.Response(429, json={"error": {"reason": "rate_limited"}}),
            httpx.Response(429, json={"error": {"reason": "rate_limited"}}),
            httpx.Response(200, json=payload_ok),
        ]
    )
    result = await connector.count("products")
    assert route.call_count == 3
    assert result == payload_ok


@pytest.mark.asyncio
@respx.mock
async def test_404_raises_not_found(connector):
    respx.get(f"{HOST}/missing/_doc/x").mock(
        return_value=httpx.Response(
            404, json={"error": {"reason": "index_not_found"}}
        )
    )
    with pytest.raises(ElasticsearchNotFound):
        await connector.get_document("missing", "x")


@pytest.mark.asyncio
@respx.mock
async def test_auth_header_is_apikey_prefix_no_bearer(connector):
    """API key flows as `Authorization: ApiKey <key>` — never `Bearer`."""
    route = respx.get(f"{HOST}/_cluster/health").mock(
        return_value=httpx.Response(200, json={"status": "green"})
    )
    await connector.get_cluster_health()
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"ApiKey {TEST_API_KEY}"
    assert not sent_auth.lower().startswith("bearer ")
