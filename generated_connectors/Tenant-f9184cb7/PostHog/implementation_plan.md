# PostHog Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**PostHog** is an open-source product analytics + feature-flag + session-replay platform exposing a REST API under `https://app.posthog.com` (or `https://us.posthog.com` / `https://eu.posthog.com` for cloud, or any self-hosted host). This connector — `PostHogConnector` (`CONNECTOR_TYPE = "posthog"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs:

| Surface | Base path | Capability |
|---|---|---|
| Projects | `/api/projects` | List + read projects under the personal key |
| Events (capture) | `/capture/` `/batch/` | Send single + batched events with `api_key` in body (project key) |
| Persons | `/api/projects/{id}/persons` | Identify, list, query users; merge / alias |
| Cohorts | `/api/projects/{id}/cohorts` | List + read user cohorts |
| Feature Flags | `/api/projects/{id}/feature_flags` | CRUD on feature flags, decide responses |
| Insights | `/api/projects/{id}/insights` | Saved trends / funnels / retention charts |
| Dashboards | `/api/projects/{id}/dashboards` | Dashboard listing + detail |
| Actions | `/api/projects/{id}/actions` | Saved action definitions |
| Annotations | `/api/projects/{id}/annotations` | Timeline annotations |
| Experiments | `/api/projects/{id}/experiments` | A/B experiments |
| Query (HogQL) | `/api/projects/{id}/query` | Arbitrary HogQL queries against events |

The connector normalises persons + events into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff honoring `Retry-After` (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::PostHogHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `pytest` / `pytest-asyncio` / `pytest-mock` / `respx` | latest | Pre-installed test stack |

No PostHog Python SDK is used — the official `posthog` library is a thin wrapper over the same REST endpoints and adds unwanted background threads that the gateway already owns at a higher layer.

## 3. Auth Flow

PostHog uses **two distinct API key types**. The connector treats them as two independent auth channels — exactly one of them is required per call.

### Credentials

- `personal_api_key` — `phx_…` — issued from **Personal Settings → Personal API Keys**. Read/write access to **management** endpoints (projects, persons, flags, cohorts, insights, queries). Sent as `Authorization: Bearer <personal_api_key>`. install_field (type `secret`, required).
- `project_id` — numeric ID of the default project. Used as a path-segment fallback for methods that omit `project_id`. install_field (type `string`, required).
- `project_api_key` — `phc_…` — issued from **Project Settings → Project API Key**. **Capture only**. Sent **in the JSON body** as `api_key`, never as a header. install_field (type `secret`, optional — required only if the tenant intends to call `capture_event` / `batch_capture` / `identify`).
- `base_url` — host override. Defaults to `https://app.posthog.com`. install_field (type `string`, optional).

### Header contract

Management API request:

```
Authorization: Bearer <personal_api_key>
Content-Type:  application/json
Accept:        application/json
```

Capture API request:

```
Content-Type:  application/json
Accept:        application/json
# Authorization header omitted on purpose
# api_key lives in the JSON body
```

### Lifecycle

- `install()` validates `personal_api_key` + `project_id` are non-empty. Does **not** call the API.
- `authorize()` — NOT implemented (`api_key` flow has no exchange). Returns a stub `TokenInfo`.
- `health_check()` — `GET /api/projects/{project_id}` as a lightweight probe (verifies both the personal key and that the project is reachable).
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Person → NormalizedDocument

| NormalizedDocument | PostHog JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{person['id']}"` | tenant-scoped |
| `source_id` | `person["id"]` (PostHog person UUID) | |
| `title` | first distinct_id, or person id | |
| `content` | `json.dumps(person.get("properties", {}))` | |
| `source` | `"posthog.persons"` | |
| `created_at` | `person["created_at"]` (RFC 3339) | |
| `metadata` | `{distinct_ids, name, email, is_identified}` | |

### 4.2 Event → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{event['id']}"` |
| `source_id` | `event["id"]` |
| `title` | `event["event"]` (event name) |
| `content` | distinct_id + properties summary |
| `source` | `"posthog.events"` |
| `created_at` | `event["timestamp"]` |
| `metadata` | `{distinct_id, event_name, properties}` |

## 5. Key API Endpoints & Methods

Every method listed below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/api/projects/{project_id}` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates persons + events | Calls `ingest_document`. |
| `capture_event(distinct_id, event, properties, timestamp, project_id?)` | POST | `/capture/` | `api_key` in body. |
| `batch_capture(events, project_id?)` | POST | `/batch/` | `api_key` at top level. |
| `identify_person(distinct_id, properties, project_id?)` | POST | `/capture/` | event = `$identify`, props go in `$set`. |
| `alias_distinct_ids(distinct_id, alias, project_id?)` | POST | `/capture/` | event = `$create_alias`. |
| `list_persons(project_id?, search?, limit?)` | GET | `/api/projects/{id}/persons` | |
| `get_person(person_id, project_id?)` | GET | `/api/projects/{id}/persons/{person_id}` | |
| `list_cohorts(project_id?)` | GET | `/api/projects/{id}/cohorts` | |
| `get_cohort(cohort_id, project_id?)` | GET | `/api/projects/{id}/cohorts/{cohort_id}` | |
| `list_feature_flags(project_id?)` | GET | `/api/projects/{id}/feature_flags` | |
| `get_feature_flag(flag_id, project_id?)` | GET | `/api/projects/{id}/feature_flags/{flag_id}` | |
| `create_feature_flag(key, name, filters?, active?, project_id?)` | POST | `/api/projects/{id}/feature_flags` | Body `{key, name, active, filters}`. |
| `list_insights(project_id?)` | GET | `/api/projects/{id}/insights` | |
| `list_dashboards(project_id?)` | GET | `/api/projects/{id}/dashboards` | |
| `get_dashboard(dashboard_id, project_id?)` | GET | `/api/projects/{id}/dashboards/{dashboard_id}` | |
| `list_actions(project_id?)` | GET | `/api/projects/{id}/actions` | |
| `list_annotations(project_id?)` | GET | `/api/projects/{id}/annotations` | |
| `list_experiments(project_id?)` | GET | `/api/projects/{id}/experiments` | |
| `list_events(project_id?, after?, before?, limit?)` | GET | `/api/projects/{id}/events` | |
| `run_query(query, project_id?)` | POST | `/api/projects/{id}/query` | Body `{query: {…}}`. |
| `list_projects()` | GET | `/api/projects` | |

Wire convention: PostHog returns `snake_case` JSON. The connector boundary passes-through `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | PostHog meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `PostHogBadRequestError` (raise) |
| 401 | Personal API key invalid / missing | `PostHogAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (key lacks scope, or no permission on project) | `PostHogAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Project / flag / cohort not found | `PostHogNotFoundError` (raise) |
| 409 | Conflict (duplicate feature flag key) | `PostHogConflictError` |
| 429 | Rate limited (PostHog returns `Retry-After`) | `PostHogRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `PostHogServerError` → retry with exponential backoff |

All in `exceptions.py` extending `PostHogError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `min(2 ** attempt, 8)` for 5xx, `Retry-After` (or fallback exponential) for 429.

## 7. Dependencies

Packages to install in the connector's venv (`install_deps` reads this section):

```
httpx>=0.27
pydantic>=2.0
structlog>=24.1
```

(pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `personal_api_key` | secret | yes | install_field | `Authorization: Bearer …` for management API |
| `project_id` | string | yes | install_field | Numeric project id (as string in install form) |
| `project_api_key` | secret | no | install_field | Body `api_key` for `/capture/` + `/batch/` |
| `base_url` | string | no | install_field | Defaults to `https://app.posthog.com` |
| `rate_limit_per_min` | number | no | install_field | Soft cap default 240 |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["personal_api_key", "project_id"]
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
| `client/http_client.py` | Single owner of httpx. Builds bearer + capture headers, retries with `Retry-After`, raises typed exceptions. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw PostHog payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry` wrapper + safe nested-dict accessors. | (stdlib only) |
| `models.py` | Pydantic schemas with snake_case fields for request bodies. | `pydantic` |
| `exceptions.py` | `PostHogError` hierarchy. | (stdlib) |
| `__init__.py` | self-bootstrap sys.path then re-export `PostHogConnector`. | `connector` |

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
