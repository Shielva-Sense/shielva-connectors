# Pinecone Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Pinecone** is a managed vector database exposing a REST API split across two distinct planes:

| Plane | Base URL | Capability |
|---|---|---|
| **Control plane** | `https://api.pinecone.io` | Indexes CRUD, Collections (snapshots) CRUD, list namespaces |
| **Data plane** | `https://{index_host}` — per-index host returned by `describe_index` | Vector upsert / query / fetch / update / delete, `describe_index_stats` |

This connector — `PineconeConnector` (`CONNECTOR_TYPE = "pinecone"`, `AUTH_TYPE = "api_key"`) — wraps both planes behind a single SOC-clean Python surface:

| Surface | Plane | Methods |
|---|---|---|
| Indexes | control | `list_indexes`, `describe_index`, `create_index`, `delete_index`, `configure_index` |
| Collections | control | `list_collections`, `create_collection`, `delete_collection` |
| Vectors | data | `upsert_vectors`, `query`, `fetch_vectors`, `update_vector`, `delete_vectors` |
| Stats | data | `describe_index_stats`, `list_namespaces` |
| Lifecycle | — | `install`, `authorize`, `health_check`, `sync` |

The connector resolves the `index_name → data_plane_host` mapping lazily on first call (via control-plane `describe_index`) and caches the result on the HTTP client. Subsequent vector ops skip the control plane entirely. SOC enforced: `connector.py` orchestrates only; HTTP lives in `client/http_client.py::PineconeHTTPClient`; normalization lives in `helpers/normalizer.py`.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv. Used for both control and data planes. |
| `pydantic` | `>=2.0` | Optional dataclass schemas for vector records; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

**No Pinecone SDK.** We hit the REST API directly via httpx so the connector is single-process, has no background event-loop, and matches the Wix/Bandwidth gold-standard pattern.

## 3. Auth Flow

Pinecone REST API uses **server-to-server API key authentication**. There is no OAuth dance, no token refresh, no expiry.

### Credentials
- `api_key` — Pinecone API key from **Pinecone Console → API Keys**. Stored as install_field (type `secret`, required). Scoped to a single Pinecone project.
- `environment` — Cloud region tag (e.g. `us-east-1-aws`, `gcp-starter`). install_field (type `string`, required). Used as a hint for legacy data-plane hosts when `describe_index` does not return a `host` field.
- `project_id` — Pinecone project ID (UUID-ish). install_field (type `string`, optional). Used as a fallback to build `https://{index}-{project_id}.svc.{environment}.pinecone.io` if the control plane is unreachable.
- `default_index` / `default_namespace` — operational shortcuts so callers can omit `index_name` / `namespace`.

### Header contract

Every outbound request — control AND data plane — carries:

```
Api-Key:                  <api_key>          ← raw key (NOT Authorization)
Content-Type:             application/json
Accept:                   application/json
X-Pinecone-API-Version:   <api_version>      ← default 2025-01
```

Note the header name is `Api-Key`, **not** `Authorization`. This is the Pinecone-specific gotcha.

### Lifecycle
- `install()` validates `api_key` is non-empty; saves config. Does **not** call the API.
- `authorize()` — returns a synthetic `TokenInfo(access_token=api_key, token_type="ApiKey")` for surface parity with OAuth connectors.
- `health_check()` — `GET {control}/indexes` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

Pinecone is primarily a **write target** (vector sink) rather than a content source. The connector still normalizes index metadata into `NormalizedDocument` for KB-style observability:

### 4.1 Index → NormalizedDocument

| NormalizedDocument | Pinecone JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{index['name']}"` | tenant-scoped |
| `source_id` | `index["name"]` | Index name |
| `title` | `index["name"]` | |
| `content` | `f"Pinecone index ({metric}, dim={dimension})"` | Human summary |
| `source` | `"pinecone.index"` | |
| `metadata` | `{dimension, metric, host, status, spec.serverless.{cloud,region}, totalVectorCount}` | |

### 4.2 Vector record (wire format)

```python
{
    "id":       str,                  # required, max 512 chars
    "values":   List[float],          # required, must match index dimension
    "metadata": Dict[str, Any] | None # optional, JSON-serializable
}
```

`helpers/utils.normalize_vector_record` coerces loose input to this shape and drops empty metadata.

## 5. Key API Endpoints & Methods

Every method MUST exist as a standalone public `async def` in `connector.py` (OCP).

### Control plane

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. No API call. |
| `health_check()` | GET | `/indexes` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates indexes | Calls `describe_index_stats` per index → ingests stats. |
| `list_indexes()` | GET | `/indexes` | No pagination — Pinecone returns full list. |
| `describe_index(index_name)` | GET | `/indexes/{name}` | Response contains `host` — cached in `_index_host_cache`. |
| `create_index(name, dimension, metric, cloud, region)` | POST | `/indexes` | Body: `{name, dimension, metric, spec: {serverless: {cloud, region}}}`. |
| `delete_index(index_name)` | DELETE | `/indexes/{name}` | Also clears `_index_host_cache[index_name]`. |
| `configure_index(index_name, replicas, pod_type)` | PATCH | `/indexes/{name}` | Body: `{spec: {pod: {replicas, pod_type}}}`. |
| `list_collections()` | GET | `/collections` | |
| `create_collection(name, source)` | POST | `/collections` | Body: `{name, source: <index_name>}`. |
| `delete_collection(collection_name)` | DELETE | `/collections/{name}` | |

### Data plane (per-index host)

The connector resolves the host transparently: every data-plane method calls `_resolve_index_host(index_name)` which returns the cached host or calls `describe_index` first.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `upsert_vectors(index_name, vectors, namespace)` | POST | `{host}/vectors/upsert` | Body: `{vectors: [...], namespace?}`. Auto-batched to 100 vectors/call. |
| `query(index_name, vector, top_k, namespace, filter, include_metadata, include_values)` | POST | `{host}/query` | Body: `{vector, topK, namespace?, filter?, includeMetadata, includeValues}`. |
| `fetch_vectors(index_name, ids, namespace)` | GET | `{host}/vectors/fetch?ids=...&namespace=...` | IDs sent as repeated query params. |
| `update_vector(index_name, id, values, metadata, namespace)` | POST | `{host}/vectors/update` | Body: `{id, values?, setMetadata?, namespace?}`. Partial update. |
| `delete_vectors(index_name, ids, delete_all, namespace)` | POST | `{host}/vectors/delete` | Body: `{ids? | deleteAll?, namespace?}`. |
| `describe_index_stats(index_name, filter)` | POST | `{host}/describe_index_stats` | Returns `{dimension, totalVectorCount, namespaces, indexFullness}`. |
| `list_namespaces(index_name)` | GET | `{host}/namespaces` | Newer API — falls back to stats keys if 404. |

Wire convention: Pinecone uses **camelCase** in JSON (`topK`, `includeMetadata`, `setMetadata`, `upsertedCount`, `totalVectorCount`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Pinecone meaning | Mapped to |
|---|---|---|
| 400 | Bad request (e.g. dimension mismatch) | `PineconeBadRequestError` (raise) |
| 401 | API key invalid / missing header | `PineconeAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.DEGRADED` |
| 403 | Forbidden (key lacks permission for project) | `PineconeAuthError` |
| 404 | Index / collection / namespace not found | `PineconeNotFoundError` (raise) |
| 409 | Conflict (e.g. index already exists) | `PineconeConflictError` |
| 429 | Rate limited (Pinecone serverless: 100/min) | `PineconeRateLimitError` → retried with exponential backoff |
| 5xx | Provider outage | `PineconeServerError` → retried with exponential backoff |

All in `exceptions.py` extending `PineconeError`. Back-compat aliases: `PineconeNetworkError = PineconeServerError` (preserved for older code).

Retry in `client/http_client.py::_request_with_retry`:
- 429 / 5xx: `min(base * 2^attempt + jitter, 32s)`, up to `_RETRY_MAX_ATTEMPTS = 3`
- transport errors (`httpx.HTTPError`): same backoff, wrapped in `PineconeNetworkError`

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section). `httpx`, `pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock`, `respx` are pre-installed.

```
# (none — Pinecone connector uses only pre-installed packages)
```

The connector explicitly avoids the `pinecone-client` SDK to keep deps minimal and match the SOC pattern used by Wix / Bandwidth.

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `Api-Key` header value |
| `environment` | string | yes | install_field | Cloud region tag (e.g. `us-east-1-aws`) — used as fallback for legacy hosts |
| `project_id` | string | no | install_field | Pinecone project UUID — fallback host builder |
| `default_index` | string | no | install_field | Used when caller omits `index_name` |
| `default_namespace` | string | no | install_field | Used when caller omits `namespace` |
| `control_url` | string | no | install_field (default `https://api.pinecone.io`) | Override for proxies |
| `api_version` | string | no | install_field (default `2025-01`) | `X-Pinecone-API-Version` header |
| `rate_limit_per_min` | number | no | install_field (default 100) | Soft client-side cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key", "environment"]
_STATUS_MAP = {
    401: ("DEGRADED",  "INVALID_CREDENTIALS"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Sole owner of httpx. Builds headers, retries 429/5xx, raises typed exceptions on HTTP error. Maintains `index → host` cache. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Pinecone index payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Vector-record coercion, chunking, namespace coercion, `with_retry` helper. | (stdlib only) |
| `models.py` | Local dataclasses (`IndexSpec`, `VectorRecord`, `QueryMatch`, `QueryResponse`) + shim re-exports of SDK enums. | `dataclasses`, `shared.base_connector` |
| `exceptions.py` | `PineconeError` hierarchy + back-compat aliases. | (stdlib) |
| `__init__.py` | Re-export `PineconeConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, host cache, batching) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
