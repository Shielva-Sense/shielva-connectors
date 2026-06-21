"""Unit tests for CohereConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import CohereConnector
from exceptions import (
    CohereAuthError,
    CohereBadRequestError,
    CohereError,
    CohereNotFound,
    CohereRateLimitError,
)

from tests.conftest import (
    COHERE_BASE,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_install_success(connector):
    respx.get(f"{COHERE_BASE}/v1/models").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "command-r-plus"}]})
    )
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


@respx.mock
@pytest.mark.asyncio
async def test_install_invalid_key_401(connector):
    respx.get(f"{COHERE_BASE}/v1/models").mock(
        return_value=httpx.Response(401, json={"message": "invalid api token"})
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_install_server_error_degrades(connector, no_retry_sleep):
    respx.get(f"{COHERE_BASE}/v1/models").mock(
        return_value=httpx.Response(503, json={"message": "boom"})
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer prefix) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_uses_bearer_prefix(connector):
    """Connector must send the api_key as `Bearer <key>` in Authorization."""
    route = respx.get(f"{COHERE_BASE}/v1/models").mock(
        return_value=httpx.Response(200, json={"models": []})
    )
    await connector.list_models(page_size=1)
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_API_KEY}"
    # Shielva client marker
    assert route.calls[0].request.headers.get("x-client-name") == "shielva-connector"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_cohere_auth_error(connector):
    respx.get(f"{COHERE_BASE}/v1/models").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    with pytest.raises(CohereAuthError):
        await connector.list_models()


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_403_raises_cohere_auth_error(connector):
    respx.get(f"{COHERE_BASE}/v1/models").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    with pytest.raises(CohereAuthError):
        await connector.list_models()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{COHERE_BASE}/v1/models").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "command-r-plus"}]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{COHERE_BASE}/v1/models").mock(
        return_value=httpx.Response(401, json={"message": "bad token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# chat()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_chat_success(connector):
    expected = {
        "id": "chat-123",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello!"}],
        },
        "finish_reason": "COMPLETE",
    }
    route = respx.post(f"{COHERE_BASE}/v2/chat").mock(
        return_value=httpx.Response(200, json=expected)
    )
    result = await connector.chat(
        model="command-r-plus",
        messages=[{"role": "user", "content": "Hi"}],
    )
    assert route.called
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["model"] == "command-r-plus"
    assert body["messages"][0]["role"] == "user"
    assert result["id"] == "chat-123"


@respx.mock
@pytest.mark.asyncio
async def test_chat_auth_error(connector):
    respx.post(f"{COHERE_BASE}/v2/chat").mock(
        return_value=httpx.Response(401, json={"message": "unauth"})
    )
    with pytest.raises(CohereAuthError):
        await connector.chat(
            model="command-r-plus",
            messages=[{"role": "user", "content": "Hi"}],
        )


@respx.mock
@pytest.mark.asyncio
async def test_chat_bad_request_422(connector):
    respx.post(f"{COHERE_BASE}/v2/chat").mock(
        return_value=httpx.Response(422, json={"message": "invalid messages"})
    )
    with pytest.raises(CohereBadRequestError):
        await connector.chat(model="command-r-plus", messages=[])


# ═══════════════════════════════════════════════════════════════════════════
# embed()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_embed_success(connector):
    expected = {"id": "embed-1", "embeddings": {"float": [[0.1, 0.2, 0.3]]}}
    route = respx.post(f"{COHERE_BASE}/v2/embed").mock(
        return_value=httpx.Response(200, json=expected)
    )
    result = await connector.embed(model="embed-v4.0", texts=["hello world"])
    assert route.called
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["model"] == "embed-v4.0"
    assert body["texts"] == ["hello world"]
    assert body["input_type"] == "search_document"
    assert result["embeddings"]["float"][0] == [0.1, 0.2, 0.3]


# ═══════════════════════════════════════════════════════════════════════════
# rerank()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_rerank_success(connector):
    expected = {
        "id": "rerank-1",
        "results": [
            {"index": 1, "relevance_score": 0.93},
            {"index": 0, "relevance_score": 0.42},
        ],
    }
    respx.post(f"{COHERE_BASE}/v2/rerank").mock(
        return_value=httpx.Response(200, json=expected)
    )
    result = await connector.rerank(
        model="rerank-english-v3.0",
        query="who is Ada Lovelace?",
        documents=["irrelevant doc", "Ada Lovelace bio"],
        top_n=2,
    )
    assert result["results"][0]["index"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# classify()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_classify_success(connector):
    expected = {
        "classifications": [
            {"input": "hi there", "prediction": "greeting", "confidence": 0.99}
        ]
    }
    respx.post(f"{COHERE_BASE}/v1/classify").mock(
        return_value=httpx.Response(200, json=expected)
    )
    result = await connector.classify(
        model="embed-v4.0",
        inputs=["hi there"],
        examples=[
            {"text": "hello", "label": "greeting"},
            {"text": "bye", "label": "farewell"},
        ],
    )
    assert result["classifications"][0]["prediction"] == "greeting"


# ═══════════════════════════════════════════════════════════════════════════
# tokenize() / detokenize()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_tokenize_success(connector):
    expected = {"tokens": [1, 2, 3], "token_strings": ["hel", "lo", " world"]}
    respx.post(f"{COHERE_BASE}/v1/tokenize").mock(
        return_value=httpx.Response(200, json=expected)
    )
    result = await connector.tokenize(model="command-r-plus", text="hello world")
    assert result["tokens"] == [1, 2, 3]


@respx.mock
@pytest.mark.asyncio
async def test_detokenize_success(connector):
    expected = {"text": "hello world"}
    respx.post(f"{COHERE_BASE}/v1/detokenize").mock(
        return_value=httpx.Response(200, json=expected)
    )
    result = await connector.detokenize(model="command-r-plus", tokens=[1, 2, 3])
    assert result["text"] == "hello world"


# ═══════════════════════════════════════════════════════════════════════════
# list_models() / get_model()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_models_success(connector):
    expected = {
        "models": [
            {"name": "command-r-plus", "endpoints": ["chat"]},
            {"name": "embed-v4.0", "endpoints": ["embed"]},
        ]
    }
    route = respx.get(f"{COHERE_BASE}/v1/models").mock(
        return_value=httpx.Response(200, json=expected)
    )
    result = await connector.list_models(page_size=2)
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("page_size") == "2"
    assert len(result["models"]) == 2


@respx.mock
@pytest.mark.asyncio
async def test_get_model_success(connector):
    expected = {"name": "command-r-plus", "endpoints": ["chat"]}
    respx.get(f"{COHERE_BASE}/v1/models/command-r-plus").mock(
        return_value=httpx.Response(200, json=expected)
    )
    result = await connector.get_model("command-r-plus")
    assert result["name"] == "command-r-plus"


@respx.mock
@pytest.mark.asyncio
async def test_get_model_not_found(connector):
    respx.get(f"{COHERE_BASE}/v1/models/missing").mock(
        return_value=httpx.Response(404, json={"message": "model not found"})
    )
    with pytest.raises(CohereNotFound):
        await connector.get_model("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Datasets / Connectors / Fine-tunes
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_datasets_success(connector):
    respx.get(f"{COHERE_BASE}/v1/datasets").mock(
        return_value=httpx.Response(200, json={"datasets": [{"id": "ds-1"}]})
    )
    result = await connector.list_datasets()
    assert result["datasets"][0]["id"] == "ds-1"


@respx.mock
@pytest.mark.asyncio
async def test_create_dataset_posts_envelope(connector):
    route = respx.post(f"{COHERE_BASE}/v1/datasets").mock(
        return_value=httpx.Response(200, json={"dataset": {"id": "ds-new"}})
    )
    payload = {"name": "my-dataset", "dataset_type": "chat-finetune-input"}
    result = await connector.create_dataset(payload)
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == payload
    assert result["dataset"]["id"] == "ds-new"


@respx.mock
@pytest.mark.asyncio
async def test_list_connectors_success(connector):
    respx.get(f"{COHERE_BASE}/v1/connectors").mock(
        return_value=httpx.Response(200, json={"connectors": []})
    )
    result = await connector.list_connectors()
    assert "connectors" in result


@respx.mock
@pytest.mark.asyncio
async def test_list_finetuned_models_success(connector):
    respx.get(f"{COHERE_BASE}/v1/finetuning/finetuned-models").mock(
        return_value=httpx.Response(200, json={"finetuned_models": []})
    )
    result = await connector.list_finetuned_models(page_size=5)
    assert "finetuned_models" in result


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{COHERE_BASE}/v1/models").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow down"}),
            httpx.Response(200, json={"models": [{"name": "after-retry"}]}),
        ]
    )
    result = await connector.list_models(page_size=1)
    assert route.call_count == 2
    assert result["models"][0]["name"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{COHERE_BASE}/v1/models").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"models": []}),
        ]
    )
    result = await connector.list_models()
    assert route.call_count == 2
    assert result == {"models": []}


@respx.mock
@pytest.mark.asyncio
async def test_retry_exhausted_429_raises_rate_limit(connector, no_retry_sleep):
    respx.post(f"{COHERE_BASE}/v2/chat").mock(
        return_value=httpx.Response(429, json={"message": "still rate limited"})
    )
    with pytest.raises(CohereRateLimitError):
        await connector.chat(
            model="command-r-plus",
            messages=[{"role": "user", "content": "hi"}],
        )


# ═══════════════════════════════════════════════════════════════════════════
# sync() — Cohere is inference-only
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_returns_success_noop(connector):
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.SUCCESS
    assert result.documents_synced == 0
    assert result.documents_found == 0


# ═══════════════════════════════════════════════════════════════════════════
# authorize() shim (api_key has no OAuth)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_api_key_token(connector):
    token = await connector.authorize(auth_code="ignored", state="ignored")
    assert token.access_token == TEST_API_KEY
    assert token.token_type == "api_key"
    assert token.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert CohereConnector.CONNECTOR_TYPE == "cohere"


def test_auth_type_class_attr():
    assert CohereConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(CohereConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in CohereConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert 401 in CohereConnector._STATUS_MAP
    assert 403 in CohereConnector._STATUS_MAP
    assert 429 in CohereConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = CohereConnector(
        tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = CohereConnector(
        tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    # Independent http_clients — no shared state
    assert c1.http_client is not c2.http_client


# ═══════════════════════════════════════════════════════════════════════════
# Connector orchestration via mock_CohereHTTPClient (decoupled from httpx)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_models_delegates_to_http_client(connector, mock_CohereHTTPClient):
    mock_CohereHTTPClient.list_models.return_value = {"models": [{"name": "m1"}]}
    result = await connector.list_models(page_size=5, endpoint="chat")
    mock_CohereHTTPClient.list_models.assert_awaited_once_with(
        page_size=5,
        page_token=None,
        endpoint="chat",
    )
    assert result["models"][0]["name"] == "m1"


@pytest.mark.asyncio
async def test_embed_delegates_to_http_client(connector, mock_CohereHTTPClient):
    mock_CohereHTTPClient.embed.return_value = {"embeddings": {"float": [[0.0]]}}
    await connector.embed(model="embed-v4.0", texts=["x"])
    mock_CohereHTTPClient.embed.assert_awaited_once()
