# Algolia Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Algolia** is a hosted search-as-a-service platform exposing a REST API at `https://{app_id}-dsn.algolia.net` (reads — DSN-routed) and `https://{app_id}.algolia.net` (writes). This connector — `AlgoliaConnector` (`CONNECTOR_TYPE = "algolia"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from an Algolia application:

| Surface | Base path | Capability |
|---|---|---|
| Indexes | `/1/indexes` | List, create-via-settings, clear, copy, delete |
| Objects | `/1/indexes/{name}/{id}` + `/batch` | Save, get, delete, partial-update single + bulk objects |
| Settings | `/1/indexes/{name}/settings` | Get + put-replace index settings |
| Synonyms | `/1/indexes/{name}/synonyms` | List + save synonyms (1-to-1, expand, alt-correction) |
| Rules | `/1/indexes/{name}/rules` | List + save merchandising rules |
| Browse | `/1/indexes/{name}/browse` | Cursor-paginated full-index export |
| Search | `/1/indexes/{name}/query` | Keyword search with filters + facets |
| Multi-search | `/1/indexes/*/queries` | Federated multi-index search in one round-trip |

The connector normalises Algolia objects fetched via `browse_index` into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), routes all HTTP through `client/http_client.py::AlgoliaHTTPClient`, and never embeds raw HTTP in `connector.py` (SOC).

### DSN routing for reads (Algolia-specific gotcha)

Algolia operates two distinct DNS pools:

- **Read pool — `<app_id>-dsn.algolia.net`** — geo-DNS routes the caller to the lowest-latency PoP. Used for `is_alive`, `list_indexes`, `get_settings`, `browse_index`, `search_index`, `multi_search`, `get_object`, `list_synonyms`, `list_rules`.
- **Write pool — `<app_id>.algolia.net`** — single primary endpoint. Used for `create_index_settings` (PUT settings), `set_settings`, `save_object`, `save_objects`, `delete_object`, `partial_update_object`, `clear_index`, `delete_index`, `save_synonym`, `save_rule`, `copy_index`.
- **Fallback ring — `<app_id>-{1,2,3}.algolianet.com`** — shuffled, used when the primary read or write host returns 5xx or transport error. This is a separate DNS zone so a `algolia.net` outage does not bring down the connector.

`build_read_hosts()` and `build_write_hosts()` in `helpers/utils.py` are the single owner of host rotation. `client/http_client.py::_request` iterates the rotation until a host returns a non-5xx response; 4xx errors bypass rotation (they are the same on every node).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Schemas for request bodies (Browse, Multi-search); pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

No additional connector-specific runtime deps — Algolia is a REST-only API with no Webhook signature scheme and no OAuth flow.

## 3. Auth Flow

Algolia REST API uses **API key authentication** sent as request headers (never query params).

### Credentials
- `app_id` — Algolia Application ID (uppercase alphanumeric, e.g. `LATENCY`). install_field (type `text`, required).
- `api_key` — Admin or scoped Algolia API key. install_field (type `secret`, required).

### Header contract
Every request to `https://{app_id}{-dsn,}.algolia.net/*`:

```
X-Algolia-Application-Id: <app_id>
X-Algolia-API-Key:        <api_key>
Content-Type:             application/json
Accept:                   application/json
```

### Lifecycle
- `install()` validates `app_id` + `api_key` are non-empty, then probes `GET /1/indexes` to verify credentials before persisting config.
- `authorize()` — NOT applicable (`api_key` flow has no exchange). Returns current `health_check()` state.
- `health_check()` — `GET /1/indexes` as a lightweight probe (Algolia does not expose a global `/isalive` for all plans; the `/1/indexes` endpoint is universal and counts only against the operations quota, not search quota).
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Browsed Object → NormalizedDocument

Algolia objects are arbitrary JSON dicts identified by `objectID`. The normalizer projects them into a `NormalizedDocument`:

| NormalizedDocument | Algolia JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{obj['objectID']}"` | tenant-scoped |
| `source_id` | `obj["objectID"]` | Algolia object identifier |
| `title` | `obj.get("title") or obj.get("name") or obj["objectID"]` | best-effort title heuristic |
| `content` | `obj.get("description") or obj.get("content") or json.dumps(obj)` | best-effort content |
| `content_type` | `"text"` | |
| `source` | `f"algolia.{index_name}"` | qualifies the index |
| `metadata` | `{objectID, index_name, kind: "algolia.object"}` + extra fields | full raw object preserved |

### 4.2 Index Info → NormalizedDocument (sync inventory)

For `sync()`'s index inventory mode:

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{idx['name']}"` |
| `source_id` | `idx["name"]` |
| `title` | `idx["name"]` |
| `content` | `f"Index {name}: {entries} entries"` |
| `source` | `"algolia.index"` |
| `metadata` | `{entries, dataSize, fileSize, lastBuildTimeS, primary}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; probe list_indexes; persist. |
| `health_check()` | GET | `/1/indexes` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates list_indexes + optional browse | Calls `ingest_document`. |
| `list_indexes()` | GET | `/1/indexes` | Returns `{items, nbPages}`. |
| `create_index_settings(index_name, settings)` | PUT | `/1/indexes/{name}/settings` | Algolia creates the index on first settings push. |
| `get_index_settings(index_name)` | GET | `/1/indexes/{name}/settings` | |
| `save_object(index_name, object_data, object_id=None)` | POST/PUT | `/1/indexes/{name}[/{id}]` | PUT when object_id given, POST otherwise. |
| `save_objects(index_name, objects, action="addObject")` | POST | `/1/indexes/{name}/batch` | Bulk indexing with `addObject` / `updateObject` / `partialUpdateObject` / `deleteObject`. |
| `get_object(index_name, object_id, attributes=None)` | GET | `/1/indexes/{name}/{id}` | Optional `attributes` query param. |
| `delete_object(index_name, object_id)` | DELETE | `/1/indexes/{name}/{id}` | |
| `partial_update_object(index_name, object_id, attributes, create_if_not_exists=True)` | POST | `/1/indexes/{name}/{id}/partial` | `?createIfNotExists=` boolean. |
| `browse_index(index_name, *, cursor=None, params=None)` | POST | `/1/indexes/{name}/browse` | Cursor pagination via response `cursor`. |
| `search_index(index_name, query, *, filters=None, hits_per_page=20, page=0)` | POST | `/1/indexes/{name}/query` | Single-index keyword search. |
| `multi_search(requests)` | POST | `/1/indexes/*/queries` | `requests=[{indexName, query, ...}]`. |
| `list_synonyms(index_name, *, query="", type=None, page=0, hits_per_page=100)` | POST | `/1/indexes/{name}/synonyms/search` | Algolia uses POST for synonym search. |
| `save_synonym(index_name, synonym_id, synonym, forward_to_replicas=False)` | PUT | `/1/indexes/{name}/synonyms/{id}` | Full upsert. |
| `list_rules(index_name, *, query="", page=0, hits_per_page=100)` | POST | `/1/indexes/{name}/rules/search` | |
| `save_rule(index_name, rule_id, rule, forward_to_replicas=False)` | PUT | `/1/indexes/{name}/rules/{id}` | Full upsert. |

Wire convention: Algolia uses **camelCase** in JSON (`objectID`, `nbHits`, `hitsPerPage`, `forwardToReplicas`, `createIfNotExists`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads — no auto-camelization to keep the connector zero-loss.

## 6. Error Handling

| HTTP | Algolia meaning | Mapped to |
|---|---|---|
| 400 | Bad request (malformed body, invalid filter syntax) | `AlgoliaBadRequestError` (raise) |
| 401 | API key invalid / missing | `AlgoliaAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.DEGRADED` |
| 403 | Forbidden (key scoped to a different surface / index) | `AlgoliaAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Index / object / synonym / rule not found | `AlgoliaNotFound` (raise) |
| 422 | Validation error | `AlgoliaError` |
| 429 | Rate limited (Algolia counts operations + searches separately per plan) | `AlgoliaRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage on this host | host rotation falls through; final exhaustion raises `AlgoliaNetworkError` |

All in `exceptions.py` extending `AlgoliaError`. Retry in `client/http_client.py::_request` is host-rotation only (no per-host exponential backoff — Algolia recommends a different host as the recovery action, not a longer wait). Higher-level `with_retry` in `helpers/utils.py` retries `AlgoliaRateLimitError` and `AlgoliaNetworkError` with exponential backoff.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# No connector-specific deps. httpx + pydantic + structlog + pytest stack
# are pre-installed in the shared venv.
```

`requirements.txt` enumerates the runtime stack for hermetic install but adds no third-party libraries beyond the shared baseline.

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `app_id` | text | yes | install_field | `X-Algolia-Application-Id` header |
| `api_key` | secret | yes | install_field | `X-Algolia-API-Key` header |
| `default_index` | text | no | install_field | Default index name for shorthand calls |
| `timeout_s` | number | no | install_field (default 30) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["app_id", "api_key"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers, host rotation, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions`, `helpers.utils` |
| `helpers/normalizer.py` | Maps raw Algolia payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Host rotation builders, with_retry helper. | `httpx`, `structlog`, `exceptions` |
| `models.py` | Dataclass schemas for the most common Algolia response shapes. | (stdlib) |
| `exceptions.py` | `AlgoliaError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `AlgoliaConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities (host rotation, retry) in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, host rotation) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
