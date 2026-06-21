# Attio Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Attio** is a modern, customisable CRM (people, companies, deals, custom objects) exposing a REST API under `https://api.attio.com/v2`. This connector — `AttioConnector` (`CONNECTOR_TYPE = "attio"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from an Attio workspace:

| Surface | Base path | Capability |
|---|---|---|
| Workspaces | `/self` | Identify the workspace the token belongs to |
| Objects | `/objects` | List object schemas (people, companies, deals, custom) |
| Records | `/objects/{slug}/records` | List / get / create / update / assert / delete records |
| Attributes | `/objects/{slug}/attributes` | List / read attribute schemas |
| Lists | `/lists` | List Attio lists + their entries |
| Notes | `/notes` | List + create notes attached to records |
| Tasks | `/tasks` | List + create tasks |
| Threads | `/threads` | Read email / chat threads |

The connector normalises records + notes + tasks into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::AttioHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry helper for 429/5xx escapes from the HTTP client |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Attio REST API supports both an **API access token** (long-lived, server-to-server) and full OAuth2 authorization code. This connector uses the **API access token** path (`AUTH_TYPE = "api_key"`) — the simplest, no-refresh model. The token is sent as `Authorization: Bearer <token>` on every request.

### Credentials
- `api_key` — Attio access token created in **Workspace settings → Developers → Access tokens → Create token**. Stored as install_field (type `secret`, required).
- `workspace_slug` — Optional workspace slug; used for documentation/UI display only. Attio scopes every token to a single workspace automatically.
- `base_url` — Optional override of `https://api.attio.com/v2` (sandbox / proxy).
- `rate_limit_per_min` — Optional client-side soft cap (default 100).

### Header contract
Every request to `https://api.attio.com/v2/*`:

```
Authorization: Bearer <api_key>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `api_key` is non-empty. Does **not** call the API.
- `authorize()` — Returns a `TokenInfo` whose `access_token` is the configured api_key (no exchange).
- `health_check()` — `GET /self` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).
- `sync()` — Iterates configured `sync_objects` (default `["people", "companies"]`), normalises each record, batch-ingests.

## 4. Data Model

### 4.1 Record → NormalizedDocument

| NormalizedDocument | Attio JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{record_id}"` | tenant-scoped |
| `source_id` | `record["id"]["record_id"]` or `record["id"]` | Attio record UUID |
| `title` | derived (name / domain / title attribute) | best-effort |
| `content` | flattened "key: value" string of `values` | searchable text |
| `source` | `f"attio.{object_slug}"` | |
| `created_at` | `record["created_at"]` | RFC 3339 |
| `updated_at` | `record["updated_at"] or record["last_modified_at"]` | |
| `metadata` | `{object_slug, values, kind: "attio.record"}` | |

### 4.2 Note → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{note['id']['note_id']}"` |
| `source_id` | `note["id"]["note_id"]` |
| `title` | `note["title"]` |
| `content` | `note["content_plaintext"]` or `note["content_markdown"]` |
| `source` | `"attio.note"` |
| `created_at` | `note["created_at"]` |

### 4.3 Task → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{task['id']['task_id']}"` |
| `source_id` | `task["id"]["task_id"]` |
| `title` | derived from `content_plaintext` (first line) |
| `content` | `task["content_plaintext"]` |
| `metadata` | `{is_completed, deadline_at, linked_records}` |

## 5. Key API Endpoints & Methods

Every method below exists as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/self` | Lightweight workspace probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates records of configured object slugs | Calls `ingest_batch`. |
| `list_workspaces()` | GET | `/self` | Returns the single-workspace envelope `{workspaces:[…]}`. |
| `list_objects()` | GET | `/objects` | All object schemas. |
| `list_attributes(object_slug)` | GET | `/objects/{slug}/attributes` | Attribute schemas. |
| `get_attribute(object_slug, attribute_id)` | GET | `/objects/{slug}/attributes/{id}` | One attribute. |
| `list_records(object_slug, *, limit=50, offset=0, filter=None, sorts=None)` | POST | `/objects/{slug}/records/query` | Cursor-style + offset paging. |
| `get_record(object_slug, record_id)` | GET | `/objects/{slug}/records/{id}` | |
| `create_record(object_slug, values)` | POST | `/objects/{slug}/records` | Body: `{data:{values:{…}}}`. |
| `update_record(object_slug, record_id, values)` | PATCH | `/objects/{slug}/records/{id}` | |
| `assert_record(object_slug, matching_attribute, values)` | PUT | `/objects/{slug}/records?matching_attribute={attr}` | Upsert by matching attribute. |
| `delete_record(object_slug, record_id)` | DELETE | `/objects/{slug}/records/{id}` | |
| `list_lists()` | GET | `/lists` | All lists. |
| `get_list(list_id)` | GET | `/lists/{id}` | |
| `list_list_entries(list_id, *, limit=50, offset=0)` | POST | `/lists/{id}/entries/query` | |
| `list_notes(*, parent_object=None, parent_record_id=None, limit=50, offset=0)` | GET | `/notes` | Optional record scope. |
| `create_note(parent_object, parent_record_id, title, content, format="plaintext")` | POST | `/notes` | |
| `list_tasks(*, limit=50, offset=0)` | GET | `/tasks` | |
| `create_task(content, format="plaintext", deadline_at=None, assignees=None, linked_records=None)` | POST | `/tasks` | |

Wire convention: Attio uses **snake_case** in JSON (`record_id`, `parent_object`, `last_modified_at`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Attio meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `AttioBadRequestError` (raise) |
| 401 | Access token invalid / missing | `AttioAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (token lacks scope) | `AttioAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `AttioNotFoundError` (raise) |
| 409 | Conflict | `AttioConflictError` |
| 429 | Rate limited (`Retry-After` header) | `AttioRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `AttioServerError` → retry with exponential backoff |

All in `exceptions.py` extending `AttioError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt`.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | Attio access token; sent as `Authorization: Bearer <key>` |
| `workspace_slug` | text | no | install_field | Display-only label |
| `base_url` | text | no | install_field (default `https://api.attio.com/v2`) | Sandbox / proxy override |
| `sync_objects` | text | no | install_field (default `people,companies`) | Comma-separated list of object slugs to ingest in `sync()` |
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
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Attio payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry helper, dict path safe-get. | (stdlib only) |
| `models.py` | Pydantic schemas with snake_case aliases for request bodies. | `pydantic` |
| `exceptions.py` | `AttioError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `AttioConnector`. | `connector` |

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
