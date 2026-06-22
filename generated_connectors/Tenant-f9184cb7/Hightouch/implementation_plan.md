# Hightouch Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Hightouch** is a Reverse-ETL / Composable CDP platform that activates data
from the warehouse into hundreds of SaaS destinations. It exposes a single
REST surface rooted at `https://api.hightouch.com/api/v1`. This connector —
`HightouchConnector` (`CONNECTOR_TYPE = "hightouch"`, `AUTH_TYPE = "api_key"`) —
wraps the operational surfaces a Shielva tenant typically needs:

| Surface       | Base path                  | Capability                                                        |
|---------------|----------------------------|-------------------------------------------------------------------|
| Workspaces    | `/workspaces`              | List workspaces the API token can see                             |
| Sources       | `/sources`                 | List + read warehouse / DB sources                                |
| Models        | `/models`                  | List + read SQL / audience definitions                            |
| Destinations  | `/destinations`            | List + read SaaS destinations                                     |
| Syncs         | `/syncs`                   | List + read + create + trigger reverse-ETL pipelines              |
| Sync Runs     | `/syncs/{id}/runs`         | List + read individual run history                                |
| Sequences     | `/sequences`               | List orchestrated multi-step sync sequences                       |
| Events        | `/events`                  | Forward customer events into Hightouch (Customer Studio / CDP)    |

The connector normalises Sources + Models + Destinations + Syncs into
`NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), exposes one
standalone `async def` per user-requested operation (OCP), retries 429 / 5xx
with exponential backoff + jitter (3 attempts), and never embeds raw HTTP in
`connector.py` (SOC — all HTTP delegated to
`client/http_client.py::HightouchHTTPClient`).

## 2. SDK / Package Selection

| Package    | Version       | Justification                                                |
|------------|---------------|--------------------------------------------------------------|
| `httpx`    | `>=0.27,<1.0` | Async client; pre-installed in shared venv                   |
| `structlog`| `>=24.1`      | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed         |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`,
`respx`. No third-party retry / JWT library is needed — the HTTP client
implements its own exponential backoff and Hightouch uses plain bearer auth.

## 3. Auth Flow

Hightouch uses a single **Personal/Workspace API token** for every endpoint.

### Credentials
- `api_token` — Hightouch API token. Created at
  **Hightouch → Settings → API Keys → Create API Key** (workspace-scoped) or
  **Settings → Personal Access Tokens** (user-scoped). Stored as install_field
  (type `secret`, **required**).
- `base_url` — Override of the Hightouch API base URL. install_field
  (type `string`, optional, default `https://api.hightouch.com/api/v1`).
- `rate_limit_per_min` — Soft client-side rate cap. install_field
  (type `number`, optional, default 60).

### Header contract
Every request to `https://api.hightouch.com/api/v1/*`:

```
Authorization: Bearer <api_token>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `api_token` is non-empty and persists install config.
  Does **not** call the API.
- `authorize()` — NOT exchanged (api_key flow). Returns a `TokenInfo` whose
  `access_token` is the configured `api_token` for ABI symmetry.
- `health_check()` — `GET /workspaces` as a lightweight, idempotent probe.
- `ensure_token()` — N/A (no token refresh lifecycle).

## 4. Data Model

### 4.1 Source → NormalizedDocument

| NormalizedDocument | Hightouch JSON                       | Notes                  |
|--------------------|--------------------------------------|------------------------|
| `id`               | `f"{tenant_id}_{source['id']}"`      | tenant-scoped          |
| `source_id`        | `source["id"]`                       | Hightouch source ID    |
| `title`            | `source["name"]`                     |                        |
| `content`          | concat of name + type + slug         |                        |
| `metadata`         | `{type, slug, kind:"hightouch.source"}` |                     |

### 4.2 Model → NormalizedDocument

| field          | from                                  |
|----------------|---------------------------------------|
| `id`           | `f"{tenant_id}_{model['id']}"`        |
| `source_id`    | `model["id"]`                         |
| `title`        | `model["name"]`                       |
| `metadata`     | `{slug, sourceId, primaryKey, kind:"hightouch.model"}` |

### 4.3 Destination → NormalizedDocument

| field      | from                                       |
|------------|--------------------------------------------|
| `id`       | `f"{tenant_id}_{dest['id']}"`              |
| `source_id`| `dest["id"]`                               |
| `title`    | `dest["name"]`                             |
| `metadata` | `{type, slug, kind:"hightouch.destination"}` |

### 4.4 Sync → NormalizedDocument

| field      | from                                       |
|------------|--------------------------------------------|
| `id`       | `f"{tenant_id}_{sync['id']}"`              |
| `source_id`| `sync["id"]`                               |
| `title`    | `sync["slug"]` or `f"Sync {sync['id']}"`   |
| `metadata` | `{modelId, destinationId, disabled, schedule, kind:"hightouch.sync"}` |

## 5. Key API Endpoints & Methods

Every method listed below MUST exist as a standalone public `async def` in
`connector.py`.

| Method                             | HTTP   | Path                                | Notes                                       |
|------------------------------------|--------|-------------------------------------|---------------------------------------------|
| `install()`                        | (lifecycle) | n/a                            | Validate config; no API call.               |
| `authorize(auth_code, state)`      | (lifecycle) | n/a                            | Returns TokenInfo(access_token=api_token).  |
| `health_check()`                   | GET    | `/workspaces`                       | Lightweight reachability probe.             |
| `sync(since, full, kb_id, …)`      | (lifecycle) | iterates sources + models + destinations + syncs | Calls `ingest_document`. |
| `list_workspaces()`                | GET    | `/workspaces`                       |                                             |
| `list_sources(page=1, per_page=50, slug=None)` | GET | `/sources`                | `page`/`per_page` query.                    |
| `get_source(source_id)`            | GET    | `/sources/{id}`                     |                                             |
| `list_models(page=1, per_page=50, slug=None)`  | GET | `/models`                 |                                             |
| `get_model(model_id)`              | GET    | `/models/{id}`                      |                                             |
| `list_destinations(page=1, per_page=50, slug=None)` | GET | `/destinations`      |                                             |
| `get_destination(destination_id)`  | GET    | `/destinations/{id}`                |                                             |
| `list_syncs(page=1, per_page=50, slug=None, model_id=None, destination_id=None)` | GET | `/syncs` | Filters via query params. |
| `get_sync(sync_id)`                | GET    | `/syncs/{id}`                       |                                             |
| `create_sync(payload)`             | POST   | `/syncs`                            | Body is the raw sync definition.            |
| `run_sync(sync_id, full_resync=False)` | POST | `/syncs/{id}/trigger`              | Body `{fullResync: bool}`.                  |
| `list_sync_runs(sync_id, page=1, per_page=50)` | GET | `/syncs/{id}/runs`         |                                             |
| `get_sync_run(sync_id, run_id)`    | GET    | `/syncs/{sid}/runs/{rid}`           |                                             |
| `list_sequences(page=1, per_page=50)` | GET | `/sequences`                        |                                             |
| `send_event(event)`                | POST   | `/events`                           | Body is the Hightouch event envelope.       |

Wire convention: Hightouch uses **camelCase** in JSON (`fullResync`,
`modelId`, `destinationId`, `primaryKey`, `slug`). The connector boundary
accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Hightouch meaning                  | Mapped to                                            |
|------|-------------------------------------|-----------------------------------------------------|
| 400  | Bad request                         | `HightouchBadRequestError` (raise)                  |
| 401  | API token invalid / missing         | `HightouchAuthError` → `TOKEN_EXPIRED` + `OFFLINE`  |
| 403  | Forbidden (token lacks scope)       | `HightouchAuthError` → `INVALID_CREDENTIALS`        |
| 404  | Not found                           | `HightouchNotFoundError` (raise)                    |
| 409  | Conflict (duplicate slug)           | `HightouchConflictError`                            |
| 429  | Rate limited                        | `HightouchRateLimitError` → `DEGRADED`              |
| 5xx  | Provider outage                     | `HightouchServerError` → retry exp backoff          |

All in `exceptions.py` extending `HightouchError`. Retry in
`client/http_client.py::_request` honours `max_retries=3`, exponential
backoff `RETRY_DELAY_S * BACKOFF_FACTOR ** attempt + jitter` (capped at
`MAX_RETRY_DELAY_S`) for both 429 and 5xx.

## 7. Dependencies

Packages to install (connector-specific only):

```
# (httpx, structlog, pytest, pytest-asyncio, pytest-mock, respx pre-installed)
```

No additional packages required. `requirements.txt` lists only `httpx` for
documentation purposes.

## 8. Config & Install Fields

| Key                  | Type   | Required | Source         | Notes                                                       |
|----------------------|--------|----------|----------------|-------------------------------------------------------------|
| `api_token`          | secret | **yes**  | install_field  | `Authorization: Bearer …`                                   |
| `base_url`           | text   | no       | install_field  | Default `https://api.hightouch.com/api/v1`                  |
| `rate_limit_per_min` | number | no       | install_field  | Soft client cap (default 60)                                |

In `connector.py`:

```python
REQUIRED_CONFIG_KEYS = ["api_token"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File                       | Responsibility                                                            | Imports                                                     |
|----------------------------|---------------------------------------------------------------------------|-------------------------------------------------------------|
| `connector.py`             | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py`    | Single owner of httpx. Builds Bearer headers, retries, raises typed exceptions. | `httpx`, `structlog`, `exceptions`                           |
| `helpers/normalizer.py`    | Maps raw Hightouch payloads → `NormalizedDocument`.                       | `shared.base_connector.NormalizedDocument`                  |
| `helpers/utils.py`         | Pagination helpers, ISO date parsing, `with_retry`.                       | (stdlib only)                                               |
| `models.py`                | Local dataclasses for typed handles on the most-used shapes.              | (stdlib)                                                    |
| `exceptions.py`            | `HightouchError` hierarchy.                                               | (stdlib)                                                    |
| `__init__.py`              | Re-export `HightouchConnector`.                                           | `connector`                                                 |

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
10. Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only ✓

**Score: 10/10.**
