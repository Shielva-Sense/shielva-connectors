# Qdrant Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Qdrant** is an open-source vector database (Qdrant Cloud + self-hosted) exposing a REST API for vector similarity search, recommendation, payload-filtered retrieval, snapshots, and cluster introspection. This connector — `QdrantConnector` (`CONNECTOR_TYPE = "qdrant"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Qdrant cluster:

| Surface | Base path | Capability |
|---|---|---|
| Service | `/` , `/healthz`, `/readyz`, `/telemetry` | Health probe, version, telemetry |
| Cluster | `/cluster` | Topology + peer information |
| Collections | `/collections` | CRUD + alias on collections |
| Points (read) | `/collections/{name}/points/*` | search, batch_search, scroll, retrieve, count, recommend |
| Points (write) | `/collections/{name}/points` | upsert, delete (by id or filter) |
| Indexes | `/collections/{name}/index` | Create / delete payload-field indexes |
| Snapshots | `/collections/{name}/snapshots` | Create + list snapshots for backup/restore |

The cluster URL is **per-tenant** (not a provider-wide constant) — Qdrant Cloud gives every tenant their own URL (e.g. `https://my-cluster.aws.cloud.qdrant.io:6333`) and self-hosted deployments use whatever URL the operator chose (`http://localhost:6333`, or an internal DNS name). For this reason `base_url` is a **required** install field, not a hardcoded class constant.

The connector normalises **collections** into `NormalizedDocument` (id = `f"{tenant_id}_{collection_name}"`) when sync is invoked — this surfaces the catalogue of collections as discoverable docs in the Shielva KB. Operational point operations (upsert/search/scroll) are exposed as standalone `async def` methods (OCP), routed through `client/http_client.py::QdrantHTTPClient`.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

No Qdrant SDK is used: `qdrant-client` would pull in `grpcio`, `pydantic-settings`, `tqdm`, and a duplicated httpx — the connector pattern owns its HTTP client to keep retry/auth in one place (SOC). Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `pydantic`, `respx`.

## 3. Auth Flow

Qdrant REST API uses **header-based API key authentication** (`AUTH_TYPE = "api_key"`).

### Credentials
- `base_url` — Cluster URL (Qdrant Cloud `https://xyz.aws.cloud.qdrant.io:6333` OR self-hosted `http://localhost:6333`). install_field (type `string`, required).
- `api_key` — API key from Qdrant Cloud Dashboard → API Keys. install_field (type `secret`, conditionally required — Cloud requires it, default self-hosted may not).
- `default_collection` — Optional default collection name for shorthand calls. install_field (type `string`, optional).
- `rate_limit_per_min` — Client-side soft cap. install_field (type `number`, optional, default 600).

### Header contract
Every request to the cluster:

```
api-key:        <api_key>             ← lowercase! NOT 'Authorization', NOT 'Bearer'
Content-Type:   application/json
Accept:         application/json
```

When `api_key` is empty (self-hosted, no auth), the `api-key` header is omitted entirely.

### Lifecycle
- `install()` validates `base_url` is non-empty (api_key is optional to support no-auth self-hosted). Does **not** call the API.
- `authorize()` — NOT applicable (`api_key` flow has no exchange) — returns a TokenInfo with the key as the access_token for ABI parity.
- `health_check()` — `GET /healthz` (preferred) with `GET /readyz` and `GET /` fallbacks as lightweight probes.

## 4. Data Model

### 4.1 Collection → NormalizedDocument (sync output)

| NormalizedDocument | Qdrant JSON | Notes |
|---|---|---|
| `id` | `f"{connector_id}_{collection_name}"` | tenant-scoped via connector_id |
| `source_id` | `collection.name` | Qdrant collection name |
| `title` | `collection.name` | |
| `content` | summary string with vector size, distance, points count | |
| `source` | `"qdrant.collection"` | |
| `created_at` | `datetime.utcnow()` | Qdrant does not return creation timestamps |
| `metadata` | `{vectors_config, points_count, segments_count, status, optimizer_status}` | |

Qdrant is primarily a **vector store**, not a document source — sync is intentionally a lightweight catalogue mirror so the tenant's KB can render "what collections does this Qdrant cluster hold?". Operational workloads (upserting embeddings, similarity search) flow through the dedicated `upsert_points` / `search_points` methods.

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate `base_url`; init HTTP client. |
| `health_check()` | GET | `/healthz` (then `/readyz`, then `/`) | Probe chain ensures both Cloud and self-hosted answer. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates `/collections` | Calls `ingest_document` per collection. |
| `list_collections()` | GET | `/collections` | |
| `create_collection(collection_name, vectors_config, …)` | PUT | `/collections/{name}` | Body: vector size + distance. |
| `get_collection(collection_name)` | GET | `/collections/{name}` | |
| `delete_collection(collection_name)` | DELETE | `/collections/{name}` | |
| `update_collection(collection_name, optimizers_config?, params?)` | PATCH | `/collections/{name}` | |
| `upsert_points(collection_name, points, wait=True)` | PUT | `/collections/{name}/points` | Body: `{points: [...]}`. |
| `get_points(collection_name, ids, with_payload=True, with_vector=False)` | POST | `/collections/{name}/points` | Retrieve by id list. |
| `search_points(collection_name, vector, limit=10, filter?, score_threshold?)` | POST | `/collections/{name}/points/search` | Top-k similarity. |
| `delete_points(collection_name, points?, filter?, wait=True)` | POST | `/collections/{name}/points/delete` | Exactly one of points/filter. |
| `scroll_points(collection_name, limit=100, offset?, filter?)` | POST | `/collections/{name}/points/scroll` | Cursor pagination. |
| `count_points(collection_name, filter?, exact=True)` | POST | `/collections/{name}/points/count` | |
| `create_payload_index(collection_name, field_name, field_schema, wait=True)` | PUT | `/collections/{name}/index` | Schema: keyword/integer/float/geo/text/bool/datetime/uuid. |
| `delete_payload_index(collection_name, field_name, wait=True)` | DELETE | `/collections/{name}/index/{field_name}` | |
| `list_snapshots(collection_name)` | GET | `/collections/{name}/snapshots` | |
| `create_snapshot(collection_name)` | POST | `/collections/{name}/snapshots` | |
| `get_cluster_info()` | GET | `/cluster` | Cluster topology + peers. |

Wire convention: Qdrant uses **snake_case** in JSON (`vectors_config`, `points_count`, `with_payload`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Qdrant meaning | Mapped to |
|---|---|---|
| 400 | Bad request (malformed body / wrong dim) | `QdrantBadRequestError` (raise) |
| 401 | api-key missing/invalid | `QdrantAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (key lacks scope / cluster locked) | `QdrantAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Collection / point not found | `QdrantNotFoundError` (raise) |
| 409 | Conflict (collection exists / wrong state) | `QdrantConflictError` |
| 429 | Cloud-tier rate limit | `QdrantRateLimitError` (retry, honour `Retry-After`) |
| 5xx | Cluster outage / shard failover | `QdrantServerError` (retry with exponential backoff) |

All in `exceptions.py` extending `QdrantError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt` for 5xx and 429, plus `Retry-After` when the server provides one.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27,<1.0
```

(structlog, pydantic, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `base_url` | string | yes | install_field | Cluster URL — per-tenant (Cloud) or per-deployment (self-hosted). |
| `api_key` | secret | no | install_field | Sent in `api-key` header. Omit for default self-hosted (no auth). |
| `default_collection` | string | no | install_field | Per-call default for shorthand calls. |
| `rate_limit_per_min` | number | no | install_field (default 600) | Soft cap; raise per Qdrant Cloud tier. |
| `timeout_s` | number | no | install_field (default 60) | Per-request httpx timeout. |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["base_url"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

`api_key` is **conditionally** required: Qdrant Cloud always demands it (and `install()` warns when missing), while a default `docker run qdrant/qdrant` opens an unauthenticated port. Both must work — the keyless path is exercised in unit tests.

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds `api-key` header, retries 429/5xx, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Qdrant collection payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Async retry decorator wrapper for orchestration-side retries. | (stdlib only) |
| `models.py` | Pydantic schemas for collection config + vector params (camel/snake passthrough). | `pydantic` |
| `exceptions.py` | `QdrantError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `QdrantConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, rate-limit) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
