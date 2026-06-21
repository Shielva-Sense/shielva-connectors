# OpenAI Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**OpenAI** is the foundational LLM + multimodal AI platform exposing a REST API at `https://api.openai.com/v1`. This connector — `OpenAIConnector` (`CONNECTOR_TYPE = "openai"`, `AUTH_TYPE = "api_key"`) — wraps every operational surface a Shielva tenant typically needs:

| Surface             | Base path             | Capability                                                       |
|---------------------|-----------------------|------------------------------------------------------------------|
| Models              | `/models`             | List + describe models the API key has access to                 |
| Chat Completions    | `/chat/completions`   | Run a chat completion (GPT-4o, GPT-4o-mini, o1, etc.)            |
| Embeddings          | `/embeddings`         | Vectorise text (text-embedding-3-small/large)                    |
| Files               | `/files`              | Upload / list / delete files used by Assistants, fine-tune, batch|
| Images              | `/images/generations` | Generate images (DALL·E 2/3, gpt-image-1)                        |
| Speech (TTS)        | `/audio/speech`       | Synthesise speech audio (tts-1, tts-1-hd)                        |
| Audio (Whisper)     | `/audio/transcriptions`| Speech-to-text (whisper-1)                                       |
| Moderations         | `/moderations`        | Policy-violation classifier (text-moderation-latest)             |

The connector exposes 11 public async methods + 4 webhook/event handler stubs, leaves the per-doc sync as a no-op (LLM provider — there is no static document corpus to ingest), and is fully multi-tenant: any `NormalizedDocument` produced uses `id = f"{tenant_id}_{source_id}"`.

## 2. SDK / Package Selection

We use **raw `httpx.AsyncClient`** rather than the official `openai` Python SDK so the connector matches the Bandwidth/Wix gold-standard pattern: single httpx owner inside `client/http_client.py`, retry policy + auth header centralised, zero hidden global state.

Dependencies (Section 7) intentionally minimal — `httpx`, `pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock` are all already pre-installed by the gateway. The only connector-specific dependency we add is **none** — pure `httpx`.

(Optional: `tiktoken` if token-budget tracking is needed downstream, but the connector itself does not require it.)

## 3. Auth Flow

| Step | Behaviour |
|---|---|
| **Credential model** | Single API key. Pre-issued by OpenAI dashboard: `https://platform.openai.com/api-keys`. Keys start with `sk-…`. |
| **Header shape** | `Authorization: Bearer <api_key>` — standard Bearer, the leading `Bearer ` IS required (unlike Wix). |
| **Optional org scoping** | `OpenAI-Organization: org_…` — sent only when `organization_id` is configured. |
| **Install** | `install()` validates the required keys exist in `self.config` (does **NOT** call the API). Returns `ConnectorStatus(HEALTHY, AUTHENTICATED)` on validation success, `(OFFLINE, MISSING_CREDENTIALS)` otherwise. |
| **Health check** | `GET /v1/models?limit=1` is the cheapest authenticated endpoint. 200 → `HEALTHY+CONNECTED`. 401 → `OFFLINE+TOKEN_EXPIRED`. 403 → `UNHEALTHY+INVALID_CREDENTIALS`. 429 → `DEGRADED+CONNECTED`. |
| **Token refresh** | None — API keys do not expire automatically. Rotation is done by re-running install with a new key. |

## 4. Data Model

The connector is API-passthrough — no inventory sync. The only normalisation surface is the chat completion response, where we expose `text`, `model`, `finish_reason`, and the raw response under `raw` for callers that need the full envelope.

`NormalizedDocument` is implemented but unused by the default `sync()` (returns `SyncResult(SUCCESS, 0/0/0)`) — kept for parity with the gateway protocol so downstream callers that want to log chat transcripts as documents can call `helpers.normalizer.normalize_chat_completion` directly.

## 5. Key API Endpoints & Methods

### 5.1 `async install() -> ConnectorStatus`
Validate `api_key` is present (the only `REQUIRED_CONFIG_KEYS` entry). Save merged config. Never calls the API.

### 5.2 `async health_check() -> ConnectorStatus`
Probe `GET /v1/models?limit=1`. Classify response via `_STATUS_MAP`.

### 5.3 `async sync(since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult`
No-op for an LLM provider — returns `SyncResult(SUCCESS, 0 found, 0 synced)` with message `"sync not applicable for LLM provider"`.

### 5.4 `async create_chat_completion(model, messages, temperature=1.0, max_tokens=1024, **kwargs) -> Dict`
`POST /v1/chat/completions`. Body: `{model, messages, temperature, max_tokens, ...kwargs}`. Returns the raw OpenAI response with `choices[0].message.content` carrying the assistant text.

### 5.5 `async create_embedding(model, input, dimensions=None) -> Dict`
`POST /v1/embeddings`. Body: `{model, input, dimensions?}`. The vector is at `response["data"][0]["embedding"]`.

### 5.6 `async list_models() -> Dict`
`GET /v1/models`. Returns `{object: "list", data: [...]}`.

### 5.7 `async get_model(model_id) -> Dict`
`GET /v1/models/{model_id}`.

### 5.8 `async list_files(purpose=None) -> Dict`
`GET /v1/files?purpose=…`. Returns `{data: [...]}`.

### 5.9 `async upload_file(purpose, file_name, content) -> Dict`
`POST /v1/files` — multipart form with `purpose` + `file=(file_name, content)`. `purpose ∈ {"assistants","batch","fine-tune","vision","user_data"}`.

### 5.10 `async delete_file(file_id) -> Dict`
`DELETE /v1/files/{file_id}`. Returns `{id, deleted: True}`.

### 5.11 `async create_image(prompt, model="dall-e-3", size="1024x1024", n=1, **kwargs) -> Dict`
`POST /v1/images/generations`.

### 5.12 `async create_speech(model, voice, input, response_format="mp3") -> bytes`
`POST /v1/audio/speech`. Returns raw audio bytes.

### 5.13 `async create_transcription(file_name, content, model="whisper-1", response_format="json") -> Dict`
`POST /v1/audio/transcriptions` — multipart with audio file. Returns `{text: "..."}`.

### 5.14 `async create_moderation(input, model="text-moderation-latest") -> Dict`
`POST /v1/moderations`.

### 5.15 `async handle_webhook(payload, headers=None) -> Dict`
Route OpenAI Realtime / Assistants event by `payload["type"]`. Unknown events → `{status: "ignored"}`.

### 5.16 `async process_callback(payload, headers=None) -> Dict`
Verify the `OpenAI-Signature` header via HMAC-SHA256 when `webhook_secret` is configured. Timing-safe compare.

### 5.17 `async handle_event(event) -> Dict`
Idempotency-keyed event ack.

### 5.18 `async batch_processor(items, **kwargs) -> Dict`
Iterate events through `handle_event` with per-item error capture.

## 6. Error Handling

| HTTP | Exception | Retryable |
|---|---|---|
| 400 | `OpenAIBadRequestError` | No |
| 401 | `OpenAIAuthError` | No |
| 403 | `OpenAIAuthError` (forbidden) | No |
| 404 | `OpenAINotFoundError` | No |
| 409 | `OpenAIConflictError` | No |
| 429 | `OpenAIRateLimitError` (carries `retry_after_s`) | Yes — honour `Retry-After`, max 3 attempts |
| 5xx | `OpenAIServerError` | Yes — exponential backoff `min(2**attempt, 8)`, max 3 attempts |
| network | `OpenAINetworkError` | Yes — exponential backoff |

Exceptions surface to callers; `health_check()` and `sync()` catch them at the lifecycle boundary and map via `_classify_failure` to `ConnectorStatus`.

## 7. Dependencies

Only connector-specific packages (gateway pre-installs: `httpx`, `pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock`):

```
# requirements.txt — no extras needed; raw httpx is sufficient.
```

## 8. Config & Install Fields

| Key | Type | Required | Purpose |
|---|---|---|---|
| `api_key` | secret | yes | OpenAI API key (`sk-…`). Sent as `Authorization: Bearer …`. |
| `base_url` | string | no | Default `https://api.openai.com/v1`. Override for Azure-compatible proxies. |
| `organization_id` | string | no | OpenAI org id (`org-…`). Sent as `OpenAI-Organization: …`. |
| `timeout_s` | number | no | Per-request httpx timeout (default `60`). |
| `webhook_secret` | secret | no | HMAC-SHA256 secret for `process_callback` signature verification. |

## 9. SOC / OCP Architecture Plan

- **SOC** — `connector.py` orchestrates only. Every HTTP call goes through `client/http_client.py::OpenAIHTTPClient.request`. Every data shape transformation is in `helpers/normalizer.py`. Retry / utility helpers in `helpers/utils.py`.
- **OCP** — adding a new OpenAI surface (e.g. `/threads`) is purely additive: add a method to `OpenAIHTTPClient`, expose a thin orchestrator on `OpenAIConnector`. `_STATUS_MAP` is a public class const so subclasses can extend without modifying logic.
- **Multi-tenant** — every `NormalizedDocument.id` is `f"{self.tenant_id}_{source_id}"`. The HTTP client is constructed per instance; there is no module-global state.
- **Abstract surface** — `install`, `sync`, `health_check` are implemented (otherwise `BaseConnector` raises `TypeError` at instantiation).
