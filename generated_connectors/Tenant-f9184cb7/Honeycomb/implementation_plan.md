# Honeycomb Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Honeycomb** is a hosted observability platform — store, query and alert on
high-cardinality structured events (traces, metrics, logs). This connector —
`HoneycombConnector` (`CONNECTOR_TYPE = "honeycomb"`, `AUTH_TYPE = "api_key"`)
— wraps the operational surfaces a Shielva tenant typically needs from a
Honeycomb environment:

| Surface | Base path | Capability |
|---|---|---|
| Auth | `/auth` | Probe team / environment / key scopes |
| Datasets | `/datasets` | List + read + create datasets |
| Columns | `/datasets/{slug}/columns` | Enumerate dataset schema |
| Queries | `/queries/{slug}` | Save / fetch query specs |
| Query Results | `/query_results/{slug}` | Run a query → poll for result |
| Markers | `/markers/{slug}` | Annotate dataset timeline (deploys, incidents) |
| Triggers | `/triggers/{slug}` | Threshold-based alerts |
| Boards | `/boards` | Dashboards over saved queries |
| SLOs | `/slos/{slug}` | Service Level Objectives |
| Recipients | `/recipients` | Notification destinations |
| Events ingest | `/events/{slug}` | Direct one-shot event row ingest |

The connector normalizes datasets (with their columns folded in) into
`NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone
`async def` methods per user-requested operation (OCP), retries 429/5xx with
exponential backoff + jitter honoring `Retry-After`, and never embeds raw
HTTP in `connector.py` (SOC — all HTTP delegated to
`client/http_client.py::HoneycombHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `pydantic` | `>=2.0` | Optional request/response schemas; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`,
`respx`.

No `tenacity` or `PyJWT` — Honeycomb has no OAuth webhook flow and the
in-house `with_retry` / `_request` retry loop is sufficient.

## 3. Auth Flow

Honeycomb uses **API key authentication** (no OAuth) — the api_key is sent in
the `X-Honeycomb-Team` header on every request. This is the canonical
Honeycomb-specific gotcha: the header is NOT `Authorization: Bearer ...`.
Anything that emits a Bearer Authorization header (gateway middleware, proxy
re-writes) must NOT clobber the team header.

### Credentials

- `api_key` — Environment-level Honeycomb API key generated at
  **Environment Settings → API Keys**. install_field (type `secret`,
  required). The key's scope is the source of truth for what the connector
  can do (read events, manage queries, manage triggers, etc.).
- `region` — `'us'` (default) or `'eu'`. install_field (type `string`,
  optional). Selects between `api.honeycomb.io/1` and
  `api.eu1.honeycomb.io/1`.
- `base_url` — explicit override. install_field (type `string`, optional).
- `default_dataset` — convenience default for callers that want to omit the
  dataset slug. install_field (type `string`, optional).
- `rate_limit_per_min` — client-side soft cap. install_field (type `number`,
  optional, default 100).

### Header contract

```
X-Honeycomb-Team: <api_key>          ← canonical Honeycomb header
Content-Type:     application/json
Accept:           application/json
```

### Lifecycle

- `install()` validates `api_key` is non-empty AND probes `GET /auth` to
  verify the key is accepted by Honeycomb. Persists config snapshot.
- `authorize()` — no consent flow; wraps the configured key into a long-lived
  synthetic `TokenInfo` for surface compatibility.
- `health_check()` — `GET /auth` lightweight probe.
- `on_token_refresh()` — no-op (api_key auth has no refresh).

## 4. Data Model

### 4.1 Dataset → NormalizedDocument

| NormalizedDocument | Honeycomb JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{dataset.slug}"` | tenant-scoped |
| `source_id` | `dataset["slug"]` | Honeycomb keys datasets by slug |
| `title` | `dataset["name"]` | Human display name |
| `content` | "Dataset: ...\nSlug: ...\nDescription: ...\nColumns: ..." | columns folded in |
| `source_url` / `url` | `https://ui.honeycomb.io/datasets/{slug}` | Deep-link |
| `author` | `"honeycomb"` | constant |
| `created_at` | `dataset["created_at"]` | RFC 3339 |
| `updated_at` | `dataset["last_written_at"]` | RFC 3339 |
| `metadata` | `{kind: "honeycomb.dataset", slug, regular_columns_count, column_count, expand_json_depth}` | |

### 4.2 Column → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{dataset_slug}:{key_name}"` |
| `title` | `f"{dataset_slug}.{key_name}"` |
| `metadata` | `{kind: "honeycomb.column", dataset, type, hidden}` |

### 4.3 Trigger / Marker → NormalizedDocument

Same pattern: `id = f"{tenant_id}_{dataset_slug}:trigger:{id}"` etc.

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector` MUST exist as a
standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client; probe `/auth`. |
| `authorize(auth_code, state)` | (lifecycle) | n/a | api_key → synthetic TokenInfo. |
| `health_check()` | GET | `/auth` | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates datasets + columns | Calls `ingest_document`. |
| `auth_info()` | GET | `/auth` | Returns team / environment / api_key_access. |
| `list_datasets()` | GET | `/datasets` | |
| `get_dataset(dataset_slug)` | GET | `/datasets/{slug}` | |
| `create_dataset(name, description, expand_json_depth)` | POST | `/datasets` | Body: `{name, description, expand_json_depth}`. |
| `list_columns(dataset_slug)` | GET | `/datasets/{slug}/columns` | |
| `list_queries(dataset_slug)` | GET | `/queries/{slug}` | |
| `create_query(dataset_slug, breakdowns, calculations, filters, time_range, granularity, orders, having)` | POST | `/queries/{slug}` | Returns `{id}`. |
| `get_query(dataset_slug, query_id)` | GET | `/queries/{slug}/{id}` | |
| `run_query(dataset_slug, query_id, disable_series, limit)` | POST | `/query_results/{slug}` | Returns `{id, complete}`; poll. |
| `get_query_result(dataset_slug, result_id)` | GET | `/query_results/{slug}/{rid}` | |
| `list_markers(dataset_slug)` | GET | `/markers/{slug}` | |
| `create_marker(dataset_slug, message, type, url, start_time, end_time)` | POST | `/markers/{slug}` | |
| `list_triggers(dataset_slug)` | GET | `/triggers/{slug}` | |
| `create_trigger(dataset_slug, name, query_id, threshold, frequency, alert_type, recipients)` | POST | `/triggers/{slug}` | |
| `list_boards()` | GET | `/boards` | |
| `get_board(board_id)` | GET | `/boards/{id}` | |
| `create_board(name, description, style, queries)` | POST | `/boards` | |
| `list_slos(dataset_slug)` | GET | `/slos/{slug}` | |
| `list_recipients()` | GET | `/recipients` | Email / Slack / Webhook / PagerDuty. |
| `send_event(dataset_slug, event)` | POST | `/events/{slug}` | One-shot ingest. |

Wire convention: Honeycomb uses **snake_case** in JSON (`api_key_access`,
`expand_json_depth`, `last_written_at`). The connector boundary returns these
as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Honeycomb meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `HoneycombBadRequestError` (raise) |
| 401 | API key invalid / missing header | `HoneycombAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden — key lacks scope | `HoneycombAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Resource not found | `HoneycombNotFoundError` (raise) |
| 409 | Conflict (duplicate dataset) | `HoneycombConflictError` |
| 429 | Rate limited | `HoneycombRateLimitError` — honors `Retry-After` header; retry up to 3 |
| 5xx | Provider outage | `HoneycombServerError` (aliased to `HoneycombNetworkError`) — retry up to 3 |
| Transport error | timeout / DNS | `HoneycombNetworkError` |

All in `exceptions.py` extending `HoneycombError`. Retry lives in
`client/http_client.py::_request` with exponential backoff +
jitter, honoring `Retry-After` when Honeycomb provides it.

`_STATUS_MAP` class const surfaces this classification:

```python
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 7. Dependencies

Packages to install in the connector's venv (`install_deps` reads this
section):

```
httpx>=0.27.0
```

(`pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`
are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `X-Honeycomb-Team` header value |
| `region` | string | no | install_field | `us` (default) or `eu` |
| `base_url` | string | no | install_field | Overrides region |
| `default_dataset` | string | no | install_field | Optional convenience default |
| `rate_limit_per_min` | number | no | install_field (default 100) | Client-side soft cap |

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
| `client/http_client.py` | Sole owner of httpx. Builds headers (`X-Honeycomb-Team`), retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Honeycomb payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `slugify`, `with_retry`, `safe_get`. | (stdlib + `structlog` + `exceptions`) |
| `models.py` | Pydantic schemas for type-checked construction of request bodies. | `pydantic` |
| `exceptions.py` | `HoneycombError` hierarchy + back-compat aliases. | (stdlib) |
| `__init__.py` | Self-bootstraps sys.path; re-exports `HoneycombConnector`. | `connector` |

SOC/OCP self-check:

1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is a standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, slug normalization) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
