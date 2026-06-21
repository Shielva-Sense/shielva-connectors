# Weaviate Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Weaviate** is an open-source vector database with REST + GraphQL surfaces. Each tenant points at their own cluster — Weaviate Cloud (`https://<cluster>.weaviate.network`), self-hosted (`http://localhost:8080`), or any operator-managed deployment. Because the cluster URL is **per-tenant**, `base_url` is a first-class install_field, NOT a class constant. This connector — `WeaviateConnector` (`CONNECTOR_TYPE = "weaviate"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Weaviate cluster:

| Surface | Base path | Capability |
|---|---|---|
| Schema | `/v1/schema` | List / create / get / delete classes (collections) |
| Objects | `/v1/objects` | CRUD a single object by UUID inside a class |
| Batch | `/v1/batch/objects` | Bulk insert objects (KB sync hot-path) |
| GraphQL | `/v1/graphql` | `Get`, `Aggregate`, `Explore` queries (vector + hybrid search) |
| Backups | `/v1/backups/{backend}` | Create + status of cluster backups |
| Tenants | `/v1/schema/{class}/tenants` | Multi-tenancy primitive — list/create/delete tenant slices in a class |
| Cluster / Health | `/v1/.well-known/ready` + `/v1/meta` | Liveness probe + version surface |

The connector normalises Weaviate `objects` into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::WeaviateHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

We deliberately do NOT depend on the official `weaviate-client` SDK:
- It's a heavy sync/async hybrid with its own connection pool that fights `httpx`.
- The REST + GraphQL surface we need is small enough to expose directly.
- Avoiding it keeps install closure minimal and aligns with the Wix/Bandwidth pattern.

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Weaviate clusters support three auth modes; the connector routes between them based on `api_key` presence and the `auth_mode` install field.

### Credentials (install_fields)

- `base_url` — Cluster URL like `https://my-cluster.weaviate.network` or `http://localhost:8080`. **Required, per-tenant** (every cluster has its own URL — never a class constant).
- `api_key` — Weaviate API key (Weaviate Cloud, or self-hosted clusters with the api-key auth module enabled). Stored as install_field (type `secret`, optional — anonymous self-hosted clusters omit it).
- `grpc_port` — gRPC port for high-throughput batch (default 50051). Optional, surfaced for future expansion; the REST client does not require it.

### Header contract

Every request to `{base_url}/v1/*`:

```
Authorization: Bearer <api_key>      (when api_key is set)
Content-Type:  application/json
Accept:        application/json
```

Anonymous self-hosted clusters omit the `Authorization` header entirely.

### Lifecycle

- `install()` validates `base_url` is non-empty and a valid HTTP(S) URL. Does **not** call the cluster.
- `authorize()` — NOT implemented (`api_key` flow has no exchange). Returns an opaque `TokenInfo` carrying the api_key.
- `health_check()` — `GET /v1/.well-known/ready` (Weaviate's standard liveness probe; returns 200 when the cluster is ready to serve queries).
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Weaviate Object → NormalizedDocument

| NormalizedDocument | Weaviate JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{object['id']}"` | tenant-scoped — the rule in SOC enforcement |
| `source_id` | `object["id"]` | Weaviate UUIDv4 |
| `title` | first non-empty of `properties.title`, `properties.name`, `properties.text` (truncated to 120 chars) | |
| `content` | `json.dumps(properties)` | The full property bag stays searchable |
| `source` | `f"weaviate.{class_name}"` | |
| `created_at` | `object["creationTimeUnix"]` (ms epoch) → datetime | |
| `updated_at` | `object["lastUpdateTimeUnix"]` → datetime | |
| `metadata` | `{class, vector_present, tenant, additional}` | Includes `additional.distance` / `additional.certainty` when available |

### 4.2 Class (collection) — passthrough Dict[str, Any]

Schemas are wire-compatible: `list_classes` returns `{classes: [...]}` and `create_class(body)` accepts the same shape Weaviate documents at https://weaviate.io/developers/weaviate/api/rest/schema. We do NOT normalize schema definitions.

### 4.3 GraphQL — opaque Dict[str, Any]

`graphql_query(query, variables)` returns the raw `{data, errors}` envelope. Callers parse the `Get`/`Aggregate`/`Explore` shape themselves.

## 5. Key API Endpoints & Methods

Every method below MUST exist as a standalone public `async def` in `connector.py`.

### Lifecycle

- `install()` → validate config; init HTTP client.
- `health_check()` → `GET /v1/.well-known/ready`. Falls back to `/v1/meta` if the ready endpoint returns 503 (still useful telemetry).
- `sync(since, full, kb_id)` → iterate every class, page through objects via `/v1/objects?class={c}&limit=100&after={uuid}`, normalize, `ingest_document`.

### Schema (classes)

- `list_classes()` → `GET /v1/schema`. Returns `{classes: [...]}`.
- `create_class(class_body)` → `POST /v1/schema`. Body is the raw class definition.
- `get_class(class_name)` → `GET /v1/schema/{className}`.
- `delete_class(class_name)` → `DELETE /v1/schema/{className}`. Returns `{}` on 204.

### Objects (per-row CRUD)

- `list_objects(*, class_name=None, limit=100, after=None, include=None)` → `GET /v1/objects?class={c}&limit={N}&after={uuid}&include={vector,classification}`.
- `create_object(class_name, properties, *, vector=None, id=None, tenant=None)` → `POST /v1/objects`. Body: `{class, properties, vector?, id?, tenant?}`.
- `get_object(class_name, object_id, *, include=None, tenant=None)` → `GET /v1/objects/{className}/{id}?include=...&tenant=...`.
- `update_object(class_name, object_id, properties, *, vector=None, tenant=None)` → `PATCH /v1/objects/{className}/{id}`. Partial-update semantics.
- `delete_object(class_name, object_id, *, tenant=None)` → `DELETE /v1/objects/{className}/{id}?tenant=...`.

### Batch

- `batch_create_objects(objects, *, consistency_level=None)` → `POST /v1/batch/objects`. Body: `{objects: [...]}`. Optional `?consistency_level=ONE|QUORUM|ALL`.

### GraphQL

- `graphql_query(query, variables=None)` → `POST /v1/graphql`. Body: `{query, variables}`.

### Multi-tenancy

- `list_tenants(class_name)` → `GET /v1/schema/{className}/tenants`.
- `create_tenant(class_name, tenants)` → `POST /v1/schema/{className}/tenants`. Body is the list of `{name, activityStatus?}`.
- `delete_tenant(class_name, tenant_names)` → `DELETE /v1/schema/{className}/tenants`. Body is the list of names.

### Backups

- `create_backup(backend, backup_id, *, include=None, exclude=None)` → `POST /v1/backups/{backend}`. Body: `{id, include?, exclude?}`. `backend` is `s3 | gcs | azure | filesystem`.
- `get_backup_status(backend, backup_id)` → `GET /v1/backups/{backend}/{id}`.

### Cluster

- `get_meta()` → `GET /v1/meta`. Returns version, hostname, modules — useful for compatibility probing.

Wire convention: Weaviate uses **camelCase** in JSON (`creationTimeUnix`, `vectorIndexConfig`, `lastUpdateTimeUnix`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Weaviate meaning | Mapped to |
|---|---|---|
| 400 | Bad request (e.g. malformed schema) | `WeaviateBadRequestError` (raise) |
| 401 | API key missing or invalid | `WeaviateAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden — RBAC role lacks scope | `WeaviateAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Class / object / tenant not found | `WeaviateNotFoundError` |
| 409 | Conflict — duplicate object id or class already exists | `WeaviateConflictError` |
| 422 | Schema validation failure | `WeaviateValidationError` |
| 429 | Rate limited (rare on self-hosted; Weaviate Cloud applies it) | `WeaviateRateLimitError` → exponential backoff |
| 5xx | Provider outage / index rebuild storm | `WeaviateServerError` → retry with exponential backoff |

All in `exceptions.py` extending `WeaviateError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt` for 5xx + 429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27,<1.0
structlog>=24.1
```

(pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `base_url` | text | **yes** | install_field | Cluster URL — per-tenant. e.g. `https://my-cluster.weaviate.network`. NEVER a class constant. |
| `api_key` | secret | no | install_field | Weaviate API key. Omit for anonymous self-hosted clusters. Required for Weaviate Cloud. |
| `grpc_port` | number | no | install_field (default 50051) | Reserved for future batch-over-gRPC. The REST client does not use this today. |
| `timeout_s` | number | no | install_field (default 30) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["base_url", "api_key"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

Note on `REQUIRED_CONFIG_KEYS`: `api_key` is listed for the common Weaviate Cloud case. Self-hosted anonymous clusters can pass an empty string; `install()` only hard-fails when `base_url` is missing.

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Weaviate objects → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry` async helper, ISO date parsing, dict-walk safe_get. | (stdlib only) |
| `models.py` | Pydantic schemas with camelCase aliases for request bodies. | `pydantic` |
| `exceptions.py` | `WeaviateError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `WeaviateConnector`. | `connector` |

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
