# Mistral AI Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Mistral AI** is a hosted LLM provider exposing a stateless REST API under `https://api.mistral.ai/v1`. This connector — `MistralConnector` (`CONNECTOR_TYPE = "mistral"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from Mistral:

| Surface | Base path | Capability |
|---|---|---|
| Chat Completions | `/chat/completions` | LLM chat generation (sync + streaming) |
| Embeddings | `/embeddings` | Vector embeddings for retrieval |
| Models | `/models` | List + read available models, delete fine-tuned models |
| Files | `/files` | Upload + manage corpora for fine-tuning / batch |
| Fine-tuning | `/fine_tuning/jobs` | Launch + monitor fine-tune runs |
| Agents | `/agents` | Hosted assistants with tool use |

The connector normalises models + fine-tuning jobs + uploaded files into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff + jitter (honouring `Retry-After`), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::MistralHTTPClient`).

Mistral is stateless — there is no syncable corpus, so `sync()` catalogues available models + uploaded files as `NormalizedDocument` entries for observability and KB indexing (no real-time provider data to mirror).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

Mistral does NOT need a dedicated SDK — the REST surface is small, fully JSON, and async-first via `httpx`. No tenacity (we own the retry loop directly in `client/http_client.py`).

## 3. Auth Flow

Mistral REST API uses **Bearer API key authentication** for server-to-server integrations.

### Credentials
- `api_key` — Mistral API key created in **console.mistral.ai → API Keys**. Stored as install_field (type `secret`, required).

### Header contract
Every request to `https://api.mistral.ai/v1/*`:

```
Authorization: Bearer <api_key>
Content-Type:  application/json
Accept:        application/json
```

For `POST /files` the request is `multipart/form-data` (httpx sets `Content-Type` automatically when `files=` is used) — only `Authorization` + `Accept` headers are set manually.

### Lifecycle
- `install()` validates `api_key` is non-empty AND probes `GET /models` once to confirm the key is accepted. On 401 → `MISSING_CREDENTIALS + DEGRADED`. On network failure → `CONNECTED + DEGRADED` (installed but probe failed — health_check will retry).
- `authorize()` — NOT used (`api_key` flow has no exchange). Returns a synthetic `TokenInfo(access_token=api_key, token_type="Bearer")` for ABI compatibility.
- `health_check()` — `GET /models` as the cheapest authenticated probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

Mistral does not have a "documents" surface in the way Gmail or Wix do — there is no inbox, no orders. The two normalisable resources are **models** and **uploaded files**.

### 4.1 Model → NormalizedDocument

| NormalizedDocument | Mistral JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{model['id']}"` | tenant-scoped |
| `source_id` | `model["id"]` | e.g. `mistral-large-latest` |
| `title` | `model["id"]` | |
| `content` | concat description + capabilities | |
| `source` | `"mistral.model"` | |
| `created_at` | `datetime.fromtimestamp(model["created"])` when present | |
| `metadata` | `{owned_by, max_context_length, capabilities, ...}` | |

### 4.2 File → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{file['id']}"` |
| `source_id` | `file["id"]` |
| `title` | `file["filename"]` |
| `content` | `f"Purpose: {file['purpose']} ({file['bytes']} bytes)"` |
| `source` | `"mistral.file"` |
| `created_at` | `datetime.fromtimestamp(file["created_at"])` |
| `metadata` | `{purpose, bytes, status, sample_type}` |

### 4.3 Fine-tuning job → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{job['id']}"` |
| `source_id` | `job["id"]` |
| `title` | `f"Fine-tune {job['id']} ({job['model']})"` |
| `content` | `f"status={job['status']}, model={job['model']}"` |
| `source` | `"mistral.fine_tuning"` |
| `metadata` | `{status, model, fine_tuned_model, hyperparameters, training_files}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | `GET /models` probe | Validate api_key; init HTTP client. |
| `health_check()` | GET | `/models` | Lightweight probe (lists models accessible to the key). |
| `sync(since, full, kb_id)` | (lifecycle) | catalogues models + files | Stateless — sync returns `COMPLETED` with model/file counts. |
| `create_chat_completion(model, messages, ...)` | POST | `/chat/completions` | Body: `{model, messages, temperature, max_tokens, top_p, stream, ...}` |
| `create_embeddings(model, inputs, ...)` | POST | `/embeddings` | Body: `{model, input, encoding_format}` — Mistral wire key is `input` (singular). |
| `list_models()` | GET | `/models` | Returns `{object: "list", data: [...]}` |
| `get_model(model_id)` | GET | `/models/{model_id}` | Single model lookup. |
| `delete_model(model_id)` | DELETE | `/models/{model_id}` | Only fine-tuned models are deletable. |
| `list_files(purpose, page, page_size)` | GET | `/files` | Cursor-style pagination via `page` + `page_size`. |
| `upload_file(purpose, file_path)` | POST | `/files` | Multipart upload: `file=<bytes>`, `purpose=<string>`. |
| `delete_file(file_id)` | DELETE | `/files/{file_id}` | |
| `list_fine_tuning_jobs(page, page_size)` | GET | `/fine_tuning/jobs` | |
| `create_fine_tuning_job(model, training_files, hyperparameters)` | POST | `/fine_tuning/jobs` | Body: `{model, training_files: [{file_id}], hyperparameters}` |

Wire convention: Mistral uses **snake_case** keys (`max_tokens`, `top_p`, `training_files`, `file_id`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Mistral meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `MistralError` (raise — caller can branch on `status_code`) |
| 401 | API key invalid | `MistralAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` |
| 403 | Forbidden | `MistralAuthError` → `AuthStatus.INVALID_CREDENTIALS` |
| 404 | Not found | `MistralNotFound` (raise) |
| 429 | Rate limited | `MistralRateLimitError` — `Retry-After` honoured by client retry loop; raised after `_MAX_RETRIES` exhausted |
| 5xx | Provider outage | `MistralError` with `status_code` in 5xx → retried with exponential backoff + jitter |

All in `exceptions.py` extending `MistralError`. Retry in `client/http_client.py::_request_with_retry` honours `_MAX_RETRIES = 3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt + jitter(0, 0.25)` for 5xx, `Retry-After` (when present) for 429.

Transport-level (`httpx.TimeoutException`, `httpx.NetworkError`) raises `MistralNetworkError` after `_MAX_RETRIES`.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27.0
structlog>=24.1
```

(pydantic, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `Authorization: Bearer <api_key>` |
| `base_url` | string | no | install_field (default `https://api.mistral.ai/v1`) | Override for proxy / sandbox |
| `default_chat_model` | string | no | install_field (default `mistral-large-latest`) | Hint for callers; not consumed by HTTP layer |
| `default_embed_model` | string | no | install_field (default `mistral-embed`) | Hint for callers |
| `rate_limit_per_min` | number | no | install_field (default 60) | Soft client-side cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key"]
_STATUS_MAP = {
    401: ("DEGRADED",  "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds Bearer headers, retries 429/5xx with `Retry-After` + jitter, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Mistral payloads (models, files, fine-tuning jobs) → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Caller-level retry helper, payload builders (chat / embeddings / fine-tuning). | `exceptions` (stdlib + structlog) |
| `models.py` | Pydantic schemas with snake_case fields for request bodies + response envelopes. | `pydantic` |
| `exceptions.py` | `MistralError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `MistralConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, payload builders) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
