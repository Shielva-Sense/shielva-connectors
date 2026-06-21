# Shortcut Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Shortcut** (formerly Clubhouse) is a project-management SaaS for software teams. Its REST API v3 — `https://api.app.shortcut.com/api/v3` — exposes Stories, Epics, Iterations, Milestones, Projects, Workflows, Members, Groups (Teams), Labels, Files, and Webhooks. This connector — `ShortcutConnector` (`CONNECTOR_TYPE = "shortcut"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Shortcut workspace:

| Surface | Base path | Capability |
|---|---|---|
| Members | `/member`, `/members`, `/members/{id}` | Auth probe, list workspace members |
| Groups (Teams) | `/groups` | List teams |
| Workflows | `/workflows` | Discover `workflow_state_id` values |
| Projects | `/projects` | Legacy project registry |
| Iterations | `/iterations` | Sprint-like iterations |
| Milestones | `/milestones` | Roadmap milestones |
| Epics | `/epics`, `/epics/{id}` | List, get, create epics |
| Stories | `/search/stories`, `/stories`, `/stories/{id}` | List/search, get, create, update, delete |
| Labels | `/labels` | List + create workspace labels |
| Files | `/files` | Uploaded-file metadata |

The connector normalises **stories** and **epics** into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff that honours `Retry-After`, and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::ShortcutHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async HTTP client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`. No third-party SDK exists for Shortcut — the REST API is small enough to drive directly with httpx.

## 3. Auth Flow

Shortcut REST API uses **API token authentication** for server-to-server integrations.

### Credentials
- `api_token` — generated at **Shortcut → User Settings → API Tokens → Generate Token**. Stored as install_field (type `secret`, required). Inherits the issuing user's workspace permissions.

### Header contract
Every request to `https://api.app.shortcut.com/api/v3/*`:

```
Shortcut-Token: <api_token>
Content-Type:   application/json
Accept:         application/json
```

The token is sent in the `Shortcut-Token` header — **not** `Authorization`, **not** query-string. This is the entire auth model.

### Lifecycle
- `install()` — validates `api_token` is non-empty; persists config via `save_config`; does **not** call the API (per CONNECTOR_SYSTEM_PROMPT rule).
- `authorize()` — returns a `TokenInfo` wrapping `api_token` for ABI compatibility; no OAuth exchange.
- `health_check()` — `GET /member` (current member) as the lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Story -> NormalizedDocument

| NormalizedDocument | Shortcut JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{story['id']}"` | tenant-scoped |
| `source_id` | `str(story["id"])` | Shortcut story numeric ID |
| `title` | `story["name"]` | |
| `content` | `story["description"]` | Markdown |
| `content_type` | `"markdown"` | |
| `source_url` / `url` | `story["app_url"]` | |
| `author` | `str(story["requested_by_id"])` | UUID member id |
| `created_at` | `story["created_at"]` | RFC 3339 |
| `updated_at` | `story["updated_at"]` | RFC 3339 |
| `metadata` | `{story_type, workflow_state_id, project_id, epic_id, iteration_id, archived, owner_ids, labels (names only), estimate, app_url, kind: "shortcut.story"}` | None-valued keys dropped |

### 4.2 Epic -> NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{epic['id']}"` |
| `source_id` | `str(epic["id"])` |
| `title` | `epic["name"]` |
| `content` | `epic["description"]` |
| `content_type` | `"markdown"` |
| `metadata` | `{state, archived, owner_ids, started, completed, app_url, kind: "shortcut.epic"}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/member` | Lightweight auth probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates stories + epics -> `ingest_document` | Returns `SyncResult`. |
| `list_stories(query, page_size, next_token)` | POST | `/search/stories` | Search DSL + cursor pagination. |
| `get_story(story_id)` | GET | `/stories/{story-public-id}` | |
| `create_story(name, story_type, project_id, workflow_state_id, owner_ids, description, epic_id, iteration_id, estimate)` | POST | `/stories` | Falls back to `default_workflow_state_id` when omitted. |
| `update_story(story_id, fields)` | PUT | `/stories/{story-public-id}` | Patches mutable fields. |
| `delete_story(story_id)` | DELETE | `/stories/{story-public-id}` | 204 No Content. |
| `list_epics(includes_description)` | GET | `/epics?includes_description=true` | |
| `get_epic(epic_id)` | GET | `/epics/{epic-public-id}` | |
| `create_epic(name, description, state)` | POST | `/epics` | |
| `list_iterations()` | GET | `/iterations` | |
| `list_milestones()` | GET | `/milestones` | |
| `list_projects()` | GET | `/projects` | |
| `list_workflows()` | GET | `/workflows` | Discover `workflow_state_id` values. |
| `list_members()` | GET | `/members` | |
| `get_member(member_id=None)` | GET | `/members/{id}` or `/member` | When `member_id` is None, return the authenticated member. |
| `list_groups()` | GET | `/groups` | Teams. |
| `list_labels()` | GET | `/labels` | |
| `create_label(name, color)` | POST | `/labels` | |

Wire convention: Shortcut uses **snake_case** in JSON (`workflow_state_id`, `requested_by_id`, `created_at`). No aliasing needed; the connector boundary accepts/returns `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Shortcut meaning | Mapped to |
|---|---|---|
| 400 / 422 | Bad request / validation | `ShortcutBadRequestError` (raise) |
| 401 | Token invalid / missing header | `ShortcutAuthError` -> `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (token lacks scope) | `ShortcutAuthError` -> `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `ShortcutNotFoundError` (raise) |
| 409 | Conflict (duplicate / state mismatch) | `ShortcutConflictError` |
| 429 | Rate limited — `Retry-After` header honoured | `ShortcutRateLimitError` -> retry in client; `ConnectorHealth.DEGRADED` at lifecycle boundary |
| 5xx | Provider outage | `ShortcutServerError` -> retry with exponential backoff (max 3, capped at 8 s) |

All in `exceptions.py` extending `ShortcutError`. Retry in `client/http_client.py::_request` honours `Retry-After` for 429 and uses `min(_BACKOFF_BASE * 2 ** attempt, _BACKOFF_MAX)` for 5xx. `_STATUS_MAP` on the connector class maps `{401, 403, 429}` -> `(ConnectorHealth, AuthStatus)` for the `_classify_failure` helper.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
(none — all required packages are pre-installed in the shared connectors venv)
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_token` | secret | yes | install_field | `Shortcut-Token` header value |
| `base_url` | text | no | install_field (default `https://api.app.shortcut.com/api/v3`) | Override for self-hosted / sandbox |
| `default_workflow_state_id` | number | no | install_field | Fallback `workflow_state_id` for `create_story` |
| `rate_limit_per_min` | number | no | install_field (default 200) | Client-side soft cap |

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

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Shortcut payloads -> `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry`, `parse_iso8601`, `safe_get`. | (stdlib only) |
| `models.py` | Pydantic schemas (documentation + tooling only). | `pydantic` |
| `exceptions.py` | `ShortcutError` hierarchy. | (stdlib) |
| `__init__.py` | Self-bootstrap sys.path (drata pattern) + re-export `ShortcutConnector`. | `connector` |

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
