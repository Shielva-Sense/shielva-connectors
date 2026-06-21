# Document360 Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Document360** is a SaaS knowledge-base platform exposing a versioned REST API under `https://apihub.document360.io/v2`. This connector — `Document360Connector` (`CONNECTOR_TYPE = "document360"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Document360 project:

| Surface | Base path | Capability |
|---|---|---|
| Projects | `/Projects` | List + read projects under the api_token |
| Versions | `/Projects/{projectId}/Versions` | List versions of a project |
| Categories | `/Categories/{versionId}` | List + create + update + delete category folders |
| Articles | `/Articles/{versionId}`, `/Articles/{articleId}` | CRUD + publish + read by language |
| Versions (article) | `/Articles/{articleId}/Versions` | Article version history |
| Tags | `/Tags/{versionId}` | List + create tags scoped to a version |
| Drive | `/Drive/Files` | List + upload drive files (attachments) |
| Team | `/TeamAccounts` | List team members and their roles |
| Languages | `/Projects/{projectId}/Languages` | List supported languages |
| Templates | `/Templates/{versionId}` | List article templates |
| Search | `/Search` | Full-text search across articles |

The connector normalises articles into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff + jitter, and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::Document360HTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async HTTP client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

Document360 does not ship an official Python SDK — we hand-roll the HTTP client.

## 3. Auth Flow

Document360 REST API uses **API token authentication** for server-to-server integrations.

### Credentials
- `api_token` — Document360 API token created in **Settings → API Tokens → Generate API Token**. Stored as install_field (type `secret`, required).
- `default_project_id` — Optional project UUID used as a per-call default. install_field (type `text`, optional).
- `default_version_id` — Optional version UUID for the project. install_field (type `text`, optional).
- `default_language_code` — Optional ISO 639-1 language code (defaults to `en`). install_field (type `text`, optional).
- `project_slug` — Optional public site slug for synthesised article URLs. install_field (type `text`, optional).
- `base_url` — Optional override of `https://apihub.document360.io/v2`. install_field (type `text`, optional).

### Header contract
Every request to `https://apihub.document360.io/v2/*`:

```
api_token: <api_token>             (raw — NOT 'Bearer <token>')
Content-Type: application/json
Accept:       application/json
```

### Lifecycle
- `install()` validates `api_token` non-empty. Does **not** call the API.
- `authorize()` returns a synthetic `TokenInfo(access_token=api_token, token_type="api_key")` — no exchange.
- `health_check()` — `GET /Projects` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Article → NormalizedDocument

| NormalizedDocument | Document360 JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{article['id']}"` | tenant-scoped, mandatory |
| `source_id` | `article["id"]` (or `Id` / `articleId`) | mixed-case tolerated |
| `title` | `article["title"]` | falls back to `(untitled)` |
| `content` | HTML-stripped `article["content"]` | original HTML kept in `metadata.raw_html` |
| `content_type` | `"html"` if `<…>` markers present else `"text"` | |
| `source` | `"document360"` | |
| `source_url` | Synthesised from `project_slug` + language + id | |
| `created_at` | `article["createdAt"]` | ISO 8601, normalised to UTC |
| `updated_at` | `article["modifiedAt"]` (fallback `createdAt`) | |
| `author` | `article["author"]` or `createdBy` | string only |
| `metadata` | `{category_id, language_code, is_published, raw_content_type, raw_html}` | |

### 4.2 List paginations
Document360 returns either a bare JSON array or a `{items: [...]}` envelope depending on endpoint vintage. Helpers `_envelope_items()` in `helpers/utils.py` accept both.

## 5. Key API Endpoints & Methods

Every method below exists as a standalone public `async def` in `connector.py`. All return raw response dicts (or `NormalizedDocument` where the spec says so).

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate api_token; init HTTP client. |
| `health_check()` | GET | `/Projects` | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | walks projects → versions → articles | Normalises each article and calls `ingest_document`. |
| `list_projects()` | GET | `/Projects` | |
| `get_project(project_id)` | GET | `/Projects/{projectId}` | |
| `list_articles(version_id, *, category_id=None, language_code="en")` | GET | `/Articles/{versionId}?categoryId=…&languageCode=…` | |
| `get_article(article_id, language_code="en")` | GET | `/Articles/{articleId}/Language/{lang}` | Returns `NormalizedDocument`. |
| `create_article(version_id, category_id, title, content="", language_code="en", order=None)` | POST | `/Articles/{versionId}` | |
| `update_article(article_id, title=None, content=None, language_code="en")` | PUT | `/Articles/{articleId}/Language/{lang}` | |
| `delete_article(article_id)` | DELETE | `/Articles/{articleId}` | |
| `publish_article(article_id, language_code="en")` | POST | `/Articles/{articleId}/Language/{lang}/Publish` | |
| `list_article_versions(article_id, language_code="en")` | GET | `/Articles/{articleId}/Language/{lang}/Versions` | |
| `list_categories(version_id, parent_category_id=None)` | GET | `/Categories/{versionId}?parentCategoryId=…` | |
| `create_category(version_id, parent_category_id, title, order=None, category_type="Folder", language_code="en")` | POST | `/Categories/{versionId}` | |
| `list_tags(version_id)` | GET | `/Tags/{versionId}` | |
| `list_team_members()` | GET | `/TeamAccounts` | |
| `list_languages(project_id)` | GET | `/Projects/{projectId}/Languages` | |
| `list_templates(version_id)` | GET | `/Templates/{versionId}` | |
| `list_drive_files(folder_id=None, page=1, page_size=50)` | GET | `/Drive/Files?folderId=…&page=…&pageSize=…` | |
| `upload_drive_file(file_name, content_b64, folder_id=None)` | POST | `/Drive/Files` | Base64-encoded multipart-equivalent JSON body. |
| `search_articles(version_id, query, language_code="en", limit=20)` | GET | `/Search?versionId=…&query=…` | |

## 6. Error Handling

| HTTP | Document360 meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `Document360BadRequestError` |
| 401 | api_token invalid / missing | `Document360AuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.DEGRADED` |
| 403 | Forbidden (token lacks role) | `Document360AuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `Document360NotFound` (raise) |
| 409 | Conflict (duplicate title) | `Document360ConflictError` |
| 429 | Rate limited | `Document360RateLimitError`; retried 3× with exponential backoff + jitter |
| 5xx | Provider outage | `Document360NetworkError`; retried 3× with exponential backoff |

All in `exceptions.py` extending `Document360Error`. Retry in `client/http_client.py::_request` honours `_DEFAULT_MAX_RETRIES=3`, backoff `min(base * 2 ** attempt + jitter, cap)`. 401/403/404 raised on first attempt — never retried.

## 7. Dependencies

Packages declared in `requirements.txt` (`install_deps` reads this):

```
httpx>=0.27.0
```

(pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_token` | secret | yes | install_field | `api_token` header value |
| `default_project_id` | text | no | install_field | per-call default project for `sync()` |
| `default_version_id` | text | no | install_field | per-call default version |
| `default_language_code` | text | no (default `en`) | install_field | ISO 639-1 |
| `project_slug` | text | no | install_field | Used to build public article URLs |
| `base_url` | text | no | install_field | Override Document360 API host |
| `rate_limit_per_min` | number | no (default 100) | install_field | Soft client-side cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_token"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Document360 payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument`, `helpers.utils` |
| `helpers/utils.py` | Cursor/page helpers, ISO date parsing, URL synth, envelope normalisation. | (stdlib only) |
| `models.py` | Local dataclasses + property shims for `AuthStatus` / `ConnectorHealth`. | `dataclasses`, `shared.base_connector` (lazy) |
| `exceptions.py` | `Document360Error` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `Document360Connector`. | `connector` |

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
