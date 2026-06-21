# Azure DevOps Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Azure DevOps Services** is Microsoft's hosted DevOps platform exposing a uniform REST API suite under `https://dev.azure.com/{organization}` (plus sister hosts `https://vssps.dev.azure.com/{organization}` for Graph users and `https://vsrm.dev.azure.com/{organization}` for classic Release Management). This connector — `AzureDevopsConnector` (`CONNECTOR_TYPE = "azure_devops"`, `AUTH_TYPE = "api_key"`) — wraps the surfaces a Shielva tenant typically needs:

| Surface | Base path | Capability |
|---|---|---|
| Projects | `/_apis/projects` | List + read |
| Teams | `/_apis/projects/{project}/teams` | List teams |
| Users | `https://vssps.dev.azure.com/{org}/_apis/graph/users` | Graph user enumeration |
| Repos | `/{project}/_apis/git/repositories` | List + read |
| Pull Requests | `/{project}/_apis/git/repositories/{repo}/pullrequests` | List, get, create |
| Work Items | `/{project}/_apis/wit/wiql` + `/_apis/wit/workitems` | WIQL query + batch fetch + JSON-patch CRUD |
| Builds | `/{project}/_apis/build/builds` | List, get, queue |
| Pipelines | `/{project}/_apis/pipelines` | List YAML pipelines |
| Releases | `https://vsrm.dev.azure.com/{org}/{project}/_apis/release/releases` | List classic-RM releases |

The connector normalises work items into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), exposes every operation as a standalone `async def` (OCP), retries 429/5xx with `Retry-After`-aware exponential backoff (3 attempts), and keeps `connector.py` free of raw HTTP (SOC — all HTTP delegated to `client/http_client.py::AzureDevOpsHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas (`models.py`); pre-installed |
| `structlog` | `>=24.1` | Mandatory per `CONNECTOR_SYSTEM_PROMPT`; pre-installed |
| `tenacity` | `>=8.2` | Orchestration-level retry decorator |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Azure DevOps REST API uses **Personal Access Token (PAT) authentication** transported via HTTP Basic.

### Credentials
- `organization` — Azure DevOps organization slug (from `dev.azure.com/{org}`). install_field (`type: string`, required).
- `pat` — Personal Access Token created in **User Settings → Personal Access Tokens**. install_field (`type: secret`, required). `personal_access_token` accepted as a legacy alias.
- `api_version` — `?api-version=` query param sent on every request. install_field (`type: string`, optional, default `7.1`).
- `default_project` — Project used by `sync()` and by methods that omit `project`. install_field (`type: string`, optional).

### Header contract
Every request to `https://dev.azure.com/{org}/*`:

```
Authorization: Basic base64(":<pat>")
Accept:        application/json;api-version=7.1
Content-Type:  application/json                  ← or application/json-patch+json for work-item CRUD
```

Every URL carries `?api-version=<api_version>` (default `7.1`).

### Lifecycle
- `install()` validates `organization` + `pat` non-empty. Does **not** call the API.
- `authorize()` — surfaces the configured PAT as `TokenInfo(token_type="Basic")` for ABI compatibility.
- `health_check()` — `GET /_apis/projects?$top=1` as a lightweight probe.
- `ensure_token()` — not called (PAT does not refresh).

## 4. Data Model

### 4.1 Work Item → NormalizedDocument

| NormalizedDocument | Azure DevOps JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{raw['id']}"` | tenant-scoped (per project rule) |
| `source_id` | `str(raw['id'])` | Work item ID |
| `tenant_id` | `self.tenant_id` | from BaseConnector |
| `connector_id` | `self.connector_id` | from BaseConnector |
| `title` | `fields['System.Title']` | falls back to `Work item {id}` |
| `content` | `fields['System.Description']` ∥ `Microsoft.VSTS.TCM.ReproSteps` | string |
| `content_type` | `"text"` | constant |
| `author` | `fields['System.CreatedBy'].displayName ∥ uniqueName` | string |
| `url` | `_links.html.href` | direct edit link |
| `created_at` | `fields['System.CreatedDate']` | ISO-8601 |
| `updated_at` | `fields['System.ChangedDate']` | ISO-8601 |
| `metadata` | `{rev, state, work_item_type, project, area_path, iteration_path, kind: "azure_devops.work_item"}` |

## 5. Key API Endpoints & Methods

Every method below is a standalone public `async def` on `AzureDevopsConnector` (OCP).

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | — | Validate + persist config |
| `health_check()` | GET | `/_apis/projects?$top=1` | Cheap probe |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | WIQL + batch | Calls `ingest_document` |
| `list_projects(state_filter, top, continuation_token)` | GET | `/_apis/projects` | continuation-token pagination |
| `get_project(project_id_or_name)` | GET | `/_apis/projects/{idOrName}` | |
| `list_teams(project, top, skip)` | GET | `/_apis/projects/{project}/teams` | |
| `list_users(top, continuation_token)` | GET | `vssps.dev.azure.com/_apis/graph/users` | Graph host |
| `list_repos(project)` | GET | `/{project}/_apis/git/repositories` | |
| `get_repo(project, repository_id)` | GET | `/{project}/_apis/git/repositories/{id}` | |
| `list_pull_requests(project, repo, status, top)` | GET | `/{project}/_apis/git/repositories/{repo}/pullrequests` | searchCriteria.status |
| `get_pull_request(project, repo, id)` | GET | …/pullrequests/{id} | |
| `create_pull_request(project, repo, title, source_ref, target_ref, description, reviewers)` | POST | …/pullrequests | camelCase body |
| `query_work_items(project, wiql)` | POST | `/{project}/_apis/wit/wiql` | refs only |
| `list_work_items(project, wiql)` | POST + GET | WIQL → batch fetch | 200-id chunks |
| `get_work_item(work_item_id, fields)` | GET | `/_apis/wit/workitems/{id}` | optional fields= projection |
| `create_work_item(project, work_item_type, fields)` | POST | `/{project}/_apis/wit/workitems/${type}` | JSON-patch body |
| `update_work_item(work_item_id, fields)` | PATCH | `/_apis/wit/workitems/{id}` | JSON-patch body |
| `list_builds(project, status_filter, top)` | GET | `/{project}/_apis/build/builds` | |
| `get_build(project, build_id)` | GET | `/{project}/_apis/build/builds/{id}` | |
| `queue_build(project, definition_id, source_branch, parameters)` | POST | `/{project}/_apis/build/builds` | parameters JSON-encoded |
| `list_pipelines(project, top, continuation_token)` | GET | `/{project}/_apis/pipelines` | |
| `list_releases(project, definition_id, top)` | GET | `vsrm.dev.azure.com/{project}/_apis/release/releases` | RM host |

Wire convention: Azure DevOps uses **PascalCase** field keys (`System.Title`, `sourceRefName`, `pullRequestId`). The connector boundary accepts/returns these as `Dict[str, Any]` payloads; Pydantic schemas in `models.py` carry camelCase aliases for typed consumers.

## 6. Error Handling

| HTTP | Azure DevOps meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `AzureDevOpsBadRequestError` (raise) |
| 401 | PAT invalid / expired | `AzureDevOpsAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` |
| 403 | PAT lacks scope | `AzureDevOpsAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `AzureDevOpsNotFoundError` (raise) |
| 409 | Revision mismatch | `AzureDevOpsConflictError` |
| 429 | TSTU throttle | `AzureDevOpsRateLimitError(retry_after_s)` — backoff respects `Retry-After` header |
| 5xx | Provider outage | `AzureDevOpsServerError` — exponential backoff |

All defined in `exceptions.py` extending `AzureDevOpsError`. Retry policy in `client/http_client.py::_request`: `_MAX_RETRIES=3`, `_BACKOFF_BASE=0.5`, `_BACKOFF_CAP=8.0`, `_RETRYABLE_STATUSES={429, 500, 502, 503, 504}`.

Back-compat aliases preserved in `exceptions.py`:
- `AzureDevOpsNotFound = AzureDevOpsNotFoundError`
- `AzureDevOpsNetworkError = AzureDevOpsServerError`

## 7. Dependencies

Packages to install in the connector venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `organization` | string | yes | install_field | `{org}` segment of base URL |
| `pat` | secret | yes | install_field | PAT — Basic-encoded with empty username |
| `personal_access_token` | secret | — | legacy alias | Accepted by `install()` for back-compat |
| `api_version` | string | no | install_field (default `7.1`) | `?api-version=` query param |
| `default_project` | string | no | install_field | Used by `sync()` |
| `rate_limit_per_min` | number | no | install_field (default `200`) | Soft client-side cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["organization", "pat"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
_DEFAULT_API_VERSION = "7.1"
_DEFAULT_RATE_LIMIT  = 200
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds Basic-auth header, api-version param, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw work-item payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Orchestration-level retry helper (`with_retry`), `chunked` batching, `safe_get`. | `asyncio`, `structlog`, `exceptions` |
| `models.py` | Pydantic schemas with camelCase aliases for request/response bodies. | `pydantic` |
| `exceptions.py` | `AzureDevOpsError` hierarchy + back-compat aliases. | (stdlib) |
| `__init__.py` | Self-bootstraps `sys.path` (Drata pattern) and re-exports `AzureDevopsConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is a standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, batching, normalisation) as composable helpers ✓
10. Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only ✓

**Score: 10/10.**
