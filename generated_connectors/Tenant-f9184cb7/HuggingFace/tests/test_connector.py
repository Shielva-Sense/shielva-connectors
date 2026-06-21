"""Unit tests for HuggingFaceConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import HuggingFaceConnector
from exceptions import (
    HuggingFaceAuthError,
    HuggingFaceModelLoadingError,
    HuggingFaceNotFound,
    HuggingFaceRateLimitError,
)

from tests.conftest import (
    CONNECTOR_ID,
    ENDPOINTS_BASE,
    HUB_BASE,
    INFERENCE_BASE,
    TENANT_ID,
    TEST_API_KEY,
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
async def test_install_missing_api_key():
    cfg = dict(TEST_CONFIG)
    cfg["api_key"] = ""
    c = HuggingFaceConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg,
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_accepts_api_token_backcompat():
    """Older configs used `api_token` — connector must still accept it."""
    cfg = {k: v for k, v in TEST_CONFIG.items() if k != "api_key"}
    cfg["api_token"] = TEST_API_KEY
    c = HuggingFaceConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg,
    )
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED


# ═══════════════════════════════════════════════════════════════════════════
# health_check() / whoami — Hub /whoami-v2
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{HUB_BASE}/whoami-v2").mock(
        return_value=httpx.Response(
            200, json={"name": "vivek", "type": "user", "email": "v@example.com"},
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{HUB_BASE}/whoami-v2").mock(
        return_value=httpx.Response(401, json={"error": "Invalid token"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@respx.mock
@pytest.mark.asyncio
async def test_whoami_returns_user_payload(connector):
    payload = {"name": "vivek", "type": "user", "orgs": []}
    respx.get(f"{HUB_BASE}/whoami-v2").mock(
        return_value=httpx.Response(200, json=payload),
    )
    result = await connector.whoami()
    assert result == payload


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer prefix on every surface)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_bearer_header_sent_on_hub_request(connector):
    route = respx.get(f"{HUB_BASE}/whoami-v2").mock(
        return_value=httpx.Response(200, json={"name": "v"}),
    )
    await connector.whoami()
    assert route.calls[0].request.headers.get("authorization") == f"Bearer {TEST_API_KEY}"


@respx.mock
@pytest.mark.asyncio
async def test_bearer_header_sent_on_inference_request(connector):
    model = "gpt2"
    route = respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        return_value=httpx.Response(200, json=[{"generated_text": "x"}]),
    )
    await connector.text_generation(model=model, inputs="hi")
    assert route.calls[0].request.headers.get("authorization") == f"Bearer {TEST_API_KEY}"


@respx.mock
@pytest.mark.asyncio
async def test_bearer_header_sent_on_endpoints_request(connector):
    route = respx.get(f"{ENDPOINTS_BASE}/endpoint").mock(
        return_value=httpx.Response(200, json={"items": []}),
    )
    await connector.list_endpoints()
    assert route.calls[0].request.headers.get("authorization") == f"Bearer {TEST_API_KEY}"


# ═══════════════════════════════════════════════════════════════════════════
# list_models() — sort + filter passthrough
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_models_with_filter_and_sort(connector):
    sample = [
        {"id": "meta-llama/Llama-3-8B-Instruct", "downloads": 1_200_000, "likes": 5000},
        {"id": "mistralai/Mistral-7B-v0.1", "downloads": 900_000, "likes": 3500},
    ]
    route = respx.get(f"{HUB_BASE}/models").mock(
        return_value=httpx.Response(200, json=sample),
    )
    result = await connector.list_models(filter="text-generation", sort="downloads", limit=10)
    assert result == sample
    assert route.called
    qs = dict(route.calls[0].request.url.params)
    assert qs.get("filter") == "text-generation"
    assert qs.get("sort") == "downloads"
    assert qs.get("limit") == "10"


@respx.mock
@pytest.mark.asyncio
async def test_get_model(connector):
    model_id = "meta-llama/Llama-3-8B-Instruct"
    payload = {"id": model_id, "downloads": 1_200_000, "pipeline_tag": "text-generation"}
    respx.get(f"{HUB_BASE}/models/{model_id}").mock(
        return_value=httpx.Response(200, json=payload),
    )
    result = await connector.get_model(model_id)
    assert result["id"] == model_id


@respx.mock
@pytest.mark.asyncio
async def test_get_model_not_found(connector):
    respx.get(f"{HUB_BASE}/models/no-such-model").mock(
        return_value=httpx.Response(404, json={"error": "Repository not found"}),
    )
    with pytest.raises(HuggingFaceNotFound):
        await connector.get_model("no-such-model")


# ═══════════════════════════════════════════════════════════════════════════
# list_datasets() / get_dataset / list_spaces() / list_organization_repos()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_datasets(connector):
    sample = [{"id": "squad", "downloads": 50000}]
    respx.get(f"{HUB_BASE}/datasets").mock(
        return_value=httpx.Response(200, json=sample),
    )
    result = await connector.list_datasets(search="squad", limit=5)
    assert result == sample


@respx.mock
@pytest.mark.asyncio
async def test_get_dataset(connector):
    ds_id = "squad"
    respx.get(f"{HUB_BASE}/datasets/{ds_id}").mock(
        return_value=httpx.Response(200, json={"id": ds_id, "downloads": 50000}),
    )
    result = await connector.get_dataset(ds_id)
    assert result["id"] == ds_id


@respx.mock
@pytest.mark.asyncio
async def test_list_spaces(connector):
    sample = [{"id": "spaces/demo-1"}]
    respx.get(f"{HUB_BASE}/spaces").mock(
        return_value=httpx.Response(200, json=sample),
    )
    result = await connector.list_spaces(search="demo", limit=5)
    assert result == sample


@respx.mock
@pytest.mark.asyncio
async def test_list_organization_repos(connector):
    payload = {"name": "huggingface", "repos": ["transformers", "datasets"]}
    respx.get(f"{HUB_BASE}/organizations/huggingface").mock(
        return_value=httpx.Response(200, json=payload),
    )
    result = await connector.list_organization_repos("huggingface")
    assert result["name"] == "huggingface"


# ═══════════════════════════════════════════════════════════════════════════
# Inference API: text_generation, feature_extraction, text_classification,
# summarization, translation, image_classification, run_inference
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_text_generation(connector):
    model = "gpt2"
    sample = [{"generated_text": "Hello world!"}]
    route = respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        return_value=httpx.Response(200, json=sample),
    )
    result = await connector.text_generation(
        model=model,
        inputs="Hello",
        parameters={"max_new_tokens": 20},
    )
    assert result == sample
    body = route.calls[0].request.read()
    assert b'"inputs": "Hello"' in body or b'"inputs":"Hello"' in body
    assert b"max_new_tokens" in body


@respx.mock
@pytest.mark.asyncio
async def test_run_inference_generic(connector):
    model = "gpt2"
    sample = [{"generated_text": "ok"}]
    route = respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        return_value=httpx.Response(200, json=sample),
    )
    result = await connector.run_inference(
        model=model, inputs="hi", parameters={"temperature": 0.5},
    )
    assert result == sample
    body = route.calls[0].request.read()
    assert b"temperature" in body


@respx.mock
@pytest.mark.asyncio
async def test_feature_extraction(connector):
    model = "sentence-transformers/all-MiniLM-L6-v2"
    sample = [0.01, 0.02, 0.03, 0.04]
    respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        return_value=httpx.Response(200, json=sample),
    )
    result = await connector.feature_extraction(model=model, inputs="cat")
    assert isinstance(result, list)
    assert len(result) == 4


@respx.mock
@pytest.mark.asyncio
async def test_text_classification(connector):
    model = "distilbert-base-uncased-finetuned-sst-2-english"
    sample = [
        [
            {"label": "POSITIVE", "score": 0.99},
            {"label": "NEGATIVE", "score": 0.01},
        ]
    ]
    respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        return_value=httpx.Response(200, json=sample),
    )
    result = await connector.text_classification(model=model, inputs="I love this!")
    assert result[0][0]["label"] == "POSITIVE"


@respx.mock
@pytest.mark.asyncio
async def test_summarization(connector):
    model = "facebook/bart-large-cnn"
    sample = [{"summary_text": "Short summary."}]
    respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        return_value=httpx.Response(200, json=sample),
    )
    result = await connector.summarization(
        model=model, inputs="Long article text...", parameters={"max_length": 60},
    )
    assert result[0]["summary_text"] == "Short summary."


@respx.mock
@pytest.mark.asyncio
async def test_translation(connector):
    model = "Helsinki-NLP/opus-mt-en-fr"
    sample = [{"translation_text": "Bonjour le monde"}]
    respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        return_value=httpx.Response(200, json=sample),
    )
    result = await connector.translation(model=model, inputs="Hello world")
    assert result[0]["translation_text"] == "Bonjour le monde"


@respx.mock
@pytest.mark.asyncio
async def test_image_classification(connector):
    model = "google/vit-base-patch16-224"
    sample = [{"label": "tabby cat", "score": 0.91}]
    route = respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        return_value=httpx.Response(200, json=sample),
    )
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # fake PNG-ish blob
    result = await connector.image_classification(model=model, image_bytes=image_bytes)
    assert result[0]["label"] == "tabby cat"
    # Body must be the raw bytes (octet-stream), not JSON.
    sent = route.calls[0].request
    assert sent.headers.get("content-type") == "application/octet-stream"
    assert sent.read() == image_bytes


# ═══════════════════════════════════════════════════════════════════════════
# Inference Endpoints (managed deployments)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_endpoints(connector):
    payload = {"items": [{"name": "prod-llama", "status": {"state": "running"}}]}
    respx.get(f"{ENDPOINTS_BASE}/endpoint").mock(
        return_value=httpx.Response(200, json=payload),
    )
    result = await connector.list_endpoints()
    assert result["items"][0]["name"] == "prod-llama"


@respx.mock
@pytest.mark.asyncio
async def test_get_endpoint(connector):
    name = "prod-llama"
    respx.get(f"{ENDPOINTS_BASE}/endpoint/{name}").mock(
        return_value=httpx.Response(200, json={"name": name, "status": {"state": "running"}}),
    )
    result = await connector.get_endpoint(name)
    assert result["name"] == name


@respx.mock
@pytest.mark.asyncio
async def test_create_endpoint(connector):
    route = respx.post(f"{ENDPOINTS_BASE}/endpoint").mock(
        return_value=httpx.Response(200, json={"name": "new-endpoint"}),
    )
    spec = {
        "name": "new-endpoint",
        "type": "protected",
        "provider": {"vendor": "aws", "region": "us-east-1"},
        "compute": {"accelerator": "gpu", "instanceType": "g5.xlarge"},
        "model": {"repository": "meta-llama/Llama-3-8B-Instruct"},
    }
    result = await connector.create_endpoint(spec)
    assert result["name"] == "new-endpoint"
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == spec


@respx.mock
@pytest.mark.asyncio
async def test_delete_endpoint(connector):
    name = "old-endpoint"
    respx.delete(f"{ENDPOINTS_BASE}/endpoint/{name}").mock(
        return_value=httpx.Response(200, json={"deleted": name}),
    )
    result = await connector.delete_endpoint(name)
    assert result == {"deleted": name}


@respx.mock
@pytest.mark.asyncio
async def test_run_inference_endpoint(connector):
    served = "https://custom-endpoint.huggingface.cloud/predict"
    route = respx.post(served).mock(
        return_value=httpx.Response(200, json=[{"generated_text": "via-endpoint"}]),
    )
    result = await connector.run_inference_endpoint(served, {"inputs": "hi"})
    assert result[0]["generated_text"] == "via-endpoint"
    body = route.calls[0].request.read()
    assert b'"inputs"' in body


# ═══════════════════════════════════════════════════════════════════════════
# Model loading 503 → retry-with-estimated_time
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_inference_model_loading_503_then_retry(fast_connector):
    model = "bigscience/bloom"
    loading_resp = httpx.Response(
        503,
        json={
            "error": "Model bigscience/bloom is currently loading",
            "estimated_time": 0.01,
        },
    )
    ok_resp = httpx.Response(200, json=[{"generated_text": "done"}])
    route = respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        side_effect=[loading_resp, ok_resp],
    )
    result = await fast_connector.text_generation(model=model, inputs="ping")
    assert result == [{"generated_text": "done"}]
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_inference_model_loading_503_exhausts_retries(fast_connector):
    """When every attempt returns 503, the final attempt surfaces the error."""
    model = "bigscience/bloom"
    loading_resp = httpx.Response(
        503,
        json={"error": "Model is currently loading", "estimated_time": 0.01},
    )
    respx.post(f"{INFERENCE_BASE}/models/{model}").mock(return_value=loading_resp)
    with pytest.raises(HuggingFaceModelLoadingError):
        await fast_connector.text_generation(model=model, inputs="ping")


# ═══════════════════════════════════════════════════════════════════════════
# 429 → retry-with-backoff
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_inference_retry_on_429(fast_connector):
    model = "gpt2"
    rate_limit = httpx.Response(429, json={"error": "Too many requests"})
    ok_resp = httpx.Response(200, json=[{"generated_text": "ok"}])
    route = respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        side_effect=[rate_limit, ok_resp],
    )
    result = await fast_connector.text_generation(model=model, inputs="hi")
    assert result == [{"generated_text": "ok"}]
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_inference_429_exhausts_retries(fast_connector):
    model = "gpt2"
    respx.post(f"{INFERENCE_BASE}/models/{model}").mock(
        return_value=httpx.Response(429, json={"error": "rate limited"}),
    )
    with pytest.raises(HuggingFaceRateLimitError):
        await fast_connector.text_generation(model=model, inputs="hi")


# ═══════════════════════════════════════════════════════════════════════════
# 5xx retry on Hub
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(fast_connector):
    """5xx triggers retry too."""
    route = respx.get(f"{HUB_BASE}/whoami-v2").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json={"name": "vivek"}),
        ]
    )
    result = await fast_connector.whoami()
    assert route.call_count == 2
    assert result == {"name": "vivek"}


# ═══════════════════════════════════════════════════════════════════════════
# Auth error on Hub call
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_models_auth_error(connector):
    respx.get(f"{HUB_BASE}/models").mock(
        return_value=httpx.Response(401, json={"error": "Invalid credentials"}),
    )
    with pytest.raises(HuggingFaceAuthError):
        await connector.list_models()


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity + multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert HuggingFaceConnector.CONNECTOR_TYPE == "huggingface"


def test_auth_type_class_attr():
    assert HuggingFaceConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(HuggingFaceConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in HuggingFaceConnector.REQUIRED_CONFIG_KEYS


def test_independent_instances_per_tenant():
    c1 = HuggingFaceConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = HuggingFaceConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
