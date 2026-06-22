# Dropbox Connector — Implementation Plan

> Step 1 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Dropbox** is a cloud file storage / sharing platform exposing a JSON-RPC-style REST API v2 under two hostnames:

| Hostname | Purpose |
|---|---|
| `https://api.dropboxapi.com/2` | RPC endpoints (metadata, search, sharing, account). All bodies are JSON. |
| `https://content.dropboxapi.com/2` | Content endpoints (`upload`, `download`). Request/response payload is binary; JSON metadata travels in the `Dropbox-API-Arg` header. |

This connector — `DropboxConnector` (`CONNECTOR_TYPE = "dropbox"`, `AUTH_TYPE = "oauth2_code"`) — wraps the operational surfaces a Shielva tenant typically needs from Dropbox:

| Surface | Capability |
|---|---|
| Files (RPC) | list_folder, list_folder/continue, get_metadata, copy, move, delete, create_folder, search_v2, list_revisions, restore |
| Files (content) | upload, download |
| Sharing | create_shared_link_with_settings, list_shared_links |
| Users | get_current_account, get_account, get_space_usage |
| Auth | OAuth2 authorization code + offline refresh, token_revoke |

The connector normalises Dropbox files into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries `429/5xx` with exponential backoff (3 attempts), honours `Retry-After` from Dropbox rate-limit responses, and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::DropboxHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async HTTP client; pre-installed in shared venv. Used for both RPC and content endpoints. |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed. |

We deliberately do **not** use the official `dropbox` SDK — it is synchronous and would force `asyncio.to_thread` wrappers, breaking the async-first contract. `httpx` calls are thinner, fully async, and let us own the retry/auth policy.

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Dropbox uses **OAuth 2.0 Authorization Code Grant**. Access tokens expire after **4 hours**; refresh tokens are issued when the authorize step requests `token_access_type=offline`.

### Credentials
- `client_id` — Dropbox App Key (App Console). install_field, type `string`, required.
- `client_secret` — Dropbox App Secret. install_field, type `secret`, required.
- `redirect_uri` — Callback registered in Dropbox App Console. Injected by the gateway at authorize-time; read via `self.config.get("redirect_uri", "")`.
- `token_url` — Provider-wide constant `https://api.dropboxapi.com/oauth2/token`. install_field with `default` so autofill pre-fills it.

### Endpoints (class constants — NOT install_fields)
```
AUTH_URI         = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URI        = "https://api.dropboxapi.com/oauth2/token"
REVOKE_URI       = "https://api.dropboxapi.com/2/auth/token/revoke"
REQUIRED_SCOPES  = [
    "files.metadata.read",
    "files.content.read",
    "files.content.write",
    "sharing.read",
    "sharing.write",
    "account_info.read",
]
```

### Lifecycle
- `install()` — validates `client_id` + `client_secret`; does NOT call the API.
- `authorize(auth_code, state)` — POST `TOKEN_URI` with `grant_type=authorization_code`, `code=auth_code`, `client_id`, `client_secret`, `redirect_uri`. Returns `TokenInfo(access_token, refresh_token, expires_at=now+expires_in, scopes)`.
- `on_token_refresh(token)` — POST `TOKEN_URI` with `grant_type=refresh_token`. Returns a new `TokenInfo`. Called by `BaseConnector.ensure_token()`.
- `health_check()` — POST `/users/get_current_account` (lightweight probe).
- `disconnect()` — POST `/2/auth/token/revoke`, then `clear_token()`.

### Header contract — RPC
```
Authorization: Bearer <access_token>
Content-Type:  application/json
```

### Header contract — Content endpoints (`upload`, `download`)
```
Authorization:    Bearer <access_token>
Dropbox-API-Arg:  <json-encoded args>          ← JSON moves OUT of body
Content-Type:     application/octet-stream     ← upload only
```

## 4. Data Model

### File → NormalizedDocument

| NormalizedDocument | Dropbox JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{source_id}"` | tenant-scoped |
| `source_id` | `entry["id"]` (e.g. `id:abc123`) | stable across rename/move |
| `title` | `entry["name"]` | |
| `content` | concat name + path + size + modified | text shadow for KB |
| `content_type` | `"text"` | |
| `source_url` | `https://www.dropbox.com/home{path_display}` | best-effort |
| `created_at` | `entry["client_modified"]` (parsed) | |
| `updated_at` | `entry["server_modified"]` (parsed) | |
| `metadata` | `{kind: "dropbox.file", path_display, path_lower, size, rev, is_downloadable, content_hash}` | |

### Folder → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{source_id}"` |
| `source_id` | `entry["id"]` or `path_lower` |
| `title` | `entry["name"]` |
| `content` | `"Folder: {path_display}"` |
| `metadata.kind` | `"dropbox.folder"` |

## 5. Key API Endpoints & Methods

Every method below MUST exist on `DropboxConnector` as a standalone `async def`. The connector method delegates to `http_client` (after `with_retry`), `connector.py` never builds an HTTP request itself.

### 5.1 `list_folder(path: str = "", recursive: bool = False, limit: int = 200) -> Dict`
- `POST https://api.dropboxapi.com/2/files/list_folder`
- body: `{path, recursive, limit, include_media_info, include_deleted, include_has_explicit_shared_members}`
- pagination via `cursor` + `has_more` returned in response; call `list_folder_continue` until exhausted.

### 5.2 `list_folder_continue(cursor: str) -> Dict`
- `POST /2/files/list_folder/continue` — body: `{cursor}`.

### 5.3 `get_metadata(path: str) -> Dict`
- `POST /2/files/get_metadata` — body: `{path, include_media_info, include_deleted}`.

### 5.4 `download_file(path: str) -> Dict[str, Any]`
- `POST https://content.dropboxapi.com/2/files/download`
- JSON args travel in `Dropbox-API-Arg: {"path": "<path>"}`.
- Returns `{"metadata": <parsed Dropbox-API-Result header>, "content_b64": <base64 bytes>}`.

### 5.5 `upload_file(path: str, content: bytes, mode: str = "add", autorename: bool = False) -> Dict`
- `POST https://content.dropboxapi.com/2/files/upload`
- args in `Dropbox-API-Arg`, raw bytes as body, `Content-Type: application/octet-stream`.

### 5.6 `copy_file(from_path: str, to_path: str, autorename: bool = False) -> Dict`
- `POST /2/files/copy_v2` — body: `{from_path, to_path, autorename}`.

### 5.7 `move_file(from_path: str, to_path: str, autorename: bool = False) -> Dict`
- `POST /2/files/move_v2` — body: `{from_path, to_path, autorename}`.

### 5.8 `delete_file(path: str) -> Dict`
- `POST /2/files/delete_v2` — body: `{path}`.

### 5.9 `create_folder(path: str, autorename: bool = False) -> Dict`
- `POST /2/files/create_folder_v2` — body: `{path, autorename}`.

### 5.10 `search(query: str, max_results: int = 100, path: str = "") -> Dict`
- `POST /2/files/search_v2` — body: `{query, options: {path, max_results, file_status: "active"}}`.

### 5.11 `list_revisions(path: str, limit: int = 10) -> Dict`
- `POST /2/files/list_revisions` — body: `{path, mode: "path", limit}`.

### 5.12 `restore_revision(path: str, rev: str) -> Dict`
- `POST /2/files/restore` — body: `{path, rev}`.

### 5.13 `create_shared_link(path: str, settings: Optional[Dict] = None) -> Dict`
- `POST /2/sharing/create_shared_link_with_settings` — body: `{path, settings}`.

### 5.14 `list_shared_links(path: Optional[str] = None, cursor: Optional[str] = None, direct_only: bool = True) -> Dict`
- `POST /2/sharing/list_shared_links` — body: `{path, cursor, direct_only}`.

### 5.15 `get_current_account() -> Dict`
- `POST /2/users/get_current_account` — body: null.

### 5.16 `get_account(account_id: str) -> Dict`
- `POST /2/users/get_account` — body: `{account_id}`.

### 5.17 `get_space_usage() -> Dict`
- `POST /2/users/get_space_usage` — body: null.

## 6. Error Handling

| HTTP | Exception | Health classification |
|---|---|---|
| 400 | `DropboxBadRequestError` | (caller-side) |
| 401 | `DropboxAuthError` | `OFFLINE / TOKEN_EXPIRED` |
| 403 | `DropboxAuthError` | `UNHEALTHY / INVALID_CREDENTIALS` |
| 404 | `DropboxNotFoundError` | — |
| 409 (`*/not_found`) | `DropboxNotFoundError` | — |
| 409 (other) | `DropboxConflictError` | — |
| 429 | `DropboxRateLimitError(retry_after_s)` | `DEGRADED / CONNECTED` |
| 5xx | `DropboxServerError` | retry candidate |
| network | `DropboxNetworkError` | retry candidate |

Retry policy (in `http_client._request`):
- `_MAX_RETRIES = 3`, `_BACKOFF_BASE = 0.5s` (exponential `0.5, 1.0, 2.0`).
- 429 honours `Retry-After` header (seconds) — capped at `_MAX_RETRY_AFTER = 30s`.
- 5xx and `httpx.TimeoutException`/`httpx.NetworkError` retried.
- 401/403/404/409 never retried (raise immediately).

## 7. Dependencies

```
httpx>=0.27,<1.0
structlog>=24.1
```

(`pytest`, `pytest-asyncio`, `pytest-mock`, `respx` pre-installed in shared dev venv.)

## 8. Config & Install Fields

| Key | Type | Required | bind | Default | Read in code |
|---|---|---|---|---|---|
| `client_id` | string | yes | false (user-supplied) | — | `self.config.get("client_id", "")` |
| `client_secret` | secret | yes | false (user-supplied) | — | `self.config.get("client_secret", "")` |
| `token_url` | string | no | true (provider-wide) | `https://api.dropboxapi.com/2/oauth2/token` | `self.TOKEN_URI` |
| `redirect_uri` | string | no | injected | — | `self.config.get("redirect_uri", "")` (gateway-supplied) |

### Class constants (NOT install_fields)
```
CONNECTOR_TYPE = "dropbox"
AUTH_TYPE      = "oauth2_code"
AUTH_URI       = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URI      = "https://api.dropboxapi.com/oauth2/token"
REVOKE_URI     = "https://api.dropboxapi.com/2/auth/token/revoke"
REQUIRED_SCOPES = [
    "files.metadata.read",
    "files.content.read",
    "files.content.write",
    "sharing.read",
    "sharing.write",
    "account_info.read",
]
_API_BASE      = "https://api.dropboxapi.com/2"
_CONTENT_BASE  = "https://content.dropboxapi.com/2"
_STATUS_MAP    = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]
```

## 9. SOC/OCP Architecture Plan

| File | Owns | Forbidden |
|---|---|---|
| `connector.py` | Orchestration — calls `http_client`, normalises via `helpers/normalizer.py`, classifies errors via `exceptions.py`, persists tokens via inherited `set_token`. Each user-requested operation = 1 standalone `async def`. | Raw `httpx` calls, JSON parsing, normalisation. |
| `client/http_client.py` | All Dropbox HTTP — RPC + content. Owns auth header, JSON encode/decode, retry policy, Retry-After parsing, error → exception mapping. | Business logic, normalisation. |
| `helpers/normalizer.py` | Raw dict → `NormalizedDocument`. Date parsing helpers. | HTTP, retry, state. |
| `helpers/utils.py` | `with_retry`, `safe_get`, `parse_dt`, `dropbox_path` validators. | HTTP, normalisation. |
| `exceptions.py` | Exception hierarchy + `status_code` / `response_body` / `retry_after_s` attributes. | HTTP, business logic. |
| `models.py` | Pydantic schemas for typed request/response shapes (optional — boundary stays Dict[str, Any]). | Logic, HTTP. |

Each public method on `DropboxConnector` is independently composable — adding a new endpoint requires adding `http_client.<method>` + `connector.<method>` and never modifying `BaseConnector` or existing methods.
