# Bitbucket Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Bitbucket Cloud** is Atlassian's hosted git platform exposing a REST API under `https://api.bitbucket.org/2.0`. This connector — `BitbucketConnector` (`CONNECTOR_TYPE = "bitbucket"`, `AUTH_TYPE = "oauth2_code"`) — wraps the operational surfaces a Shielva tenant typically needs from a Bitbucket Cloud workspace:

| Surface | Base path | Capability |
|---|---|---|
| User | `/user` | Current authenticated principal (used by health_check) |
| Workspaces | `/workspaces` | List + read workspaces the principal belongs to |
| Repositories | `/repositories/{ws}` | List, read, create, delete repositories |
| Branches | `/repositories/{ws}/{slug}/refs/branches` | List repository branches |
| Pull Requests | `/repositories/{ws}/{slug}/pullrequests` | List, get, create, merge PRs |
| Issues | `/repositories/{ws}/{slug}/issues` | List, get, create repository issues |
| Commits | `/repositories/{ws}/{slug}/commits` | List + read commits, optionally per-branch |
| Snippets | `/snippets/{ws}` | List workspace snippets |
| Webhooks | `/repositories/{ws}/{slug}/hooks` | List + create per-repo webhooks |
| Files | `/repositories/{ws}/{slug}/src/{commit}/{path}` | Raw file content at a commit |

The connector normalizes repositories + pull requests + issues into `NormalizedDocument` (`id = f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff + Retry-After (3 attempts), refreshes the OAuth token on a 401 (one-shot replay), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::BitbucketHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Bitbucket Cloud uses **OAuth 2.0 Authorization Code Grant** for third-party server-to-server integrations.

### Credentials
- `client_id` — OAuth consumer Key from Bitbucket → Workspace settings → OAuth consumers. install_field (type `string`, required).
- `client_secret` — OAuth consumer Secret. install_field (type `secret`, required).
- `redirect_uri` — Injected by the Shielva gateway at install time. Required at code-exchange to mirror what was used in the authorize redirect.
- `scopes` — Space-separated OAuth scopes; default covers read/write on account, repository, pullrequest, issue.

### Lifecycle

| Phase | Behaviour |
|---|---|
| `install()` | Validates `client_id` + `client_secret`. Does NOT call the API. Persists redirect_uri for `authorize()`. Returns `(HEALTHY, PENDING)`. |
| `authorize(auth_code, state)` | POSTs `grant_type=authorization_code` form-encoded to `TOKEN_URI` with HTTP Basic auth `(client_id, client_secret)`. Persists access + refresh token via `set_token`. |
| `on_token_refresh()` | Called by `BaseConnector.ensure_token()` ahead of expiry. POSTs `grant_type=refresh_token` with HTTP Basic auth. Preserves the refresh_token if Bitbucket does not return a new one. |
| `health_check()` | `GET /user` — lightweight probe. |
| 401 mid-flight | Single-shot refresh via `BitbucketHTTPClient._token_refresh` callback, then replay the original request. A second 401 surfaces `BitbucketAuthError`. |

### Header contract

Every API request to `https://api.bitbucket.org/2.0/*`:

```
Authorization: Bearer <access_token>
Accept:        application/json
Content-Type:  application/json   (for write methods)
```

Token endpoint requests use HTTP Basic with `(client_id, client_secret)` and `Content-Type: application/x-www-form-urlencoded`.

## 4. Data Model

### 4.1 Repository → NormalizedDocument

| NormalizedDocument | Bitbucket JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_repo-{uuid or full_name}"` | tenant-scoped |
| `source_id` | `repo["full_name"]` | `workspace/slug` |
| `title` | `repo["name"]` | falls back to full_name |
| `content` | `repo["description"]` | |
| `source_url` | `repo["links"]["html"]["href"]` | |
| `created_at` | `repo["created_on"]` | RFC 3339 |
| `updated_at` | `repo["updated_on"]` | |
| `metadata` | `{kind: "repository", workspace, is_private, language}` | |

### 4.2 Pull Request → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_pr-{pr['id']}"` |
| `source_id` | `f"pr-{pr['id']}"` |
| `title` | `pr["title"]` |
| `content` | `pr["description"]` or `pr["summary"]["raw"]` |
| `source_url` | `pr["links"]["html"]["href"]` |
| `author` | `pr["author"]["display_name"]` |
| `created_at` | `pr["created_on"]` |
| `metadata` | `{kind, state, source_branch, destination_branch, workspace, repo_slug}` |

### 4.3 Issue → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_issue-{issue['id']}"` |
| `source_id` | `f"issue-{issue['id']}"` |
| `title` | `issue["title"]` |
| `content` | `issue["content"]["raw"]` |
| `author` | `issue["reporter"]["display_name"]` |
| `metadata` | `{kind: "issue", state, issue_kind, priority, workspace, repo_slug}` |

## 5. Key API Endpoints & Methods

Every method listed below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `authorize(auth_code, state)` | POST | `https://bitbucket.org/site/oauth2/access_token` | Exchange OAuth code for tokens. |
| `on_token_refresh()` | POST | TOKEN_URI | Refresh access token. |
| `health_check()` | GET | `/user` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates workspaces → repos → PRs | Calls `ingest_document` per PR. |
| `get_current_user()` | GET | `/user` | Raw response. |
| `list_workspaces(role, pagelen)` | GET | `/workspaces` | Cursor pagination. |
| `get_workspace(workspace)` | GET | `/workspaces/{workspace}` | |
| `list_repositories(workspace, role, pagelen, page)` | GET | `/repositories/{workspace}` | Page-based pagination. |
| `get_repository(workspace, repo_slug)` | GET | `/repositories/{workspace}/{repo_slug}` | |
| `create_repository(workspace, repo_slug, body)` | POST | `/repositories/{workspace}/{repo_slug}` | Body: scm + is_private + name. |
| `delete_repository(workspace, repo_slug)` | DELETE | `/repositories/{workspace}/{repo_slug}` | 204 → `{}`. |
| `list_branches(workspace, repo_slug, pagelen)` | GET | `/repositories/{ws}/{slug}/refs/branches` | |
| `list_pull_requests(ws, slug, state, pagelen)` | GET | `/repositories/{ws}/{slug}/pullrequests` | Default state=OPEN. |
| `get_pull_request(ws, slug, pull_id)` | GET | `/repositories/{ws}/{slug}/pullrequests/{id}` | |
| `create_pull_request(ws, slug, title, source_branch, destination_branch, description, reviewers)` | POST | `/repositories/{ws}/{slug}/pullrequests` | |
| `merge_pull_request(ws, slug, pull_id, merge_strategy, message)` | POST | `/repositories/{ws}/{slug}/pullrequests/{id}/merge` | merge_commit \| squash \| fast_forward. |
| `list_issues(ws, slug, state, pagelen)` | GET | `/repositories/{ws}/{slug}/issues` | state filter via `?q=state="new"`. |
| `get_issue(ws, slug, issue_id)` | GET | `/repositories/{ws}/{slug}/issues/{id}` | |
| `create_issue(ws, slug, title, content, priority, kind)` | POST | `/repositories/{ws}/{slug}/issues` | |
| `list_commits(ws, slug, branch, pagelen)` | GET | `/repositories/{ws}/{slug}/commits[/{branch}]` | |
| `get_commit(ws, slug, commit)` | GET | `/repositories/{ws}/{slug}/commit/{node}` | |
| `list_webhooks(ws, slug)` | GET | `/repositories/{ws}/{slug}/hooks` | |
| `create_webhook(ws, slug, description, url, events, active)` | POST | `/repositories/{ws}/{slug}/hooks` | |
| `get_file_content(ws, slug, commit, path)` | GET | `/repositories/{ws}/{slug}/src/{commit}/{path}` | Returns raw text. |

Wire convention: Bitbucket Cloud uses **snake_case** in JSON (`full_name`, `source_branch`, `merge_strategy`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Bitbucket meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `BitbucketBadRequestError` (raise) |
| 401 | Token expired / missing | One-shot refresh + replay. Second 401 → `BitbucketAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` |
| 403 | Scope insufficient / forbidden | `BitbucketAuthError` → `AuthStatus.AUTHENTICATED` + `ConnectorHealth.DEGRADED` (read-only access still works for other surfaces) |
| 404 | Not found | `BitbucketNotFoundError` (raise) |
| 409 | Conflict (e.g. PR already merged) | `BitbucketConflictError` |
| 429 | Rate limited — honour Retry-After header | `BitbucketRateLimitError` → exponential backoff (3 retries) |
| 5xx | Provider outage | `BitbucketServerError` → exponential backoff with jitter |

All in `exceptions.py` extending `BitbucketError`. Retry in `client/http_client.py::_request` honours `_MAX_RETRIES=3`, exponential backoff `_BASE_DELAY_S * _BACKOFF_FACTOR ** attempt + jitter` capped at `_MAX_DELAY_S=32`.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27,<1.0
pytest-timeout==2.4.0
```

(pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | string | yes | install_field | OAuth consumer Key |
| `client_secret` | secret | yes | install_field | OAuth consumer Secret |
| `scopes` | string | no | install_field (default: full r/w) | Space-separated OAuth scopes |
| `auth_url` | string | no | install_field (default: `https://bitbucket.org/site/oauth2/authorize`) | Override only for proxy/sandbox |
| `token_url` | string | no | install_field (default: `https://bitbucket.org/site/oauth2/access_token`) | Override only for proxy/sandbox |
| `base_url` | string | no | install_field (default: `https://api.bitbucket.org/2.0`) | Override only for proxy/sandbox |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |
| `redirect_uri` | string | no | gateway-injected | OAuth callback URL — required at code exchange |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]
_STATUS_MAP = {
    401: ("DEGRADED",  "TOKEN_EXPIRED"),
    403: ("DEGRADED",  "AUTHENTICATED"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers, retries 429/5xx, refreshes on 401, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Bitbucket payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Connector-level retry helper, safe dict walker. | `exceptions`, `asyncio`, `random` |
| `models.py` | Pydantic schemas for typed views of pagination + create-PR/issue/webhook bodies. | `pydantic` |
| `exceptions.py` | `BitbucketError` hierarchy. | (stdlib) |
| `__init__.py` | Self-bootstraps `sys.path` (drata pattern) + re-exports `BitbucketConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, refresh-on-401, pagination) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
