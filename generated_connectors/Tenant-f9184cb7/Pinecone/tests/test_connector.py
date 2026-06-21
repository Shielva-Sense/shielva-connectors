"""Unit tests for PineconeConnector — respx-mocked, zero real network I/O.

Coverage maps directly to implementation_plan.md Section 5:
  control plane → list/describe/create/delete/configure indexes + collection CRUD
  data plane    → upsert / query / fetch / update / delete vectors, stats, namespaces
  lifecycle     → install, authorize, health_check, sync
  resilience    → 429 retry, 5xx retry, 404 surfacing, host caching
"""
import json
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import PineconeConnector
from exceptions import (
    PineconeAuthError,
    PineconeBadRequestError,
    PineconeConflictError,
    PineconeError,
    PineconeNotFoundError,
    PineconeRateLimitError,
)

from tests.conftest import (
    CONNECTOR_ID,
    CONTROL,
    SAMPLE_INDEX_SPEC,
    SAMPLE_QUERY_RESPONSE,
    SAMPLE_STATS,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
    TEST_ENVIRONMENT,
    TEST_HOST,
    TEST_INDEX,
)


# ════════════════════════════════════════════════════════════════════════════
# 1. install()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ════════════════════════════════════════════════════════════════════════════
# 2. authorize() — synthetic TokenInfo
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_synthetic_token(connector):
    token_info = await connector.authorize(auth_code="", state="")
    assert token_info.access_token == TEST_API_KEY
    assert token_info.token_type == "ApiKey"
    assert token_info.refresh_token is None
    assert token_info.expires_at is None


# ════════════════════════════════════════════════════════════════════════════
# 3. Auth header shape (Api-Key NOT Authorization)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_api_key_header_is_used_not_authorization(connector):
    """Pinecone expects the key in `Api-Key`, never `Authorization`."""
    route = respx.get(f"{CONTROL}/indexes").mock(
        return_value=httpx.Response(200, json={"indexes": []})
    )
    await connector.list_indexes()
    assert route.called
    sent = route.calls[0].request
    assert sent.headers.get("Api-Key") == TEST_API_KEY
    # Critical: Authorization header MUST NOT carry the key.
    auth_header = sent.headers.get("Authorization") or ""
    assert TEST_API_KEY not in auth_header
    assert sent.headers.get("X-Pinecone-API-Version") == "2025-01"


# ════════════════════════════════════════════════════════════════════════════
# 4. health_check() — auth error + happy path
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error(connector):
    respx.get(f"{CONTROL}/indexes").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{CONTROL}/indexes").mock(
        return_value=httpx.Response(200, json={"indexes": []})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


# ════════════════════════════════════════════════════════════════════════════
# 5. list_indexes()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_indexes(connector):
    route = respx.get(f"{CONTROL}/indexes").mock(
        return_value=httpx.Response(
            200, json={"indexes": [{"name": "idx1"}, {"name": "idx2"}]}
        )
    )
    result = await connector.list_indexes()
    assert route.called
    assert len(result["indexes"]) == 2


# ════════════════════════════════════════════════════════════════════════════
# 6. describe_index() — host caching
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_describe_index_caches_host(connector):
    """describe_index should populate the host cache so subsequent
    data-plane calls hit the cached host without a second describe call."""
    desc_route = respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    upsert_route = respx.post(f"{TEST_HOST}/vectors/upsert").mock(
        return_value=httpx.Response(200, json={"upsertedCount": 1})
    )

    spec = await connector.describe_index(TEST_INDEX)
    assert spec["name"] == TEST_INDEX
    assert connector.http_client.get_cached_host(TEST_INDEX) == TEST_HOST

    # Now upsert without another describe call
    result = await connector.upsert_vectors(
        TEST_INDEX,
        vectors=[{"id": "v1", "values": [0.1] * 1536}],
    )
    assert result["upsertedCount"] == 1
    assert desc_route.call_count == 1
    assert upsert_route.called


# ════════════════════════════════════════════════════════════════════════════
# 7. create_index()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_create_index(connector):
    route = respx.post(f"{CONTROL}/indexes").mock(
        return_value=httpx.Response(
            201,
            json={"name": "new-idx", "dimension": 768, "metric": "cosine"},
        )
    )
    result = await connector.create_index(
        name="new-idx", dimension=768, metric="cosine"
    )
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["name"] == "new-idx"
    assert body["dimension"] == 768
    assert body["metric"] == "cosine"
    assert body["spec"]["serverless"]["cloud"] == "aws"
    assert result["dimension"] == 768


# ════════════════════════════════════════════════════════════════════════════
# 8. configure_index() — pod resize
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_configure_index_requires_at_least_one_arg(connector):
    with pytest.raises(PineconeBadRequestError):
        await connector.configure_index(TEST_INDEX)


@pytest.mark.asyncio
@respx.mock
async def test_configure_index_patches_spec_pod(connector):
    route = respx.patch(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json={"name": TEST_INDEX})
    )
    await connector.configure_index(TEST_INDEX, replicas=4, pod_type="p1.x1")
    body = json.loads(route.calls[0].request.content)
    assert body == {"spec": {"pod": {"replicas": 4, "pod_type": "p1.x1"}}}


# ════════════════════════════════════════════════════════════════════════════
# 9. upsert_vectors() — implicit describe + metadata-dropping
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_upsert_vectors_resolves_host_then_upserts(connector):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    upsert_route = respx.post(f"{TEST_HOST}/vectors/upsert").mock(
        return_value=httpx.Response(200, json={"upsertedCount": 2})
    )
    result = await connector.upsert_vectors(
        TEST_INDEX,
        vectors=[
            {"id": "a", "values": [0.1, 0.2], "metadata": {"k": "v"}},
            {"id": "b", "values": [0.3, 0.4]},
        ],
        namespace="ns1",
    )
    assert result["upsertedCount"] == 2
    body = json.loads(upsert_route.calls[0].request.content)
    assert body["namespace"] == "ns1"
    assert len(body["vectors"]) == 2
    assert body["vectors"][0]["id"] == "a"
    assert body["vectors"][0]["metadata"] == {"k": "v"}
    # 2nd vector dropped metadata key (None)
    assert "metadata" not in body["vectors"][1]


# ════════════════════════════════════════════════════════════════════════════
# 10. query()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_query(connector):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    route = respx.post(f"{TEST_HOST}/query").mock(
        return_value=httpx.Response(200, json=SAMPLE_QUERY_RESPONSE)
    )
    result = await connector.query(
        TEST_INDEX,
        vector=[0.1] * 1536,
        top_k=2,
        include_metadata=True,
    )
    assert len(result["matches"]) == 2
    assert result["matches"][0]["id"] == "v1"
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["topK"] == 2
    assert body["includeMetadata"] is True
    assert body["includeValues"] is False


# ════════════════════════════════════════════════════════════════════════════
# 11. fetch_vectors()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_fetch_vectors(connector):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    route = respx.get(f"{TEST_HOST}/vectors/fetch").mock(
        return_value=httpx.Response(
            200,
            json={
                "vectors": {
                    "v1": {"id": "v1", "values": [0.1, 0.2]},
                    "v2": {"id": "v2", "values": [0.3, 0.4]},
                }
            },
        )
    )
    result = await connector.fetch_vectors(TEST_INDEX, ids=["v1", "v2"])
    assert route.called
    assert "v1" in result["vectors"]
    assert "v2" in result["vectors"]


# ════════════════════════════════════════════════════════════════════════════
# 12. delete_vectors()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_delete_vectors_by_ids(connector):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    route = respx.post(f"{TEST_HOST}/vectors/delete").mock(
        return_value=httpx.Response(200, json={})
    )
    await connector.delete_vectors(TEST_INDEX, ids=["v1", "v2"])
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["ids"] == ["v1", "v2"]
    assert "deleteAll" not in body


@pytest.mark.asyncio
@respx.mock
async def test_delete_vectors_delete_all(connector):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    route = respx.post(f"{TEST_HOST}/vectors/delete").mock(
        return_value=httpx.Response(200, json={})
    )
    await connector.delete_vectors(TEST_INDEX, delete_all=True, namespace="ns")
    body = json.loads(route.calls[0].request.content)
    assert body["deleteAll"] is True
    assert body["namespace"] == "ns"


# ════════════════════════════════════════════════════════════════════════════
# 13. update_vector()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_update_vector(connector):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    route = respx.post(f"{TEST_HOST}/vectors/update").mock(
        return_value=httpx.Response(200, json={})
    )
    await connector.update_vector(
        TEST_INDEX,
        id="v1",
        values=[0.5, 0.6],
        metadata={"updated": True},
    )
    body = json.loads(route.calls[0].request.content)
    assert body["id"] == "v1"
    assert body["values"] == [0.5, 0.6]
    assert body["setMetadata"] == {"updated": True}


# ════════════════════════════════════════════════════════════════════════════
# 14. describe_index_stats() + list_namespaces()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_describe_index_stats(connector):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    respx.post(f"{TEST_HOST}/describe_index_stats").mock(
        return_value=httpx.Response(200, json=SAMPLE_STATS)
    )
    stats = await connector.describe_index_stats(TEST_INDEX)
    assert stats["totalVectorCount"] == 42
    assert stats["dimension"] == 1536


@pytest.mark.asyncio
@respx.mock
async def test_list_namespaces_happy_path(connector):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    respx.get(f"{TEST_HOST}/namespaces").mock(
        return_value=httpx.Response(
            200,
            json={"namespaces": [{"name": "tenants", "vectorCount": 12}]},
        )
    )
    result = await connector.list_namespaces(TEST_INDEX)
    assert result["namespaces"][0]["name"] == "tenants"


@pytest.mark.asyncio
@respx.mock
async def test_list_namespaces_falls_back_on_404(connector):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    respx.get(f"{TEST_HOST}/namespaces").mock(
        return_value=httpx.Response(404, json={"message": "not supported"})
    )
    respx.post(f"{TEST_HOST}/describe_index_stats").mock(
        return_value=httpx.Response(200, json=SAMPLE_STATS)
    )
    result = await connector.list_namespaces(TEST_INDEX)
    names = sorted(ns["name"] for ns in result["namespaces"])
    assert names == ["", "tenants"]


# ════════════════════════════════════════════════════════════════════════════
# 15. list_collections / create_collection / delete_collection
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_collections(connector):
    respx.get(f"{CONTROL}/collections").mock(
        return_value=httpx.Response(200, json={"collections": []})
    )
    result = await connector.list_collections()
    assert result == {"collections": []}


@pytest.mark.asyncio
@respx.mock
async def test_create_collection(connector):
    route = respx.post(f"{CONTROL}/collections").mock(
        return_value=httpx.Response(201, json={"name": "snap", "source": TEST_INDEX})
    )
    await connector.create_collection(name="snap", source=TEST_INDEX)
    body = json.loads(route.calls[0].request.content)
    assert body == {"name": "snap", "source": TEST_INDEX}


@pytest.mark.asyncio
@respx.mock
async def test_delete_collection(connector):
    route = respx.delete(f"{CONTROL}/collections/snap").mock(
        return_value=httpx.Response(204, content=b"")
    )
    result = await connector.delete_collection("snap")
    assert route.called
    assert result == {}


# ════════════════════════════════════════════════════════════════════════════
# 16. delete_index — clears cache
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_delete_index_clears_host_cache(connector):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    respx.delete(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(204, content=b"")
    )

    await connector.describe_index(TEST_INDEX)
    assert connector.http_client.get_cached_host(TEST_INDEX) == TEST_HOST

    await connector.delete_index(TEST_INDEX)
    assert connector.http_client.get_cached_host(TEST_INDEX) is None


# ════════════════════════════════════════════════════════════════════════════
# 17. retry-on-429 — exponential backoff converges
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429(connector, no_retry_sleep):
    responses = [
        httpx.Response(429, json={"message": "rate limited"}),
        httpx.Response(200, json={"indexes": [{"name": "ok"}]}),
    ]
    route = respx.get(f"{CONTROL}/indexes").mock(side_effect=responses)
    result = await connector.list_indexes()
    assert result["indexes"][0]["name"] == "ok"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500(connector, no_retry_sleep):
    responses = [
        httpx.Response(500, json={"message": "boom"}),
        httpx.Response(200, json={"indexes": []}),
    ]
    route = respx.get(f"{CONTROL}/indexes").mock(side_effect=responses)
    result = await connector.list_indexes()
    assert route.call_count == 2
    assert result == {"indexes": []}


# ════════════════════════════════════════════════════════════════════════════
# 18. 404 surfaces typed exception
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_describe_index_not_found(connector):
    respx.get(f"{CONTROL}/indexes/missing").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    with pytest.raises(PineconeNotFoundError):
        await connector.describe_index("missing")


@pytest.mark.asyncio
@respx.mock
async def test_create_index_409_conflict(connector):
    respx.post(f"{CONTROL}/indexes").mock(
        return_value=httpx.Response(409, json={"message": "already exists"})
    )
    with pytest.raises(PineconeConflictError):
        await connector.create_index(name="dupe", dimension=128)


# ════════════════════════════════════════════════════════════════════════════
# 19. sync()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_sync_with_default_index(connector_with_default_index):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    respx.post(f"{TEST_HOST}/describe_index_stats").mock(
        return_value=httpx.Response(200, json=SAMPLE_STATS)
    )
    result = await connector_with_default_index.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1


@pytest.mark.asyncio
@respx.mock
async def test_sync_discovers_indexes(connector):
    respx.get(f"{CONTROL}/indexes").mock(
        return_value=httpx.Response(200, json={"indexes": [{"name": TEST_INDEX}]})
    )
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    respx.post(f"{TEST_HOST}/describe_index_stats").mock(
        return_value=httpx.Response(200, json=SAMPLE_STATS)
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1


# ════════════════════════════════════════════════════════════════════════════
# 20. Connector identity & multi-tenant isolation
# ════════════════════════════════════════════════════════════════════════════


def test_connector_type():
    assert PineconeConnector.CONNECTOR_TYPE == "pinecone"


def test_auth_type():
    assert PineconeConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert "api_key" in PineconeConnector.REQUIRED_CONFIG_KEYS
    assert "environment" in PineconeConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    """OCP — _STATUS_MAP keys must cover the auth/rate-limit triplet."""
    assert 401 in PineconeConnector._STATUS_MAP
    assert 403 in PineconeConnector._STATUS_MAP
    assert 429 in PineconeConnector._STATUS_MAP


def test_independent_instances_per_tenant():
    c1 = PineconeConnector(tenant_id="tA", connector_id="c1", config=dict(TEST_CONFIG))
    c2 = PineconeConnector(tenant_id="tB", connector_id="c2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    # HTTP client instances must be distinct (no shared state)
    assert c1.http_client is not c2.http_client


# ════════════════════════════════════════════════════════════════════════════
# 21. _resolve_index — error path
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_query_raises_without_index_or_default(connector):
    with pytest.raises(PineconeError):
        await connector.query(index_name="", vector=[0.1, 0.2])


# ════════════════════════════════════════════════════════════════════════════
# 22. NormalizedDocument shape from sync
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_sync_ingests_normalized_document(connector_with_default_index, mocker):
    respx.get(f"{CONTROL}/indexes/{TEST_INDEX}").mock(
        return_value=httpx.Response(200, json=SAMPLE_INDEX_SPEC)
    )
    respx.post(f"{TEST_HOST}/describe_index_stats").mock(
        return_value=httpx.Response(200, json=SAMPLE_STATS)
    )
    captured = []

    async def _capture(doc, *_, **__):
        captured.append(doc)

    mocker.patch.object(
        PineconeConnector, "ingest_document", side_effect=_capture
    )

    await connector_with_default_index.sync()
    assert len(captured) == 1
    doc = captured[0]
    # NormalizedDocument.id is tenant-scoped per CONNECTOR_SYSTEM_PROMPT
    assert doc.id == f"{TENANT_ID}_{TEST_INDEX}"
    assert doc.metadata["kind"] == "pinecone.index"
    assert doc.metadata["dimension"] == 1536
    assert doc.metadata["total_vector_count"] == 42
