"""Unit tests for QdrantConnector — respx-mocked, zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import QdrantConnector
from exceptions import QdrantAuthError, QdrantNotFound

from tests.conftest import (
    CONNECTOR_ID,
    QDRANT_BASE,
    TENANT_ID,
    TEST_API_KEY,
    TEST_COLLECTION,
    TEST_CONFIG,
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
async def test_install_allows_blank_api_key(connector):
    """Default self-hosted Qdrant has no auth — install must accept an empty key."""
    connector.config["api_key"] = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.AUTHENTICATED
    assert result.health == ConnectorHealth.HEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Header shape: api-key (lowercase, no Bearer)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_uses_api_key_lowercase(connector):
    """Qdrant expects the api-key header (lowercase, NO 'Authorization', NO 'Bearer')."""
    route = respx.get(f"{QDRANT_BASE}/collections").mock(
        return_value=httpx.Response(200, json={"result": {"collections": []}, "status": "ok"})
    )
    await connector.list_collections()
    assert route.called
    sent = route.calls[0].request.headers
    # api-key sent verbatim, no Authorization, no Bearer prefix.
    assert sent.get("api-key") == TEST_API_KEY
    assert sent.get("authorization") in (None, "")


@respx.mock
@pytest.mark.asyncio
async def test_api_key_header_omitted_when_blank(connector):
    """Self-hosted no-auth path: blank api_key -> no api-key header at all."""
    connector.api_key = ""
    connector.http_client._api_key = ""
    route = respx.get(f"{QDRANT_BASE}/collections").mock(
        return_value=httpx.Response(200, json={"result": {"collections": []}, "status": "ok"})
    )
    await connector.list_collections()
    sent = route.calls[0].request.headers
    assert "api-key" not in {k.lower() for k in sent.keys()}


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_qdrant_auth_error(connector):
    respx.get(f"{QDRANT_BASE}/collections").mock(
        return_value=httpx.Response(401, json={"status": {"error": "Invalid api-key"}})
    )
    with pytest.raises(QdrantAuthError):
        await connector.list_collections()


# ═══════════════════════════════════════════════════════════════════════════
# health_check
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{QDRANT_BASE}/healthz").mock(
        return_value=httpx.Response(200, text="healthz check passed")
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{QDRANT_BASE}/healthz").mock(
        return_value=httpx.Response(401, json={"status": {"error": "Invalid api-key"}})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_falls_back_to_root_on_404(connector):
    """Older self-hosted Qdrant lacks /healthz; connector must probe / as a fallback."""
    respx.get(f"{QDRANT_BASE}/healthz").mock(
        return_value=httpx.Response(404, json={"status": {"error": "not found"}})
    )
    respx.get(f"{QDRANT_BASE}/").mock(
        return_value=httpx.Response(200, json={"title": "qdrant - vector search engine", "version": "1.11.0"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Collections
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_collections_success(connector):
    payload = {
        "result": {"collections": [{"name": "shielva-kb"}, {"name": "embeddings"}]},
        "status": "ok",
        "time": 0.001,
    }
    route = respx.get(f"{QDRANT_BASE}/collections").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_collections()
    assert route.called
    assert len(result["result"]["collections"]) == 2
    assert result["status"] == "ok"


@respx.mock
@pytest.mark.asyncio
async def test_get_collection_success(connector):
    name = "shielva-kb"
    respx.get(f"{QDRANT_BASE}/collections/{name}").mock(
        return_value=httpx.Response(
            200,
            json={"result": {"status": "green", "points_count": 5, "name": name}, "status": "ok"},
        )
    )
    result = await connector.get_collection(name)
    assert result["result"]["status"] == "green"
    assert result["result"]["points_count"] == 5


@respx.mock
@pytest.mark.asyncio
async def test_get_collection_not_found(connector):
    respx.get(f"{QDRANT_BASE}/collections/missing").mock(
        return_value=httpx.Response(
            404, json={"status": {"error": "Collection 'missing' doesn't exist"}}
        )
    )
    with pytest.raises(QdrantNotFound):
        await connector.get_collection("missing")


@respx.mock
@pytest.mark.asyncio
async def test_create_collection_posts_vectors_config(connector):
    name = "shielva-kb"
    route = respx.put(f"{QDRANT_BASE}/collections/{name}").mock(
        return_value=httpx.Response(200, json={"result": True, "status": "ok"})
    )
    vectors = {"size": 768, "distance": "Cosine"}
    result = await connector.create_collection(
        collection_name=name,
        vectors=vectors,
        on_disk_payload=True,
        shard_number=2,
    )
    assert route.called
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["vectors"] == vectors
    assert body["on_disk_payload"] is True
    assert body["shard_number"] == 2
    assert body["replication_factor"] == 1
    assert result["result"] is True


@respx.mock
@pytest.mark.asyncio
async def test_delete_collection_success(connector):
    respx.delete(f"{QDRANT_BASE}/collections/shielva-kb").mock(
        return_value=httpx.Response(200, json={"result": True, "status": "ok"})
    )
    result = await connector.delete_collection("shielva-kb")
    assert result["result"] is True


@respx.mock
@pytest.mark.asyncio
async def test_update_collection_only_emits_provided_keys(connector):
    name = "shielva-kb"
    route = respx.patch(f"{QDRANT_BASE}/collections/{name}").mock(
        return_value=httpx.Response(200, json={"result": True, "status": "ok"})
    )
    await connector.update_collection(
        collection_name=name,
        optimizers_config={"deleted_threshold": 0.2},
    )
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == {"optimizers_config": {"deleted_threshold": 0.2}}
    assert "vectors" not in body
    assert "params" not in body


# ═══════════════════════════════════════════════════════════════════════════
# Points
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_upsert_points_posts_points_array_with_wait(connector):
    name = "shielva-kb"
    route = respx.put(f"{QDRANT_BASE}/collections/{name}/points").mock(
        return_value=httpx.Response(
            200, json={"result": {"operation_id": 42, "status": "completed"}, "status": "ok"}
        )
    )
    points = [
        {"id": 1, "vector": [0.1, 0.2, 0.3], "payload": {"doc": "a"}},
        {"id": 2, "vector": [0.4, 0.5, 0.6], "payload": {"doc": "b"}},
    ]
    result = await connector.upsert_points(name, points=points)
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["points"] == points
    assert "wait=true" in str(route.calls[0].request.url)
    assert result["status"] == "ok"


@respx.mock
@pytest.mark.asyncio
async def test_delete_points_by_ids(connector):
    name = "shielva-kb"
    route = respx.post(f"{QDRANT_BASE}/collections/{name}/points/delete").mock(
        return_value=httpx.Response(200, json={"result": {"status": "ok"}, "status": "ok"})
    )
    await connector.delete_points(name, points=[1, 2, 3])
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["points"] == [1, 2, 3]
    assert "filter" not in body


@respx.mock
@pytest.mark.asyncio
async def test_delete_points_by_filter(connector):
    name = "shielva-kb"
    route = respx.post(f"{QDRANT_BASE}/collections/{name}/points/delete").mock(
        return_value=httpx.Response(200, json={"result": {"status": "ok"}, "status": "ok"})
    )
    flt = {"must": [{"key": "tenant_id", "match": {"value": "t-A"}}]}
    await connector.delete_points(name, filter=flt)
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["filter"] == flt
    assert "points" not in body


@respx.mock
@pytest.mark.asyncio
async def test_get_points_retrieves_by_ids(connector):
    name = "shielva-kb"
    respx.post(f"{QDRANT_BASE}/collections/{name}/points").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": [
                    {"id": 1, "payload": {"doc": "a"}},
                    {"id": 2, "payload": {"doc": "b"}},
                ],
                "status": "ok",
            },
        )
    )
    result = await connector.get_points(name, ids=[1, 2])
    assert len(result["result"]) == 2
    assert result["result"][0]["payload"]["doc"] == "a"


@respx.mock
@pytest.mark.asyncio
async def test_search_points_sends_vector_filter_threshold(connector):
    name = "shielva-kb"
    route = respx.post(f"{QDRANT_BASE}/collections/{name}/points/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": [{"id": 1, "score": 0.99, "payload": {"doc": "a"}}],
                "status": "ok",
            },
        )
    )
    flt = {"must": [{"key": "tenant_id", "match": {"value": "t-1"}}]}
    result = await connector.search_points(
        collection_name=name,
        vector=[0.1, 0.2, 0.3],
        limit=5,
        score_threshold=0.8,
        filter=flt,
    )
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["vector"] == [0.1, 0.2, 0.3]
    assert body["limit"] == 5
    assert body["score_threshold"] == 0.8
    assert body["filter"] == flt
    assert result["status"] == "ok"


@respx.mock
@pytest.mark.asyncio
async def test_scroll_points_returns_next_page_offset(connector):
    name = "shielva-kb"
    respx.post(f"{QDRANT_BASE}/collections/{name}/points/scroll").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "points": [{"id": 1, "payload": {"doc": "a"}}],
                    "next_page_offset": "3",
                },
                "status": "ok",
            },
        )
    )
    result = await connector.scroll_points(name, limit=2)
    assert result["result"]["next_page_offset"] == "3"


@respx.mock
@pytest.mark.asyncio
async def test_count_points_returns_total(connector):
    name = "shielva-kb"
    route = respx.post(f"{QDRANT_BASE}/collections/{name}/points/count").mock(
        return_value=httpx.Response(
            200, json={"result": {"count": 42}, "status": "ok"}
        )
    )
    result = await connector.count_points(name, exact=True)
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["exact"] is True
    assert result["result"]["count"] == 42


# ═══════════════════════════════════════════════════════════════════════════
# Payload indexes
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_create_payload_index_sends_field_schema(connector):
    name = "shielva-kb"
    route = respx.put(f"{QDRANT_BASE}/collections/{name}/index").mock(
        return_value=httpx.Response(
            200, json={"result": {"operation_id": 7, "status": "completed"}, "status": "ok"}
        )
    )
    await connector.create_payload_index(
        collection_name=name,
        field_name="tenant_id",
        field_schema="keyword",
    )
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["field_name"] == "tenant_id"
    assert body["field_schema"] == "keyword"


@respx.mock
@pytest.mark.asyncio
async def test_delete_payload_index_targets_field(connector):
    name = "shielva-kb"
    route = respx.delete(f"{QDRANT_BASE}/collections/{name}/index/tenant_id").mock(
        return_value=httpx.Response(200, json={"result": True, "status": "ok"})
    )
    await connector.delete_payload_index(name, field_name="tenant_id")
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Snapshots
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_snapshots_success(connector):
    name = "shielva-kb"
    respx.get(f"{QDRANT_BASE}/collections/{name}/snapshots").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": [{"name": "snap-1.tar", "creation_time": "2026-01-01T00:00:00"}],
                "status": "ok",
            },
        )
    )
    result = await connector.list_snapshots(name)
    assert len(result["result"]) == 1


@respx.mock
@pytest.mark.asyncio
async def test_create_snapshot_success(connector):
    name = "shielva-kb"
    respx.post(f"{QDRANT_BASE}/collections/{name}/snapshots").mock(
        return_value=httpx.Response(
            200,
            json={"result": {"name": "snap-1.tar", "size": 1024}, "status": "ok"},
        )
    )
    result = await connector.create_snapshot(name)
    assert result["result"]["name"] == "snap-1.tar"


# ═══════════════════════════════════════════════════════════════════════════
# Cluster
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_cluster_info(connector):
    respx.get(f"{QDRANT_BASE}/cluster").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "status": "enabled",
                    "peer_id": 1,
                    "peers": {},
                    "raft_info": {},
                },
                "status": "ok",
            },
        )
    )
    result = await connector.get_cluster_info()
    assert result["result"]["status"] == "enabled"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{QDRANT_BASE}/collections").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}, json={"status": {"error": "rate limited"}}),
            httpx.Response(200, json={"result": {"collections": [{"name": "after-retry"}]}, "status": "ok"}),
        ]
    )
    result = await connector.list_collections()
    assert route.call_count == 2
    assert result["result"]["collections"][0]["name"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{QDRANT_BASE}/collections").mock(
        side_effect=[
            httpx.Response(500, json={"status": {"error": "boom"}}),
            httpx.Response(200, json={"result": {"collections": []}, "status": "ok"}),
        ]
    )
    result = await connector.list_collections()
    assert route.call_count == 2
    assert result["status"] == "ok"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_is_not_retried(connector, no_retry_sleep):
    """401 is terminal — input won't change, must not loop."""
    route = respx.post(f"{QDRANT_BASE}/collections/shielva-kb/points/search").mock(
        return_value=httpx.Response(401, json={"status": {"error": "Invalid api-key"}})
    )
    with pytest.raises(QdrantAuthError):
        await connector.search_points("shielva-kb", vector=[0.1, 0.2])
    assert route.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity (gateway contract)
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert QdrantConnector.CONNECTOR_TYPE == "qdrant"


def test_auth_type_class_attr():
    assert QdrantConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(QdrantConnector, "REQUIRED_CONFIG_KEYS")
    assert "base_url" in QdrantConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = QdrantConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = QdrantConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
