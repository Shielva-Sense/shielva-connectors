# YouTrack Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.
> Mirrors the Wix / Bandwidth gold-standard implementation_plan layout.

## 1. Overview

**YouTrack** is JetBrains' issue-tracking and agile project-management product, exposing a REST API at `/{instance}/api`. Cloud instances live under `https://{instance}.youtrack.cloud/api`; self-hosted instances expose the same surface at `https://youtrack.example.com/api`. Because the host part is per-tenant, the connector accepts the full **`base_url`** as a required install_field — there is no single provider-wide host to hardcode.

This connector — `YouTrackConnector` (`CONNECTOR_TYPE = "youtrack"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a YouTrack instance:

| Surface         | Base path                                  | Capability                                              |
|-----------------|--------------------------------------------|---------------------------------------------------------|
| Users           | `/users` + `/users/me`                     | Current user, list users, get user                      |
| Projects        | `/admin/projects`                          | List projects, get a single project                     |
| Issues          | `/issues`                                  | List, get, create, update, delete issues                |
| Comments        | `/issues/{id}/comments`                    | List, add comments                                      |
| Tags            | `/issueTags`                               | List tags                                               |
| Time Tracking   | `/issues/{id}/timeTracking/workItems`      | List logged work items                                  |
| Agile Boards    | `/agiles`                                  | List boards                                             |
| Sprints         | `/agiles/{agileId}/sprints`                | List sprints on a board                                 |
| Articles (KB)   | `/articles`                                | List knowledge-base articles                            |

The connector normalises issues into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::YouTrackHTTPClient`).

## 2. SDK / Package Selection

| Package      | Version       | Justification                                                       |
|--------------|---------------|---------------------------------------------------------------------|
| `httpx`      | `>=0.27,<1.0` | Async client; pre-installed in shared venv                          |
| `structlog`  | `>=24.1`      | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed                |
| `tenacity`   | `>=8.2`       | Retry decorator for transient 429 / 5xx escapes from the HTTP layer |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

YouTrack REST API uses **permanent-token API key authentication** for server-to-server integrations.

### Credentials
- `base_url` — full YouTrack API base URL ending in `/api`. install_field (type `string`, required). Per-tenant. Examples: `https://yourorg.youtrack.cloud/api`, `https://youtrack.example.com/api`.
- `permanent_token` — Permanent token created in **Profile → Account Security → Permanent Tokens**. Format `perm:<base64>.<base64>.<hex>`. install_field (type `secret`, required).
- `default_project_id` — Optional default project to use for `sync()` and as a fallback in `create_issue`. install_field (type `string`, optional).
- `rate_limit_per_min` — Soft client-side cap; install_field (type `number`, optional, default 200).

### Header contract
Every request to `{base_url}/*`:

```
Authorization: Bearer <permanent_token>     ← permanent token, starts with "perm:"
Accept:        application/json
Content-Type:  application/json
```

### Lifecycle
- `install()` — validate `base_url` + `permanent_token` are non-empty, then probe `GET /users/me?fields=login` to verify the token is accepted.
- `authorize()` — returns a synthetic `TokenInfo` whose `access_token` is the permanent token; no exchange happens.
- `health_check()` — `GET /users/me?fields=login` is the canonical heartbeat.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Issue → NormalizedDocument

| NormalizedDocument | YouTrack JSON                        | Notes                                                    |
|--------------------|--------------------------------------|----------------------------------------------------------|
| `id`               | `f"{tenant_id}_{issue['id']}"`       | tenant-scoped per hard constraint                        |
| `source_id`        | `issue["id"]`                        | YouTrack internal id (e.g. `2-15`)                       |
| `title`            | `f"[{idReadable}] {summary}"`        | Human-readable                                           |
| `content`          | `issue["description"]`               | Markdown / plain text                                    |
| `source_url`       | `{instance}/issue/{idReadable}`      | Derived by `helpers.utils.issue_web_url`                 |
| `author`           | `issue["reporter"]["login"]`         | Reporter login                                           |
| `created_at`       | `issue["created"]` (ms epoch)        | Converted via `_ts_to_dt`                                |
| `updated_at`       | `issue["updated"]` (ms epoch)        |                                                          |
| `source`           | `"youtrack"`                         |                                                          |
| `metadata`         | `{id_readable, priority, state, assignee, custom_fields}` | Common custom-field values lifted              |

### 4.2 Project (raw)

Returned verbatim from `/admin/projects` — `{id, shortName, name, description, archived, leader}`.

### 4.3 Comment / WorkItem / Tag / Board / Sprint / Article

All return the raw YouTrack JSON shape — these are read-only or admin surfaces that the consumer (ARC / ACP) wires to its own UI.

## 5. Key API Endpoints & Methods

Every method below MUST exist as a standalone public `async def` in `connector.py`.

| Method                                              | HTTP   | Path                                                                | Notes                                              |
|-----------------------------------------------------|--------|---------------------------------------------------------------------|----------------------------------------------------|
| `install()`                                         | —      | n/a                                                                 | Validate config; probe `/users/me`.                |
| `health_check()`                                    | GET    | `/users/me?fields=login`                                            | Lightweight token probe.                           |
| `sync(since, full, kb_id, webhook_url)`             | —      | paginates `/issues`                                                 | Calls `ingest_document`.                           |
| `get_current_user()`                                | GET    | `/users/me`                                                         |                                                    |
| `list_users(query, skip, top, fields)`              | GET    | `/users`                                                            | `query`, `$skip`, `$top`, `fields`.                |
| `get_user(user_id, fields)`                         | GET    | `/users/{userId}`                                                   |                                                    |
| `list_projects(skip, top, fields)`                  | GET    | `/admin/projects`                                                   |                                                    |
| `get_project(project_id, fields)`                   | GET    | `/admin/projects/{projectId}`                                       |                                                    |
| `list_issues(query, skip, top, fields)`             | GET    | `/issues?query=...&$skip=&$top=&fields=`                            | YouTrack query language.                           |
| `get_issue(issue_id, fields)`                       | GET    | `/issues/{issueId}`                                                 |                                                    |
| `create_issue(project_id, summary, description, custom_fields, fields)` | POST | `/issues`                            | Body: `{project: {id}, summary, description, customFields}`.|
| `update_issue(issue_id, summary, description, custom_fields)` | POST | `/issues/{issueId}`                          | Only-supplied fields are sent.                     |
| `delete_issue(issue_id)`                            | DELETE | `/issues/{issueId}`                                                 |                                                    |
| `add_comment(issue_id, text)`                       | POST   | `/issues/{issueId}/comments`                                        | Body: `{text}`.                                    |
| `list_comments(issue_id, skip, top, fields)`        | GET    | `/issues/{issueId}/comments`                                        |                                                    |
| `list_tags(skip, top, fields)`                      | GET    | `/issueTags`                                                        | Account-wide tag registry.                         |
| `list_time_tracking(issue_id, skip, top)`           | GET    | `/issues/{issueId}/timeTracking/workItems`                          |                                                    |
| `list_boards(skip, top, fields)`                    | GET    | `/agiles`                                                           | Agile boards.                                      |
| `list_sprints(board_id, skip, top, fields)`         | GET    | `/agiles/{agileId}/sprints`                                         |                                                    |
| `list_articles(query, skip, top, fields)`           | GET    | `/articles`                                                         | Knowledge-base articles.                           |

Wire convention: YouTrack uses **camelCase** in JSON (`idReadable`, `customFields`, `fullName`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | YouTrack meaning                                | Mapped to                                                                |
|------|-------------------------------------------------|--------------------------------------------------------------------------|
| 400  | Bad request                                     | `YouTrackBadRequestError` (raise)                                        |
| 401  | Token invalid / missing                         | `YouTrackAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403  | Token lacks permissions                         | `YouTrackAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404  | Resource not found                              | `YouTrackNotFound` (raise)                                               |
| 409  | Conflict (duplicate / state mismatch)           | `YouTrackConflictError`                                                  |
| 428  | Precondition (revision mismatch)                | `YouTrackPreconditionError`                                              |
| 429  | Rate limited                                    | `YouTrackRateLimitError` → `ConnectorHealth.DEGRADED` (retry w/ backoff) |
| 5xx  | Provider outage                                 | `YouTrackServerError` → retry w/ exponential backoff                     |

All in `exceptions.py` extending `YouTrackError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `min(RETRY_DELAY_S * BACKOFF_FACTOR ** attempt, MAX_RETRY_DELAY_S)`.

Back-compat alias: `YouTrackNetworkError = YouTrackServerError` (older callers expect this name).

## 7. Dependencies

```
httpx>=0.27,<1.0
structlog>=24.1
tenacity>=8.2
```

(`pytest`, `pytest-asyncio`, `pytest-mock`, `respx` are pre-installed.)

## 8. Config & Install Fields

| Key                  | Type    | Required | Source         | Notes                                                            |
|----------------------|---------|----------|----------------|------------------------------------------------------------------|
| `base_url`           | string  | yes      | install_field  | Full API URL ending in `/api` — e.g. `https://x.youtrack.cloud/api`. |
| `permanent_token`    | secret  | yes      | install_field  | Sent as `Authorization: Bearer ...`. Starts with `perm:`.        |
| `default_project_id` | string  | no       | install_field  | Default project for `sync()` and `create_issue` fallback.        |
| `rate_limit_per_min` | number  | no       | install_field  | Soft client-side cap. Default 200.                               |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["base_url", "permanent_token"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File                       | Responsibility                                                                                            | Imports                                                              |
|----------------------------|-----------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------|
| `connector.py`             | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.**               | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py`    | Single owner of httpx. Builds headers, retries on 429/5xx + transport errors, raises typed exceptions.    | `httpx`, `structlog`, `exceptions`                                   |
| `helpers/normalizer.py`    | Maps raw YouTrack issue payloads → `NormalizedDocument`.                                                  | `shared.base_connector.NormalizedDocument`, `helpers.utils`          |
| `helpers/utils.py`         | `normalize_base_url`, `issue_web_url`, `extract_field_value`, `with_retry`.                               | (stdlib only)                                                        |
| `models.py`                | Pydantic schemas with camelCase aliases for request bodies.                                               | `pydantic`                                                           |
| `exceptions.py`            | `YouTrackError` hierarchy.                                                                                | (stdlib)                                                             |
| `__init__.py`              | Re-export `YouTrackConnector`.                                                                            | `connector`                                                          |

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
