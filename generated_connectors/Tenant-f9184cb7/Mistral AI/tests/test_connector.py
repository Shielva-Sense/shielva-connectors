"""Unit tests for MistralConnector — respx-mocked, zero real I/O."""
import json
import tempfile
from pathlib import Path

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import MistralConnector
from exceptions import (
    MistralAuthError,
    MistralBadRequestError,
    MistralError,
    MistralNotFound,
    MistralNotFoundError,
    MistralRateLimitError,
)

from tests.conftest import (
    CONNECTOR_ID,
    MISTRAL_BASE,
    SAMPLE_CHAT_RESPONSE,
    SAMPLE_FILES_RESPONSE,
    SAMPLE_MODELS_RESPONSE,
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
    respx.get(f"{MISTRAL_BASE}/models").mock(
        return_value=httpx.Response(200, json=SAMPLE_MODELS_RESPONSE)
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
async def test_install_auth_error_when_key_rejected(connector):
    respx.get(f"{MISTRAL_BASE}/models").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer(connector):
    """Connector must send the api_key as a Bearer token in Authorization."""
    route = respx.get(f"{MISTRAL_BASE}/models").mock(
        return_value=httpx.Response(200, json=SAMPLE_MODELS_RESPONSE)
    )
    await connector.list_models()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_API_KEY}"


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{MISTRAL_BASE}/models").mock(
        return_value=httpx.Response(200, json=SAMPLE_MODELS_RESPONSE)
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_token_expired(connector):
    respx.get(f"{MISTRAL_BASE}/models").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# create_chat_completion()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_chat_completion_success(connector):
    route = respx.post(f"{MISTRAL_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=SAMPLE_CHAT_RESPONSE)
    )
    result = await connector.create_chat_completion(
        model="mistral-large-latest",
        messages=[{"role": "user", "content": "Hi"}],
        temperature=0.3,
    )
    assert result["id"] == "cmpl-abc123"
    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert route.called
    sent = json.loads(route.calls.last.request.content.decode())
    assert sent["model"] == "mistral-large-latest"
    assert sent["temperature"] == 0.3
    assert sent["messages"][0]["content"] == "Hi"


@respx.mock
@pytest.mark.asyncio
async def test_chat_completion_passes_optional_fields(connector):
    route = respx.post(f"{MISTRAL_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=SAMPLE_CHAT_RESPONSE)
    )
    await connector.create_chat_completion(
        model="mistral-large-latest",
        messages=[{"role": "user", "content": "Hi"}],
        response_format={"type": "json_object"},
        tools=[{"type": "function", "function": {"name": "noop"}}],
    )
    sent = json.loads(route.calls.last.request.content.decode())
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["tools"][0]["function"]["name"] == "noop"


@respx.mock
@pytest.mark.asyncio
async def test_chat_completion_auth_error(connector):
    respx.post(f"{MISTRAL_BASE}/chat/completions").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    with pytest.raises(MistralAuthError):
        await connector.create_chat_completion(
            model="mistral-large-latest",
            messages=[{"role": "user", "content": "Hi"}],
        )


# ═══════════════════════════════════════════════════════════════════════════
# create_embeddings()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_embeddings_success(connector):
    emb_response = {
        "object": "list",
        "data": [
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            {"index": 1, "embedding": [0.4, 0.5, 0.6]},
        ],
        "model": "mistral-embed",
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }
    route = respx.post(f"{MISTRAL_BASE}/embeddings").mock(
        return_value=httpx.Response(200, json=emb_response)
    )
    result = await connector.create_embeddings(
        model="mistral-embed", inputs=["hello", "world"]
    )
    assert len(result["data"]) == 2
    sent = json.loads(route.calls.last.request.content.decode())
    assert sent["model"] == "mistral-embed"
    # Mistral wire key is `input` (singular).
    assert sent["input"] == ["hello", "world"]
    assert sent["encoding_format"] == "float"


# ═══════════════════════════════════════════════════════════════════════════
# Models — list / get / delete
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_models_success(connector):
    respx.get(f"{MISTRAL_BASE}/models").mock(
        return_value=httpx.Response(200, json=SAMPLE_MODELS_RESPONSE)
    )
    result = await connector.list_models()
    assert result["data"][0]["id"] == "mistral-large-latest"


@respx.mock
@pytest.mark.asyncio
async def test_get_model_success(connector):
    model_id = "mistral-large-latest"
    respx.get(f"{MISTRAL_BASE}/models/{model_id}").mock(
        return_value=httpx.Response(200, json={"id": model_id, "object": "model"})
    )
    result = await connector.get_model(model_id)
    assert result["id"] == model_id


@respx.mock
@pytest.mark.asyncio
async def test_delete_model_not_found(connector):
    respx.delete(f"{MISTRAL_BASE}/models/nonexistent").mock(
        return_value=httpx.Response(404, json={"message": "model not found"})
    )
    with pytest.raises(MistralNotFoundError):
        await connector.delete_model("nonexistent")


@pytest.mark.asyncio
async def test_delete_model_validates_model_id(connector):
    with pytest.raises(MistralError):
        await connector.delete_model("")


# ═══════════════════════════════════════════════════════════════════════════
# Files — list / upload / get / delete
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_files_success(connector):
    route = respx.get(f"{MISTRAL_BASE}/files").mock(
        return_value=httpx.Response(200, json=SAMPLE_FILES_RESPONSE)
    )
    result = await connector.list_files(purpose="fine-tune", page=0, page_size=50)
    assert result["data"][0]["id"] == "file-1"
    qs = route.calls.last.request.url.params
    assert qs.get("purpose") == "fine-tune"
    assert qs.get("page") == "0"
    assert qs.get("page_size") == "50"


@respx.mock
@pytest.mark.asyncio
async def test_upload_file_multipart(connector):
    upload_response = {"id": "file-xyz", "purpose": "fine-tune", "filename": "tr.jsonl"}
    route = respx.post(f"{MISTRAL_BASE}/files").mock(
        return_value=httpx.Response(200, json=upload_response)
    )
    with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".jsonl") as fh:
        fh.write(b'{"prompt": "hello", "completion": "world"}\n')
        path = fh.name

    try:
        result = await connector.upload_file(purpose="fine-tune", file_path=path)
    finally:
        Path(path).unlink(missing_ok=True)

    assert result["id"] == "file-xyz"
    req = route.calls.last.request
    ctype = req.headers.get("Content-Type", "")
    assert ctype.startswith("multipart/form-data")
    body = req.content
    assert b"purpose" in body
    assert b"fine-tune" in body


@pytest.mark.asyncio
async def test_upload_file_validates_purpose(connector):
    with pytest.raises(MistralError):
        await connector.upload_file(purpose="", file_path="/tmp/x")


@pytest.mark.asyncio
async def test_upload_file_validates_path(connector):
    with pytest.raises(MistralError):
        await connector.upload_file(purpose="fine-tune", file_path="")


@respx.mock
@pytest.mark.asyncio
async def test_get_file_success(connector):
    respx.get(f"{MISTRAL_BASE}/files/file-abc").mock(
        return_value=httpx.Response(200, json={"id": "file-abc", "purpose": "fine-tune"})
    )
    result = await connector.get_file("file-abc")
    assert result["id"] == "file-abc"


@respx.mock
@pytest.mark.asyncio
async def test_delete_file_success(connector):
    respx.delete(f"{MISTRAL_BASE}/files/file-abc").mock(
        return_value=httpx.Response(200, json={"id": "file-abc", "deleted": True})
    )
    result = await connector.delete_file("file-abc")
    assert result["deleted"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Fine-tuning
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_create_fine_tuning_job_success(connector):
    job_response = {
        "id": "ft-job-1",
        "status": "queued",
        "model": "open-mistral-7b",
    }
    route = respx.post(f"{MISTRAL_BASE}/fine_tuning/jobs").mock(
        return_value=httpx.Response(200, json=job_response)
    )
    result = await connector.create_fine_tuning_job(
        model="open-mistral-7b",
        training_files=["file-1", "file-2"],
        hyperparameters={"training_steps": 100, "learning_rate": 1e-4},
    )
    assert result["id"] == "ft-job-1"
    sent = json.loads(route.calls.last.request.content.decode())
    assert sent["model"] == "open-mistral-7b"
    assert sent["training_files"] == [{"file_id": "file-1"}, {"file_id": "file-2"}]
    assert sent["hyperparameters"]["training_steps"] == 100


@pytest.mark.asyncio
async def test_create_fine_tuning_job_validates_files(connector):
    with pytest.raises(MistralError):
        await connector.create_fine_tuning_job(model="open-mistral-7b", training_files=[])


@pytest.mark.asyncio
async def test_create_fine_tuning_job_validates_model(connector):
    with pytest.raises(MistralError):
        await connector.create_fine_tuning_job(model="", training_files=["file-1"])


@respx.mock
@pytest.mark.asyncio
async def test_list_fine_tuning_jobs_success(connector):
    jobs_response = {"object": "list", "data": [{"id": "ft-1"}], "total": 1}
    respx.get(f"{MISTRAL_BASE}/fine_tuning/jobs").mock(
        return_value=httpx.Response(200, json=jobs_response)
    )
    result = await connector.list_fine_tuning_jobs()
    assert result["data"][0]["id"] == "ft-1"


# ═══════════════════════════════════════════════════════════════════════════
# Retry behaviour
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """First call returns 429 with Retry-After: 0; second call returns 200."""
    route = respx.post(f"{MISTRAL_BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "rate"}),
            httpx.Response(200, json=SAMPLE_CHAT_RESPONSE),
        ]
    )
    result = await connector.create_chat_completion(
        model="mistral-large-latest",
        messages=[{"role": "user", "content": "Hi"}],
    )
    assert result["id"] == "cmpl-abc123"
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{MISTRAL_BASE}/models").mock(
        side_effect=[
            httpx.Response(500, json={"message": "internal"}),
            httpx.Response(200, json=SAMPLE_MODELS_RESPONSE),
        ]
    )
    result = await connector.list_models()
    assert "data" in result
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_persistent_429_raises_rate_limit_error(connector, no_retry_sleep):
    respx.post(f"{MISTRAL_BASE}/embeddings").mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "0"}, json={"message": "rate"}
        )
    )
    with pytest.raises(MistralRateLimitError):
        await connector.create_embeddings(model="mistral-embed", inputs=["a"])


# ═══════════════════════════════════════════════════════════════════════════
# Mocked-client unit test (uses mock_MistralHTTPClient fixture)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_models_delegates_to_http_client(connector, mock_MistralHTTPClient):
    """connector.list_models() must call http_client.list_models with the api_key."""
    connector.http_client = mock_MistralHTTPClient
    mock_MistralHTTPClient.list_models.return_value = SAMPLE_MODELS_RESPONSE

    result = await connector.list_models()
    mock_MistralHTTPClient.list_models.assert_awaited_with(TEST_API_KEY)
    assert result == SAMPLE_MODELS_RESPONSE


# ═══════════════════════════════════════════════════════════════════════════
# Bad-request mapping
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_400_bad_request_raises_bad_request_error(connector):
    respx.post(f"{MISTRAL_BASE}/chat/completions").mock(
        return_value=httpx.Response(400, json={"message": "bad body"})
    )
    with pytest.raises(MistralBadRequestError):
        await connector.create_chat_completion(
            model="mistral-large-latest",
            messages=[{"role": "user", "content": "Hi"}],
        )


# ═══════════════════════════════════════════════════════════════════════════
# Back-compat alias
# ═══════════════════════════════════════════════════════════════════════════

def test_mistral_not_found_alias_matches_canonical():
    """`MistralNotFound` alias must be the same class as `MistralNotFoundError`."""
    assert MistralNotFound is MistralNotFoundError


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity + multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert MistralConnector.CONNECTOR_TYPE == "mistral"


def test_auth_type_class_attr():
    assert MistralConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(MistralConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in MistralConnector.REQUIRED_CONFIG_KEYS


def test_independent_instances_per_tenant():
    c1 = MistralConnector(tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = MistralConnector(tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    assert c1.tenant_id == "tenant-A"
    assert c2.tenant_id == "tenant-B"


@pytest.mark.asyncio
async def test_connector_id_propagates_to_status(connector):
    """install() must surface the connector_id passed at construction."""
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.connector_id == CONNECTOR_ID
    assert TENANT_ID == connector.tenant_id
