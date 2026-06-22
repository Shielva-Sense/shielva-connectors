# Cohere Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Cohere** is a multilingual LLM-as-a-service provider exposing a REST API suite under `https://api.cohere.com`. This connector — `CohereConnector` (`CONNECTOR_TYPE = "cohere"`, `AUTH_TYPE = "api_key"`) — wraps the inference + model-management surfaces a Shielva tenant typically needs:

| Surface | Base path (v2) | Capability |
|---|---|---|
| Chat | `/v2/chat` | Multi-turn chat completions (Command R, Command R+, Command A) |
| Embed | `/v2/embed` | Vector embeddings (Embed v3 / v4) for search/RAG |
| Rerank | `/v2/rerank` | Cross-encoder relevance reranking (Rerank v3) |
| Classify | `/v1/classify` | Few-shot text classification |
| Tokenize | `/v1/tokenize` | Model-specific tokenisation |
| Detokenize | `/v1/detokenize` | Convert token IDs back to text |
| Models | `/v1/models` | List + describe available models |
| Datasets | `/v1/datasets` | List + create fine-tuning datasets |
| Connectors | `/v1/connectors` | List Cohere RAG connectors (not Shielva connectors) |
| Fine-tuning | `/v1/finetuning/finetuned-models` | List + inspect fine-tune jobs |

Cohere is an **inference API** — there is no document corpus to ingest. `sync()` is a no-op returning `SyncResult.COMPLETED` with 0 documents. The connector still surfaces standalone `async def` methods per user-requested operation (OCP), normalises model + dataset list responses into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`) when callers ask for them via the normalizer helper, and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::CohereHTTPClient`).

## 2. SDK / Package Selection

We deliberately do **not** depend on the official `cohere` SDK. The SDK pulls in `fastavro` + `httpx-sse` + numerous transitive deps for the streaming wire format, and it does not match Shielva's `async/await + httpx + structlog` patterns 1:1. Direct REST calls keep the dependency surface tiny and the retry behaviour under our control.

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Cohere uses **Bearer API key authentication**. There is no OAuth, no refresh, no expiry.

### Credentials
- `api_key` — Cohere production API key from **Dashboard → API Keys**. Stored as install_field (type `secret`, required).
- `base_url` — defaults to `https://api.cohere.com`; override only for a sandbox or proxy. Stored as install_field (type `string`, optional).
- `default_chat_model` — used by `chat()` when caller omits a model id. install_field (type `string`, optional, default `command-r-plus`).
- `default_embed_model` — used by `embed()` when caller omits a model id. install_field (type `string`, optional, default `embed-v4.0`).
- `rate_limit_per_min` — soft client-side cap surfaced for documentation; the http client itself relies on Cohere's 429 + retry. install_field (type `number`, optional, default 100).

### Header contract
Every request to `https://api.cohere.com/*`:

```
Authorization: Bearer <api_key>
Content-Type:   application/json
Accept:         application/json
X-Client-Name:  shielva-connector
```

### Lifecycle
- `install()` validates `api_key` is non-empty, then verifies the key by calling `GET /v1/models?page_size=1`. 401 → `INVALID_CREDENTIALS + OFFLINE`. Network/server error → `DEGRADED + FAILED` so the gateway can retry without forcing the operator to re-enter the key.
- `authorize()` — surface-compat shim only (returns a `TokenInfo` wrapping the api_key). Cohere has no auth-code exchange.
- `health_check()` — `GET /v1/models?page_size=1` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

Cohere is inference-first; there's no native corpus. When callers want a list of models or datasets normalised into `NormalizedDocument` (so they land in a knowledge base alongside other connectors), the normalizer helper provides:

### 4.1 Model → NormalizedDocument

| NormalizedDocument | Cohere JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{model['name']}"` | tenant-scoped |
| `source_id` | `model["name"]` | e.g. `command-r-plus` |
| `title` | `model["name"]` | |
| `content` | concatenation of name + endpoints + finetuned flag | |
| `source` | `"cohere.models"` | |
| `metadata` | `{endpoints, finetuned, context_length, default_endpoints}` | |

### 4.2 Dataset → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{dataset['id']}"` |
| `source_id` | `dataset["id"]` |
| `title` | `dataset["name"]` |
| `content` | `dataset.get("dataset_type", "") + " " + str(dataset.get("size", ""))` |
| `source` | `"cohere.datasets"` |
| `created_at` | `dataset["created_at"]` |
| `metadata` | `{dataset_type, validation_status, size}` |

### 4.3 Inference responses

Returned as-is (`Dict[str, Any]`) — callers consume the wire format directly. The chat response shape under v2:

```json
{
  "id": "chat-...",
  "message": { "role": "assistant", "content": [{ "type": "text", "text": "..." }] },
  "finish_reason": "COMPLETE",
  "usage": { "tokens": {"input": N, "output": M} }
}
```

A `helpers.utils.summarize_chat_response(resp)` helper extracts the assistant text.

## 5. Key API Endpoints & Methods

Every method listed below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; call `GET /v1/models?page_size=1` to confirm key. |
| `health_check()` | GET | `/v1/models?page_size=1` | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | n/a | No-op (`SyncResult.COMPLETED`, 0 docs). |
| `chat(model, messages, *, temperature, max_tokens, stream, ...)` | POST | `/v2/chat` | v2 chat completion. |
| `embed(model, texts, *, input_type, embedding_types)` | POST | `/v2/embed` | v2 embeddings. |
| `rerank(model, query, documents, *, top_n)` | POST | `/v2/rerank` | v2 reranker. |
| `classify(model, inputs, examples)` | POST | `/v1/classify` | Few-shot classification. |
| `tokenize(model, text)` | POST | `/v1/tokenize` | Per-model tokenisation. |
| `detokenize(model, tokens)` | POST | `/v1/detokenize` | Reverse tokenisation. |
| `list_models(*, page_size=20, page_token=None, endpoint=None)` | GET | `/v1/models` | Paginated. |
| `get_model(model_id)` | GET | `/v1/models/{model_id}` | |
| `list_datasets(*, dataset_type=None, limit=50)` | GET | `/v1/datasets` | Lists fine-tune datasets. |
| `create_dataset(payload)` | POST | `/v1/datasets` | Multipart in real life — connector accepts a prepared JSON envelope. |
| `list_connectors(*, page_size=20, page_token=None)` | GET | `/v1/connectors` | Cohere RAG connectors (not Shielva ones). |
| `list_finetuned_models(*, page_size=20)` | GET | `/v1/finetuning/finetuned-models` | List fine-tune jobs. |

Wire convention: Cohere uses **snake_case** in JSON (`max_tokens`, `input_type`, `page_token`) — connector method signatures mirror it.

## 6. Error Handling

| HTTP | Cohere meaning | Mapped to |
|---|---|---|
| 400 | Bad request (missing field, invalid model) | `CohereError` (raise) |
| 401 | API key invalid / missing | `CohereAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (key lacks endpoint scope) | `CohereAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Model / dataset not found | `CohereNotFound` (raise) |
| 422 | Validation error (invalid messages shape) | `CohereError` (raise) |
| 429 | Rate limited (trial keys = 5 RPM, prod = 10k RPM) | `CohereRateLimitError` → retry up to 3 with exponential backoff |
| 5xx | Provider outage | `CohereNetworkError` → retry with exponential backoff |

All in `exceptions.py` extending `CohereError`. Retry in `client/http_client.py::_request` honours `_max_retries=3` and `_RETRY_BACKOFF_FACTOR=2.0`, capped at `_RETRY_MAX_DELAY_S=16` seconds. `httpx.TimeoutException` / `httpx.NetworkError` are wrapped into `CohereNetworkError`.

`_STATUS_MAP` on the connector classifies failures during `health_check()`:

```python
_STATUS_MAP = {
    401: ("OFFLINE",   "INVALID_CREDENTIALS"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 7. Dependencies

Packages to install in the connector's venv (`install_deps` reads this section):

```
# (none beyond the pre-installed shared venv stack)
```

httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock and respx are pre-installed in the shared `.venv`. The Cohere connector adds **zero** new runtime dependencies — direct REST calls only.

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `Authorization: Bearer <key>` |
| `base_url` | string | no | install_field (default `https://api.cohere.com`) | Override for sandbox/proxy |
| `default_chat_model` | string | no | install_field (default `command-r-plus`) | Used by `chat()` when caller omits |
| `default_embed_model` | string | no | install_field (default `embed-v4.0`) | Used by `embed()` when caller omits |
| `rate_limit_per_min` | number | no | install_field (default 100) | Surfaced for docs / future client-side throttle |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key"]
_STATUS_MAP = {
    401: ("OFFLINE",   "INVALID_CREDENTIALS"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds Bearer headers, retries 429/5xx, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Cohere model/dataset payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | API-key masking, chat-response text summarisation, `with_retry` async helper. | (stdlib only) |
| `models.py` | Pydantic schemas for chat/embed/rerank/classify request bodies. | `pydantic` |
| `exceptions.py` | `CohereError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `CohereConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, masking) as composable helpers ✓
10. Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only ✓

**Score: 10/10.**
