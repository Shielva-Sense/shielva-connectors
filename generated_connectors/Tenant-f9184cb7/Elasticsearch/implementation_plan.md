# Elasticsearch Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Elasticsearch** is the canonical distributed search + analytics engine, exposing a stable REST API under a tenant-specific cluster URL (e.g. `https://my-cluster.es.us-east-1.aws.elastic-cloud.com:9243` for Elastic Cloud, or `https://es.internal:9200` for a self-hosted deployment). This connector — `ElasticsearchConnector` (`CONNECTOR_TYPE = "elasticsearch"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a cluster:

| Surface | Base path | Capability |
|---|---|---|
| Cluster | `/`, `/_cluster/health` | Liveness + green/yellow/red health |
| Indices | `/_cat/indices`, `/{index}` | List / get / create / delete |
| Documents | `/{index}/_doc/{id}` | Index / get / update / delete |
| Search | `/{index}/_search`, `/{index}/_count` | Full DSL + count |
| Bulk | `/_bulk` | NDJSON paired action+source |
| Mapping | `/{index}/_mapping` | Get / put mappings |
| Aliases | `/_aliases`, `/_cat/aliases` | List aliases |
| Snapshots | `/_snapshot/{repo}/_all` | Snapshot inventory |

**Per-tenant cluster URL.** Unlike SaaS connectors with a single fixed host, every Elasticsearch install carries its own `base_url` (Elastic Cloud → `*.cloud.es.io`, self-hosted → operator-controlled). The connector reads this from `self.config["base_url"]` (alias accepted: `host`) and never embeds a default. The `base_url` is the only **mandatory** install field — `api_key` is *optional* (anonymous self-hosted clusters exist) but strongly recommended and the canonical path for Elastic Cloud.

The connector normalises indices (one `NormalizedDocument` per index) into the Shielva KB (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::ElasticsearchHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `pydantic` | `>=2.0` | Available but unused — connector boundary is plain dicts |

We deliberately do NOT depend on the official `elasticsearch-py` client. Reasons:
1. It ships its own httpx-incompatible transport and a sync-first API that clashes with the async-throughout BaseConnector.
2. It pulls a huge dependency tree (urllib3, certifi pin), adds version-coupling risk to specific cluster versions.
3. Our needs are a tight, well-typed subset (15 method surfaces). A thin httpx layer is simpler, smaller, and lets us own retry + auth behaviour end-to-end.

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Elasticsearch supports three auth surfaces; this connector exposes the two server-to-server ones:

### `AUTH_TYPE = "api_key"` (preferred)

- Generate from **Kibana → Stack Management → API Keys → Create API Key**.
- Kibana returns an *encoded* key (`base64(id:api_key)`).
- Sent as `Authorization: ApiKey <encoded>`. **No** `Bearer` prefix.

### HTTP Basic (fallback)

- `username` + `password` install fields.
- Sent as `Authorization: Basic <base64(username:password)>`.
- Useful for self-hosted clusters where API keys aren't enabled.

### Anonymous (rare)

- Self-hosted dev clusters with `xpack.security.enabled: false` accept unauthenticated requests.
- Supported by leaving `api_key`, `username`, and `password` all blank.

### Header contract

```
Authorization: ApiKey <encoded>     ← api_key path
            OR Basic  <base64(u:p)> ← basic path
            OR (absent)             ← anonymous
Content-Type: application/json      ← all but /_bulk
            OR application/x-ndjson ← /_bulk only
Accept:       application/json
```

### Lifecycle

- `install()` validates `base_url` is present; if `api_key` and `username/password` are both blank, accept anonymous mode. Probes `GET /` to verify reachability + credential validity.
- `authorize()` returns `health_check()` — there is no OAuth exchange.
- `health_check()` probes `GET /_cluster/health` — lightweight, returns even on yellow/red status (which the gateway surfaces as DEGRADED rather than OFFLINE).
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Index → NormalizedDocument

| NormalizedDocument | Elasticsearch JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{index_name}"` | tenant-scoped |
| `source_id` | `index_name` | Index name is its own UUID |
| `title` | `index_name` | |
| `content` | summary string with health, docs.count, store.size | |
| `content_type` | `"text"` | |
| `source` | `"elasticsearch.index"` | |
| `metadata` | `{health, status, uuid, pri, rep, docs_count, docs_deleted, store_size, kind: "elasticsearch.index"}` | |

(Documents inside indices are NOT normalised by sync — Elasticsearch is a *destination* for the rest of the KB, not a source. `sync()` produces an *inventory* of indices for visibility.)

### 4.2 Wire-level dataclasses (`models.py`)

camelCase `@property` shims mirror Elasticsearch's mixed snake_case + leading-underscore wire format:

- `ClusterHealth` — `/_cluster/health` envelope
- `IndexInfo` — `/_cat/indices` row
- `IndexDocumentResponse` — `index_document` response
- `SearchHit`, `SearchResponse` — `_search` shape
- `BulkResponse` — `_bulk` summary

The connector boundary returns raw dicts; these dataclasses are for typed-construction in tests + helpers.

## 5. Key API Endpoints & Methods

Every method listed in `metadata/connector.json::apis` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | `GET /` | Validate config; verify reachability. |
| `health_check()` | GET | `/_cluster/health` | Returns even on yellow/red. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates `/_cat/indices` | Calls `ingest_document` per index. |
| `get_cluster_health(level)` | GET | `/_cluster/health?level={level}` | level = cluster/indices/shards |
| `list_indices(index_pattern, format)` | GET | `/_cat/indices/{pattern}?format={fmt}` | format=json by default |
| `get_index(index)` | GET | `/{index}` | Full index settings + mappings + aliases |
| `create_index(index, settings, mappings)` | PUT | `/{index}` | |
| `delete_index(index)` | DELETE | `/{index}` | Irreversible |
| `index_document(index, document, doc_id, refresh)` | POST/PUT | `/{index}/_doc[/{id}]` | Auto-id when `doc_id` blank |
| `get_document(index, doc_id)` | GET | `/{index}/_doc/{id}` | |
| `update_document(index, doc_id, doc, doc_as_upsert)` | POST | `/{index}/_update/{id}` | |
| `delete_document(index, doc_id)` | DELETE | `/{index}/_doc/{id}` | |
| `search(index, query, size, from_, sort, aggs)` | POST | `/{index}/_search` | Full DSL |
| `count(index, query)` | POST | `/{index}/_count` | |
| `bulk(operations)` | POST | `/_bulk` | NDJSON body |
| `get_mapping(index)` | GET | `/{index}/_mapping` | |
| `put_mapping(index, properties)` | PUT | `/{index}/_mapping` | |
| `list_aliases(name)` | GET | `/_cat/aliases[/{name}]?format=json` | |
| `list_snapshots(repository)` | GET | `/_snapshot/{repository}/_all` | |

## 6. Error Handling

| HTTP | Elasticsearch meaning | Mapped to |
|---|---|---|
| 400 | Bad request — malformed DSL, illegal_argument_exception | `ElasticsearchError` (raise) |
| 401 | Missing/invalid auth header | `ElasticsearchAuthError` → `AuthStatus.TOKEN_EXPIRED` |
| 403 | Authenticated but lacks cluster privilege | `ElasticsearchAuthError` → `AuthStatus.INVALID_CREDENTIALS` |
| 404 | index_not_found / document missing | `ElasticsearchNotFound` (raise) |
| 409 | Version conflict (optimistic concurrency) | `ElasticsearchError` |
| 429 | `circuit_breaking_exception` / search-rate cap | `ElasticsearchRateLimitError` → retry |
| 5xx | Node out / shard relocation in progress | `ElasticsearchNetworkError` → retry |

All in `exceptions.py` extending `ElasticsearchError`. Retry in `helpers/utils.py::with_retry` honours `max_retries=3`, exponential backoff `RETRY_DELAY_S * BACKOFF_FACTOR ** attempt`, jittered.

## 7. Dependencies

Connector-specific dependencies in `requirements.txt`:

```
httpx>=0.27.0
pytest>=7
pytest-asyncio>=0.23
respx>=0.21
```

(structlog, pytest-mock are pre-installed by the platform venv.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `base_url` | string | yes | install_field | Per-tenant cluster URL — Elastic Cloud or self-hosted. **Alias accepted: `host`.** |
| `api_key` | secret | no | install_field | Encoded Kibana API key. Optional for anonymous self-hosted clusters; required for Elastic Cloud. |
| `username` | string | no | install_field | HTTP Basic — fallback when API keys unavailable |
| `password` | secret | no | install_field | Required with `username` |
| `verify_ssl` | boolean | no (default true) | install_field | Disable only for self-signed dev clusters |
| `rate_limit_per_min` | number | no (default 600) | install_field | Soft client-side cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["base_url"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers, handles NDJSON, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions`, `helpers.utils` |
| `helpers/normalizer.py` | Maps raw Elasticsearch payloads → `NormalizedDocument` (index inventory). | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Auth header builder, NDJSON serializer, retry helper. | `httpx`, `structlog`, `exceptions` |
| `models.py` | Dataclass schemas with camelCase property shims. | (stdlib) |
| `exceptions.py` | `ElasticsearchError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `ElasticsearchConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities (auth, retry, NDJSON) in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` only ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` — never hardcoded ✓
9. Features (retry, NDJSON) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
