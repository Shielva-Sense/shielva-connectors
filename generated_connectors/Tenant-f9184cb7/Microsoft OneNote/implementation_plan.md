# Microsoft OneNote Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Microsoft OneNote** is the Microsoft 365 note-taking surface exposed through the Microsoft Graph v1.0 REST API at `https://graph.microsoft.com/v1.0/me/onenote` (per-user) or `/users/{id}/onenote` (delegated/app contexts). This connector — `OneNoteConnector` (`CONNECTOR_TYPE = "onenote"`, `AUTH_TYPE = "oauth2_code"`) — wraps the four-level OneNote object model and exposes it as standalone async methods:

| Surface | Base path | Capability |
|---|---|---|
| Notebooks | `/notebooks` | List + read + create notebooks |
| Section groups | `/notebooks/{id}/sectionGroups`, `/sectionGroups` | List section groups |
| Sections | `/notebooks/{id}/sections`, `/sections` | List + read + create sections |
| Pages | `/sections/{id}/pages`, `/pages` | List + read + create + update + delete pages |
| Page content | `/pages/{id}/content` | Raw XHTML read + JSON-command PATCH |
| Copy / search | `/pages/{id}/copyToSection`, `/pages?$search=` | Cross-section copy and full-text search |

The connector normalises pages into `NormalizedDocument` with id `f"{tenant_id}_{source_id}"`, surfaces one standalone public `async def` per user-facing operation (OCP), routes all HTTP through `client/http_client.py::OneNoteHTTPClient` (SOC), and honours Microsoft Graph's throttling contract (`Retry-After`) plus refresh-on-401 semantics for refresh_token rotation.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `respx` | `>=0.21` | Test-time HTTP mocking |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `pydantic`.

OneNote does NOT need JWT/HMAC verification (no webhook signature), so no `PyJWT` dependency. No SDK is used — Microsoft's official `msgraph-sdk-python` is heavyweight and synchronous; the Wix/Outlook gold-standard pattern is raw httpx.

## 3. Auth Flow

Microsoft Graph for OneNote uses the OAuth2 authorization-code grant against the Microsoft identity platform endpoints.

### Credentials
- `client_id` — Azure AD app registration "Application (client) ID". install_field (type `string`, required).
- `client_secret` — Azure AD client secret value (NOT the ID). install_field (type `secret`, required).
- `tenant_id` — Azure tenant slug — `common` for multi-tenant, `organizations` for any work/school account, or a directory GUID/domain for single-tenant. install_field (type `string`, optional, default `common`).
- `redirect_uri` — OAuth2 redirect URI registered in Azure. install_field (type `string`, required).
- `scopes` — Space-separated Graph scopes. Default `Notes.ReadWrite Notes.Read offline_access`. install_field (type `string`, optional).

### Endpoint contract

```
Authorize:  https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize
Token:      https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
Graph:      https://graph.microsoft.com/v1.0/me/onenote
```

Every authenticated request to Graph sends:

```
Authorization: Bearer <access_token>
Accept:        application/json
Content-Type:  application/json   (omitted/overridden for XHTML page bodies)
```

### Lifecycle
- `install()` validates `client_id` + `client_secret`; persists config; returns `(HEALTHY, PENDING)` so the gateway can drive the OAuth code flow.
- `authorize(code, state)` POSTs the OAuth2 form to `{token_url}` and returns a `TokenInfo` (access + refresh + scopes + expiry).
- `on_token_refresh()` exchanges the stored `refresh_token` for a new access token; called automatically by the BaseConnector when `ensure_token()` sees an expired token.
- `health_check()` probes `GET /me/onenote/notebooks?$top=1` as the lightest reachability test.
- The HTTP client also performs a single in-flight `refresh-on-401` via the connector-supplied token_refresher callback for races where the cached expiry beat the server-side revocation.

## 4. Data Model

### 4.1 Page → NormalizedDocument

| NormalizedDocument | OneNote JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{page['id']}"` | tenant-scoped — never `connector_id`-prefixed |
| `source_id` | `page["id"]` | OneNote page GUID |
| `title` | `page["title"]` | `(untitled page)` fallback |
| `content` | text extracted from `/pages/{id}/content` XHTML | tag-stripped, whitespace-normalised |
| `content_type` | `"text"` | server XHTML rolled up to text |
| `source_url` / `url` | `page.links.oneNoteWebUrl.href` | deep-link into the OneNote web client |
| `author` | `page["createdByAppId"]` | Graph doesn't expose human author on /me |
| `created_at` | `page["createdDateTime"]` | RFC 3339 → datetime |
| `updated_at` | `page["lastModifiedDateTime"]` | RFC 3339 → datetime |
| `source` | `"onenote"` | |
| `tenant_id` / `connector_id` | from constructor | for downstream KB scoping |
| `metadata` | `{parent_section_id, parent_section_name, parent_notebook_id, parent_notebook_name, content_url, level, order, web_url}` | preserves the OneNote tree |

### 4.2 Notebook / Section / SectionGroup

Returned as raw `Dict[str, Any]` from the public methods — no normalisation. These resources never become KB documents on their own; only Pages produce documents.

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; persist. |
| `authorize(auth_code, state)` | POST | `/{tenant}/oauth2/v2.0/token` | grant_type=authorization_code. |
| `health_check()` | GET | `/notebooks?$top=1` | Lightest reachability probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates pages + content | Watermark = `lastModifiedDateTime`. |
| `list_notebooks(top, skip, filter, orderby)` | GET | `/notebooks` | `$top/$skip/$filter/$orderby`. |
| `get_notebook(notebook_id)` | GET | `/notebooks/{id}` | |
| `create_notebook(display_name)` | POST | `/notebooks` | body `{displayName}`. |
| `list_sections(notebook_id?, top, skip)` | GET | `/notebooks/{id}/sections` or `/sections` | |
| `get_section(section_id)` | GET | `/sections/{id}` | |
| `create_section(notebook_id, display_name)` | POST | `/notebooks/{id}/sections` | body `{displayName}`. |
| `list_section_groups(notebook_id?)` | GET | `/notebooks/{id}/sectionGroups` or `/sectionGroups` | |
| `list_pages(section_id?, top, skip, filter, search)` | GET | `/sections/{id}/pages` or `/pages` | `$filter/$search`. |
| `get_page(page_id)` | GET | `/pages/{id}` | metadata only. |
| `get_page_content(page_id)` | GET | `/pages/{id}/content` | returns raw XHTML `str`. |
| `create_page(section_id, html_body, content_type)` | POST | `/sections/{id}/pages` | `Content-Type: application/xhtml+xml`; body is XHTML not JSON. |
| `update_page(page_id, commands)` | PATCH | `/pages/{id}/content` | body is a JSON array of patch commands. |
| `delete_page(page_id)` | DELETE | `/pages/{id}` | 204 → `{}`. |
| `copy_page_to_section(page_id, target_section_id, group_id?)` | POST | `/pages/{id}/copyToSection` | 202 → operation handle. |
| `search_pages(query, top)` | GET | `/pages?$search=…` | full-text across the user's mailbox. |
| `get_page_normalized(page_id)` | (helper) | composes `get_page` + `get_page_content` | returns `NormalizedDocument`. |

Wire convention: Graph uses **camelCase** JSON (`displayName`, `lastModifiedDateTime`, `oneNoteWebUrl`). The connector boundary accepts/returns Dict[str, Any] payloads as-is.

## 6. Error Handling

| HTTP | Graph meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `OneNoteError` (raise) |
| 401 | Token expired / invalid | `OneNoteAuthError` → in-flight refresh; persistent 401 → `(DEGRADED, TOKEN_EXPIRED)` |
| 403 | Forbidden — scope missing | `OneNoteError` → `(UNHEALTHY, INVALID_CREDENTIALS)` |
| 404 | Notebook/section/page not found | `OneNoteNotFound` (raise) |
| 429 | Throttled — `Retry-After` honoured ≤ 30s | `OneNoteRateLimitError` → `(DEGRADED, CONNECTED)` |
| 5xx | Graph outage | `OneNoteNetworkError` → exponential backoff retry |

All in `exceptions.py` extending `OneNoteError`. Retry in `helpers/utils.py::with_retry` honours `max_retries=3`, exponential backoff `base_delay * 2 ** attempt + jitter`, capped at 32s. The HTTP client itself does a single auto-retry on 429 + a single auto-refresh on 401; subsequent failures bubble up for `with_retry` to back off further.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
respx>=0.21
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | string | yes | install_field | Azure AD app registration ID |
| `client_secret` | secret | yes | install_field | Azure AD secret value |
| `tenant_id` | string | no (default `common`) | install_field | Azure tenant (NOT Shielva tenant) |
| `redirect_uri` | string | yes | install_field | OAuth callback registered in Azure |
| `scopes` | string | no (default `Notes.ReadWrite Notes.Read offline_access`) | install_field | space-separated Graph scopes |
| `auth_url` | string | no | install_field | computed from `tenant_id` if blank |
| `token_url` | string | no | install_field | computed from `tenant_id` if blank |
| `base_url` | string | no (default `https://graph.microsoft.com/v1.0/me/onenote`) | install_field | Graph base for OneNote |
| `rate_limit_per_min` | number | no (default 120) | install_field | client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]
_STATUS_MAP = {
    401: ("DEGRADED",  "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing, no XHTML construction.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers, retries 429 once, refreshes on 401 once, raises typed exceptions. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Graph payloads → `NormalizedDocument`. Strips XHTML. | `shared.base_connector.NormalizedDocument`, `helpers.utils.parse_iso_datetime` |
| `helpers/utils.py` | `with_retry` (exponential backoff), `build_simple_page_xhtml`, `parse_iso_datetime`. | `httpx`, `exceptions` |
| `models.py` | Local dataclasses (Notebook / Section / Page / SectionGroup) + AuthStatus/Health shims. | `shared.base_connector` (enums only) |
| `exceptions.py` | `OneNoteError` hierarchy. | (stdlib) |
| `__init__.py` | Self-bootstrap sys.path + re-export `OneNoteConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` — no hardcoding ✓
9. Features (retry, refresh-on-401, throttle-honour) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
