# HuggingFace Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**HuggingFace** is the ML-hub provider exposing three distinct REST surfaces under separate base URLs. This connector — `HuggingFaceConnector` (`CONNECTOR_TYPE = "huggingface"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant needs from HuggingFace:

| Surface | Base URL | Capability |
|---|---|---|
| Hub API | `https://huggingface.co/api` | Models, datasets, spaces, organizations, whoami |
| Inference API | `https://api-inference.huggingface.co` | Serverless text-generation, embeddings, classification, summarization, translation, image classification |
| Inference Endpoints | `https://api.endpoints.huggingface.cloud/v2` | Managed dedicated model deployments (list / get / create / delete / invoke) |
| Repositories | `https://huggingface.co/api/{type}/{repoId}` | Repo-level read for models / datasets / spaces |

The connector normalises **models** into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 503 model-loading + 429 rate-limit + 5xx with exponential / hinted backoff, and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::HuggingFaceHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator alternative for caller-side retries |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

HuggingFace uses **API token (Bearer) authentication** for server-to-server integrations. No OAuth round-trip; no refresh.

### Credentials
- `api_key` — HuggingFace user access token created at **huggingface.co/settings/tokens**. install_field (type `secret`, required). Sent as `Authorization: Bearer <api_key>`.
- `base_url` — Optional override for the Hub base URL. install_field (type `string`, optional, default `https://huggingface.co/api`).
- `inference_url` — Optional override for the Inference API base URL. install_field (type `string`, optional, default `https://api-inference.huggingface.co`).
- `endpoints_url` — Optional override for the managed Inference Endpoints base URL. install_field (type `string`, optional, default `https://api.endpoints.huggingface.cloud/v2`).
- `default_model` — Optional default model id for inference calls. install_field (type `string`, optional).

### Header contract

Every outbound request to any HuggingFace surface:

```
Authorization: Bearer <api_key>
Content-Type:  application/json     (for JSON bodies)
Content-Type:  application/octet-stream   (for binary image/audio bodies)
Accept:        application/json
```

### Lifecycle
- `install()` validates `api_key` is non-empty. Does **not** call the API.
- `authorize()` — no-op (api_key flow). Returns `TokenInfo(access_token=api_key, token_type="Bearer")`.
- `health_check()` — `GET /whoami-v2` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Model → NormalizedDocument

| NormalizedDocument | HuggingFace JSON | Notes |
|---|---|---|
| `id` | `f"{connector_id}_{source_id}"` | tenant-scoped via connector_id |
| `source_id` | `model["id"]` (e.g. `meta-llama/Llama-3-8B-Instruct`) | |
| `title` | `model["id"]` | |
| `content` | `model.get("description")` or model_id | |
| `source` | `"huggingface"` | |
| `source_url` | `f"https://huggingface.co/{model_id}"` | |
| `author` | `model.get("author")` or owner segment of id | |
| `created_at` | `model["createdAt"]` parsed RFC 3339 | |
| `updated_at` | `model["lastModified"]` parsed | |
| `metadata` | `{tags, downloads, likes, pipeline_tag}` | |

### 4.2 Inference response

Inference API responses are passed through as-is (`Any`) — they are not normalized because the shape is highly model-dependent (list of dicts for text-generation, list of floats for embeddings, list-of-lists for classification, etc.).

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

### 5.1 Lifecycle

- `install()` — validate `api_key`; init HTTP client; persist config; seed `TokenInfo`.
- `authorize(auth_code, state)` — no-op; returns `TokenInfo`.
- `health_check()` — `GET /whoami-v2` (probe).
- `sync(since, full, kb_id)` — iterate models for the authenticated author, normalize, ingest.

### 5.2 Hub — model + dataset + space + org metadata

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `whoami()` | GET | `/whoami-v2` | Identity probe |
| `list_models(search?, author?, filter?, limit, sort)` | GET | `/models?limit=&sort=&search=&author=&filter=` | |
| `get_model(model_id)` | GET | `/models/{model_id}` | |
| `list_datasets(search?, limit)` | GET | `/datasets?limit=&search=` | |
| `get_dataset(dataset_id)` | GET | `/datasets/{dataset_id}` | |
| `list_spaces(search?, limit)` | GET | `/spaces?limit=&search=` | |
| `list_organization_repos(organization)` | GET | `/organizations/{name}` | |

### 5.3 Inference — serverless model invocation

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `run_inference(model, inputs, parameters?)` | POST | `https://api-inference.huggingface.co/models/{model}` | Generic JSON entrypoint |
| `text_generation(model, inputs, parameters?)` | POST | (same) | JSON `{inputs, parameters}` |
| `feature_extraction(model, inputs)` | POST | (same) | Returns embedding vector(s) |
| `text_classification(model, inputs)` | POST | (same) | Returns label scores |
| `summarization(model, inputs, parameters?)` | POST | (same) | |
| `translation(model, inputs)` | POST | (same) | |
| `image_classification(model, image_bytes)` | POST | (same) | Body: `application/octet-stream` |

### 5.4 Inference Endpoints — managed dedicated deployments

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `list_endpoints()` | GET | `https://api.endpoints.huggingface.cloud/v2/endpoint` | |
| `get_endpoint(endpoint_name)` | GET | `https://api.endpoints.huggingface.cloud/v2/endpoint/{name}` | |
| `create_endpoint(payload)` | POST | `https://api.endpoints.huggingface.cloud/v2/endpoint` | Body: full endpoint spec |
| `delete_endpoint(endpoint_name)` | DELETE | `https://api.endpoints.huggingface.cloud/v2/endpoint/{name}` | |
| `run_inference_endpoint(endpoint_url, payload)` | POST | provided URL | Caller supplies the served URL |

Wire convention: HuggingFace uses **camelCase** keys for some fields (`createdAt`, `lastModified`, `pipeline_tag`) and snake_case for others. The connector boundary accepts/returns `Dict[str, Any]` payloads as-is.

## 6. Error Handling

| HTTP | HuggingFace meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `HuggingFaceAPIError` (raise) |
| 401 | Token invalid / missing | `HuggingFaceAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (token lacks scope) | `HuggingFaceAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `HuggingFaceNotFound` (raise) |
| 429 | Rate limited | `HuggingFaceRateLimitError` → retry with exponential backoff + jitter |
| 503 (model loading) | `{"error": "Model X is currently loading", "estimated_time": N}` | `HuggingFaceModelLoadingError(estimated_time=N)` → retry after hint |
| 5xx | Provider outage | `HuggingFaceAPIError(status_code=5xx)` → retry with exponential backoff |

All in `exceptions.py` extending `HuggingFaceError`. Retry in `client/http_client.py::_request` honours `max_retries=3`.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27.0
respx>=0.21
pytest>=7
pytest-asyncio>=0.23
pytest-mock>=3.12
```

(structlog, pydantic, tenacity are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `Authorization: Bearer <api_key>` |
| `base_url` | string | no | install_field (default `https://huggingface.co/api`) | Hub base URL override |
| `inference_url` | string | no | install_field (default `https://api-inference.huggingface.co`) | Inference API override |
| `endpoints_url` | string | no | install_field (default `https://api.endpoints.huggingface.cloud/v2`) | Managed endpoints override |
| `default_model` | string | no | install_field | Default model id for inference calls |
| `rate_limit_per_min` | number | no | install_field (default 60) | Soft client-side cap |

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
| `client/http_client.py` | Single owner of httpx. Builds headers, retries (503 hint, 429 backoff, 5xx backoff), raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw HuggingFace payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `sanitize_model_id`, `with_retry`, `safe_get`. | (stdlib only) |
| `models.py` | Pydantic / dataclass schemas for request shapes. | `pydantic`, `dataclasses` |
| `exceptions.py` | `HuggingFaceError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `HuggingFaceConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, backoff) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
