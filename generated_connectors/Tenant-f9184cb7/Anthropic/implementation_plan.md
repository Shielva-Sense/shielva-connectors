# Anthropic Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Anthropic** is the maker of the Claude family of large language models, exposing a JSON REST API under `https://api.anthropic.com/v1`. This connector — `AnthropicConnector` (`CONNECTOR_TYPE = "anthropic"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from Anthropic:

| Surface | Base path | Capability |
|---|---|---|
| Messages | `/messages` | Create chat completions (stream + non-stream) |
| Token counting | `/messages/count_tokens` | Cost-estimate input tokens before sending |
| Models | `/models` | List + read available models for the API key |
| Message Batches | `/messages/batches` | Submit/cancel/poll async batch inference jobs |
| Files (beta) | `/files` | Upload/list/get/delete user files (beta header gated) |
| Webhooks | n/a | Anthropic has no webhooks today; BaseConnector overrides are implemented as documented no-ops |

The connector treats Anthropic as an **inference surface, not a document source** — `sync()` returns a zero-document `SyncResult` (the API does not host crawlable corpora). All public methods are standalone `async def` (OCP), every HTTP call goes through `client/http_client.py::AnthropicHTTPClient` (SOC), and retries are handled with exponential backoff + `Retry-After` honouring (3 attempts on 429 / 5xx).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator for 429 / 5xx — installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`.

We deliberately do **not** depend on the official `anthropic` Python SDK. That SDK ships its own httpx client, retry, and rate-limit machinery — duplicating ours and tripling the wheel size. Direct REST calls keep the connector small and identical in shape to every other Shielva connector.

## 3. Auth Flow

The Anthropic REST API uses **API key authentication** for server-to-server access.

### Credentials
- `api_key` — Anthropic key (`sk-ant-...`) created in **Console → Settings → API Keys → Create Key**. Stored as install_field (type `secret`, required).
- `base_url` — Override (e.g. for a staging proxy or a regional endpoint). install_field (type `text`, optional, default `https://api.anthropic.com/v1`).
- `anthropic_version` — Date-pinned API version. install_field (type `text`, optional, default `"2023-06-01"`).
- `timeout_s` — Per-request httpx timeout in seconds. install_field (type `number`, optional, default `60`).
- `rate_limit_per_min` — Client-side soft cap, used by the token-bucket in `AnthropicHTTPClient`. install_field (type `number`, optional, default `50`).

### Header contract
Every request to `https://api.anthropic.com/v1/*`:

```
x-api-key:          <api_key>            (raw key — NO 'Bearer' prefix)
anthropic-version:  2023-06-01            (date-pinned API version, mandatory)
content-type:       application/json
accept:             application/json
anthropic-beta:     files-api-2025-04-14  (only on /files calls)
```

### Lifecycle
- `install()` validates `api_key` is non-empty. Does **not** call the API.
- `authorize()` — NOT implemented (api_key flow has no exchange). Returns an empty `TokenInfo` with `access_token == api_key` for ABI compatibility.
- `health_check()` — `GET /models?limit=1` as a lightweight probe (no token spend, unlike pinging `/messages` which costs input tokens).
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

The Anthropic API is request/response inference — it returns chat completions, not corpora. `sync()` is a no-op that returns `SyncResult(status=COMPLETED, documents_found=0, ...)`. However, when a caller wants to normalize a single Messages response to `NormalizedDocument`, we expose `helpers.normalizer.normalize_message_response`:

### 4.1 Message Response → NormalizedDocument

| NormalizedDocument | Anthropic JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{response['id']}"` | tenant-scoped |
| `source_id` | `response["id"]` | `msg_01...` |
| `title` | `f"Claude completion {response['model']}"` | |
| `content` | concatenated text from `response["content"][*]["text"]` | content_type=`text` |
| `source` | `"anthropic.messages"` | |
| `created_at` | `datetime.utcnow()` | API does not return a timestamp |
| `metadata` | `{model, stop_reason, usage.input_tokens, usage.output_tokens}` | |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate `api_key`; init HTTP client. |
| `authorize(auth_code, state)` | (lifecycle) | n/a | api_key shim — returns `TokenInfo(access_token=api_key)`. |
| `health_check()` | GET | `/models?limit=1` | Lightweight model-list probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | n/a | Documented no-op — returns `SyncResult(COMPLETED, 0/0/0)`. |
| `create_message(model, messages, max_tokens, system, temperature, stream)` | POST | `/messages` | Body: `{model, messages, max_tokens, system, temperature, stream}`. |
| `count_tokens(model, messages, system)` | POST | `/messages/count_tokens` | Input-token estimate. |
| `list_models(limit, before_id, after_id)` | GET | `/models` | Cursor pagination via `before_id` / `after_id`. |
| `get_model(model_id)` | GET | `/models/{model_id}` | |
| `create_batch(requests)` | POST | `/messages/batches` | Body: `{requests: [...]}`. |
| `get_batch(batch_id)` | GET | `/messages/batches/{batch_id}` | |
| `list_batches(limit, before_id, after_id)` | GET | `/messages/batches` | |
| `cancel_batch(batch_id)` | POST | `/messages/batches/{batch_id}/cancel` | |
| `get_batch_results(batch_id)` | GET | `/messages/batches/{batch_id}/results` | JSONL stream (returned as text in the dict's `raw` slot when not JSON). |
| `list_files(limit, before_id, after_id)` | GET | `/files` | Beta — adds `anthropic-beta: files-api-2025-04-14`. |
| `get_file(file_id)` | GET | `/files/{file_id}` | Beta. |
| `delete_file(file_id)` | DELETE | `/files/{file_id}` | Beta. |
| `handle_webhook(payload, headers)` | (lifecycle) | n/a | Documented no-op — Anthropic has no webhooks today. |
| `process_callback(payload, headers)` | (lifecycle) | n/a | Documented no-op. |
| `handle_event(event)` | (lifecycle) | n/a | Documented no-op. |
| `batch_processor(items, **kwargs)` | (lifecycle) | n/a | Returns `{processed: 0, items: []}`. |

Wire convention: Anthropic uses **snake_case** JSON (`stop_reason`, `input_tokens`, `output_tokens`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Anthropic meaning | Mapped to |
|---|---|---|
| 400 | `invalid_request_error` — bad model/body | `AnthropicBadRequestError` (raise) |
| 401 | `authentication_error` — bad / missing api key | `AnthropicAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | `permission_error` — org disabled / no scope | `AnthropicAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | `not_found_error` — bad model id, batch id, file id | `AnthropicNotFoundError` (raise) |
| 413 | `request_too_large` — body > limit | `AnthropicBadRequestError` |
| 429 | `rate_limit_error` (Anthropic returns `retry-after`) | `AnthropicRateLimitError` → `ConnectorHealth.DEGRADED` |
| 529 | `overloaded_error` — Anthropic side overload | `AnthropicServerError` → retry with backoff |
| 5xx | provider outage | `AnthropicServerError` → retry with exponential backoff |

All in `exceptions.py` extending `AnthropicError`. Retry in `client/http_client.py::_request` honours `max_retries=3`; exponential backoff with jitter (`base * 2 ** attempt + uniform(0, 0.25)`), capped at 30 s. When the response carries a `Retry-After` header (integer seconds), we honour it instead of the computed backoff.

`AnthropicNetworkError` covers transport-layer failure (DNS, TCP reset, TLS, timeout). Distinguished so callers can retry on transient network issues without retrying on logic errors.

## 7. Dependencies

Packages to install in the connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(`httpx`, `pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock` are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `x-api-key` header value |
| `base_url` | text | no | install_field (default `https://api.anthropic.com/v1`) | Override for proxy / staging |
| `anthropic_version` | text | no | install_field (default `2023-06-01`) | Pins the API surface contract |
| `timeout_s` | number | no | install_field (default 60) | Per-request httpx timeout |
| `rate_limit_per_min` | number | no | install_field (default 50) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers (`x-api-key`, `anthropic-version`), retries, raises typed exceptions on HTTP error, honours `Retry-After`, applies per-minute token-bucket pacing. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Anthropic payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry` generic retry helper, `safe_get` nested-dict walker. | (stdlib only) |
| `models.py` | Pydantic schemas with snake_case fields for request/response bodies. | `pydantic` |
| `exceptions.py` | `AnthropicError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `AnthropicConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, pagination) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
