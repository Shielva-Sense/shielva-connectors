# Snyk Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Snyk** is a developer-first security platform that scans source, dependencies, containers, and IaC for vulnerabilities and license risk. It exposes two HTTP surfaces:

- **REST v3** at `https://api.snyk.io/rest` — JSON:API style, every request requires `?version=YYYY-MM-DD~beta` (or a GA date). Content type `application/vnd.api+json`.
- **REST v1 (legacy)** at `https://api.snyk.io/v1` — plain JSON. Used for endpoints not yet ported to v3 (`/user/me`, dependencies, integrations, member listing, package-test).

This connector — `SnykConnector` (`CONNECTOR_TYPE = "snyk"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from Snyk:

| Surface | Base path | Capability |
|---|---|---|
| User (self) | `/v1/user/me` | Identify the API token holder; used as health-check probe |
| Organizations | `/rest/orgs`, `/v1/orgs` | List + read orgs the token can see |
| Org members | `/v1/org/{id}/members` | List users belonging to an organization |
| Projects | `/rest/orgs/{id}/projects` | List + read + delete scanned projects |
| Targets | `/rest/orgs/{id}/targets` | List + read scan targets (repo / image / SBOM source) |
| Issues | `/rest/orgs/{id}/issues` | List + read open vulnerability / license issues |
| Dependencies | `/v1/org/{id}/dependencies` | Legacy dependency graph search |
| Reporting | `/rest/orgs/{id}/audit_logs/search` | Org audit trail (kept lightweight: list only) |
| User settings | `/v1/user/me/notification-settings/org/{id}` | Per-org notification settings for the token holder |

The connector normalises projects + issues into `NormalizedDocument` (`id = f"{tenant_id}_{source_id}"`), surfaces every user-named operation as a standalone `async def` (OCP), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::SnykHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

There is no official Snyk SDK we want to take on; httpx covers both v1 and v3.

## 3. Auth Flow

Snyk uses a single **API token** for server-to-server access (`AUTH_TYPE = "api_key"`).

### Credentials
- `api_token` — Snyk personal or service-account token issued at **Account Settings → General → Auth Token**. Stored as install_field (type `secret`, required).
- `default_org_id` — Default organization slug or UUID. install_field (type `text`, optional). Used by `sync()` and as a fall-back for endpoints that need an org context.
- `api_version` — Snyk REST API version date (e.g. `2024-10-15`). install_field (type `text`, optional, default `2024-10-15`).
- `base_url` — Override for `https://api.snyk.io/rest`. install_field (type `text`, optional).
- `v1_base_url` — Override for `https://api.snyk.io/v1`. install_field (type `text`, optional).

### Header contract — Snyk-specific gotcha
**Every** request — both v1 and v3 — uses the literal prefix `token` in `Authorization`. **NOT** `Bearer`. This is the single most common integration error.

```
Authorization: token <api_token>           # NOT Bearer
Content-Type:  application/vnd.api+json    # REST v3
Content-Type:  application/json            # legacy v1
Accept:        application/vnd.api+json    # REST v3
Accept:        application/json            # legacy v1
```

### Lifecycle
- `install()` validates `api_token` is non-empty. Does **not** call the API.
- `authorize()` — NOT implemented; api_key flow has no code exchange.
- `health_check()` — `GET /v1/user/me` as a lightweight probe (no version param required).
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Project → NormalizedDocument

| NormalizedDocument | Snyk JSON:API | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{project['id']}"` | tenant-scoped |
| `source_id` | `project["id"]` | Snyk project UUID |
| `title` | `attrs["name"]` | Project name |
| `content` | concat name + type + origin + status | |
| `source` | `"snyk.projects"` | |
| `created_at` | `attrs["created"]` | RFC 3339 |
| `metadata` | `{type, origin, target_id, status, kind: "snyk.project"}` | |

### 4.2 Issue → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{issue['id']}"` |
| `source_id` | `issue["id"]` |
| `title` | `attrs["title"]` |
| `content` | severity + type + description |
| `source` | `"snyk.issues"` |
| `created_at` | `attrs["created_at"]` |
| `metadata` | `{severity, type, status, kind: "snyk.issue"}` |

## 5. Key API Endpoints & Methods

Every method listed here MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/v1/user/me` | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates projects + issues for default_org_id | Calls `ingest_document`. |
| `list_organizations(limit=100, starting_after=None)` | GET | `/rest/orgs?version=…&limit=…` | REST v3 list. |
| `get_organization(org_id)` | GET | `/rest/orgs/{org_id}?version=…` | REST v3 read. |
| `list_projects(org_id, *, target_id=None, types=None, limit=100, starting_after=None)` | GET | `/rest/orgs/{org_id}/projects?version=…` | REST v3 list with filters. |
| `get_project(org_id, project_id)` | GET | `/rest/orgs/{org_id}/projects/{project_id}?version=…` | REST v3 read. |
| `delete_project(org_id, project_id)` | DELETE | `/rest/orgs/{org_id}/projects/{project_id}?version=…` | REST v3 delete; returns `{}` on 204. |
| `list_issues(org_id, *, project_id=None, severity=None, type=None, limit=50, starting_after=None)` | GET | `/rest/orgs/{org_id}/issues?version=…` | REST v3 list with filters. |
| `get_issue(org_id, issue_id)` | GET | `/rest/orgs/{org_id}/issues/{issue_id}?version=…` | REST v3 read. |
| `list_dependencies(org_id, *, project_id=None, limit=50)` | POST | `/v1/org/{org_id}/dependencies?perPage=…&page=…` | Legacy v1 — POST body carries `{filters: {projects: [...]}}`. |
| `list_targets(org_id, *, source=None, limit=100, starting_after=None)` | GET | `/rest/orgs/{org_id}/targets?version=…` | REST v3 list. |
| `get_target(org_id, target_id)` | GET | `/rest/orgs/{org_id}/targets/{target_id}?version=…` | REST v3 read. |
| `list_users(org_id)` | GET | `/v1/org/{org_id}/members` | Legacy v1 list of org members. |
| `get_user_settings(org_id)` | GET | `/v1/user/me/notification-settings/org/{org_id}` | Legacy v1 per-org notification settings. |
| `list_org_members(org_id)` | GET | `/v1/org/{org_id}/members` | Alias for `list_users`. |

Wire convention: Snyk JSON:API uses snake_case (`effective_severity_level`, `starting_after`, `created_at`); legacy v1 mixes snake and camel. The connector boundary returns dicts as-is.

## 6. Error Handling

| HTTP | Snyk meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `SnykBadRequestError` (raise) |
| 401 | Token invalid / missing | `SnykAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (token lacks scope or org access) | `SnykAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `SnykNotFoundError` (raise) |
| 409 | Conflict | `SnykConflictError` |
| 429 | Rate limited (`Retry-After` honoured when present, default 1.0s) | `SnykRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `SnykServerError` → retry with exponential backoff |

All in `exceptions.py` extending `SnykError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt` for 5xx, `Retry-After` (or same backoff) for 429.

Back-compat aliases `SnykNetworkError = SnykServerError` and `SnykNotFound = SnykNotFoundError` are kept so callers do not break.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# none — httpx / pydantic / structlog / pytest / pytest-asyncio / pytest-mock / respx all pre-installed
```

If respx is somehow absent in CI, it can be re-added with `respx>=0.21`. The default expectation is that the shared venv carries it.

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_token` | secret | yes | install_field | `Authorization: token <…>` header value |
| `default_org_id` | text | no | install_field | Default org context for `sync()` and shorthand calls |
| `api_version` | text | no | install_field (default `2024-10-15`) | REST v3 `?version=` query parameter |
| `base_url` | text | no | install_field (default `https://api.snyk.io/rest`) | REST v3 base |
| `v1_base_url` | text | no | install_field (default `https://api.snyk.io/v1`) | Legacy v1 base |
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
| `helpers/normalizer.py` | Maps raw Snyk payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Pagination cursor extraction, retry wrapper, ISO date parsing. | (stdlib only) |
| `models.py` | Pydantic schemas + connector-local request dataclasses. | `pydantic` |
| `exceptions.py` | `SnykError` hierarchy. | (stdlib) |
| `__init__.py` | Self-bootstrap sys.path then re-export `SnykConnector`. | `os`, `sys`, `connector` |

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
