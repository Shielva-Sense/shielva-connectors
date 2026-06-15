# Gmail Connector — Implementation Plan

## 1. Overview

The Google Gmail Connector integrates with the Gmail REST API (v1) to allow the Shielva platform to ingest, manage, and delete email messages and threads from a user's Gmail account. Auth is OAuth 2.0 Authorization Code Grant with offline access (to obtain a refresh token). The connector exposes full CRUD-style operations including list, read, add (label), delete (soft/hard), and bulk delete, plus an incremental sync that propagates deletions to the knowledge base.

**Provider**: Google  
**Service**: gmail  
**Auth Type**: `oauth2_code`  
**Base API**: `https://gmail.googleapis.com/gmail/v1/users/me`

---

## 2. SDK / Package Selection

No new packages are required beyond what is already declared in `requirements.txt` and the shared venv.

| Package | Version | Justification |
|---|---|---|
| `google-api-python-client` | `>=2.0` | Gmail API discovery client used for `users.messages.*` and `users.threads.*` calls. Already in shared venv. |
| `google-auth` / `google-auth-oauthlib` | `>=2.0` | OAuth2 token lifecycle. Already in shared venv. |
| `aiohttp` | `>=3.9` | Async HTTP transport used by `GmailHTTPClient`. Already in shared venv. |
| `structlog` | `>=23.0` | Structured logging. Already in shared venv via SDK. |

```
pip install  # no new packages — all deps are in the shared venv or requirements.txt
```

---

## 3. Auth Flow

1. **get_oauth_url()** — inherited from `BaseConnector`. Uses `AUTH_URI`, `CLIENT_ID`, `REQUIRED_SCOPES`, and forces `access_type=offline&prompt=consent` so a refresh token is always returned.
2. **authorize(auth_code, state)** — POSTs `auth_code` to `TOKEN_URL` via `aiohttp`. Exchanges for `{access_token, refresh_token, expires_in, scope}`. Constructs `TokenInfo(access_token=…, refresh_token=…, expires_at=now+expires_in, scopes=[…])`. Calls `await self.set_token(token_info)` to persist to Redis.
3. **on_token_refresh()** — POSTs `{grant_type=refresh_token, refresh_token=…, client_id=…, client_secret=…}` to `TOKEN_URL`. Returns new `TokenInfo`. Called automatically by `ensure_token()` before any API call.
4. Token is retrieved via `await self.ensure_token()` inside `_build_http_client()` and passed as `Authorization: Bearer <access_token>`.

---

## 4. Data Model — NormalizedDocument Mapping

Gmail `messages.get` (format=full) response → `NormalizedDocument`:

| Gmail field | NormalizedDocument field | Notes |
|---|---|---|
| `id` | `id` | Gmail message ID |
| `id` | `source_id` | Same value, the external system's ID |
| `payload.headers["Subject"]` | `title` | Header lookup; fallback `"(no subject)"` |
| decoded body (text/plain preferred, text/html fallback) | `content` | Normalizer extracts and decodes |
| `"text"` or `"html"` | `content_type` | Set by normalizer based on chosen part |
| `payload.headers["From"]` | `metadata["from"]` | |
| `payload.headers["To"]` | `metadata["to"]` | |
| `payload.headers["Date"]` | `metadata["date"]` | |
| `labelIds` | `metadata["labels"]` | |
| `threadId` | `metadata["thread_id"]` | |
| `snippet` | `metadata["snippet"]` | |
| connector type | `source` | `"google_gmail"` |
| `self.tenant_id` | `tenant_id` | |
| `self.connector_id` | `connector_id` | |

---

## 5. Key API Endpoints & Methods

### 5.1 `install()`
- **Purpose**: Validate config, return `ConnectorStatus` showing connector is offline-but-ready.
- **API**: None (no external call).
- **Returns**: `ConnectorStatus(connector_id=…, health=HEALTHY, auth_status=PENDING)`.

### 5.2 `authorize(auth_code, state)`
- **Endpoint**: `POST https://oauth2.googleapis.com/token`
- **Payload**: `{code, client_id, client_secret, redirect_uri, grant_type=authorization_code}`
- **Response**: `{access_token, refresh_token, expires_in, scope, token_type}`
- **Returns**: `TokenInfo(access_token, refresh_token, expires_at=now+expires_in, scopes=[…])`

### 5.3 `health_check()`
- **Endpoint**: `GET https://gmail.googleapis.com/gmail/v1/users/me/profile`
- **Purpose**: Verify the token is valid and the Gmail account is accessible.
- **Returns**: `ConnectorStatus(health=HEALTHY, auth_status=CONNECTED)` on success; `DEGRADED/FAILED` on error.

### 5.4 `sync(since, full, kb_id, webhook_url)`
- **Endpoint**: `GET users/me/messages` (list), then `GET users/me/messages/{id}` (get each)
- **Pagination**: `pageToken` cursor; loop until `nextPageToken` is absent.
- **Query params**: `q=after:{unix_ts}` for incremental; omit for full.
- **Deletion propagation**:
  - Load `known_ids: Set[str]` via `load_known_ids(self.config)`.
  - Collect `current_ids` from the list response.
  - For each id in `known_ids − current_ids` call `await self._remove_from_kb(msg_id)`.
  - Log `gmail.sync.deletions_propagated` with count.
  - Persist `current_ids` via `await self.save_config(save_known_ids(self.config, current_ids))`.
- **Returns**: `SyncResult(status=COMPLETED, documents_synced=N, documents_found=M)`

### 5.5 `list_email(query, max_results, page_token)`
- **Endpoint**: `GET users/me/messages?q={query}&maxResults={max_results}&pageToken={page_token}`
- **Pagination**: returns `{messages: [{id, threadId}], nextPageToken, resultSizeEstimate}`.
- **Returns**: list of message stub dicts + next page token.

### 5.6 `read_email(msg_id)`
- **Endpoint**: `GET users/me/messages/{msg_id}?format=full`
- **Returns**: `NormalizedDocument` via normalizer.

### 5.7 `add_email(msg_id, label_ids)`
- **Endpoint**: `POST users/me/messages/{msg_id}/modify` with `{addLabelIds: [...]}`
- **Returns**: updated message stub dict.

### 5.8 `delete_email(msg_id, permanent)`
- **Alias**: calls `delete_message(msg_id, permanent=permanent)`.

### 5.9 `remove_email(msg_id)`
- **Alias**: calls `delete_message(msg_id, permanent=False)` (soft/trash only).

### 5.10 `delete_message(msg_id, permanent=False)`
- **Soft** (`permanent=False`): `POST users/me/messages/{msg_id}/trash` → returns trashed resource dict.
- **Hard** (`permanent=True`):
  - Calls `_assert_permanent_delete_allowed()` first.
  - `DELETE users/me/messages/{msg_id}` → returns `None` (204 No Content).
- **Logs**: `gmail.delete_message.ok`.

### 5.11 `delete_thread(thread_id, permanent=False)`
- **Soft**: `POST users/me/threads/{thread_id}/trash` → returns trashed thread dict.
- **Hard**:
  - Calls `_assert_permanent_delete_allowed()` first.
  - `DELETE users/me/threads/{thread_id}` → `None` (204 No Content).
- **Logs**: `gmail.delete_thread.ok`.

### 5.12 `bulk_delete(query, permanent=False)`
- Reuses the `list_email` pageToken loop to collect all matching `msg_id`s.
- For each `msg_id`: calls `execute_trash_message` or `execute_delete_message`; catches per-message exceptions.
- Returns `{"deleted": N, "failed": N, "errors": [...]}`.
- **Logs**: `gmail.bulk_delete.ok` with deleted/failed counts.

---

## 6. Error Handling

| HTTP Status | Exception | Handling |
|---|---|---|
| 401 | `ConnectorAuthError` | Raised in `_map_http_error`; triggers token refresh via `ensure_token()` |
| 403 | `ConnectorPermissionError` | Raised in `_map_http_error`; re-raised to caller |
| 404 | `ConnectorNotFoundError` | `Message not found: {msg_id}` |
| 429 | `ConnectorRateLimitError` | `@retry` decorator with exponential backoff handles transparently |
| 5xx | `ConnectorError` | Raised in `_map_http_error`; `@retry` handles with backoff |

The `@retry` decorator in `helpers/utils.py` implements exponential backoff with jitter, max 3 retries, initial delay 1 s, multiplier 2.

---

## 7. Dependencies

No new packages are required. All dependencies are in the shared venv or `requirements.txt`:

```
# No additional pip installs needed
# google-api-python-client, google-auth, aiohttp, structlog — already available
```

---

## 8. Config & Install Fields

| Key | Type | Source | Required | Notes |
|---|---|---|---|---|
| `client_id` | `str` | Hardcoded class const `CLIENT_ID` | — | OAuth client ID |
| `client_secret` | `str` | Hardcoded class const `CLIENT_SECRET` | — | OAuth client secret |
| `scopes` | `List[str]` | Hardcoded class const `REQUIRED_SCOPES` | — | `gmail.modify`; `mail.google.com` added only when `allow_permanent_delete=True` |
| `auth_url` | `str` | Hardcoded class const `AUTH_URL` | — | `https://accounts.google.com/o/oauth2/v2/auth` |
| `token_url` | `str` | Hardcoded class const `TOKEN_URL` | — | `https://oauth2.googleapis.com/token` |
| `base_url` | `str` | Hardcoded class const `BASE_URL` | — | `https://gmail.googleapis.com/gmail/v1` |
| `rate_limit_per_min` | `int` | Hardcoded class const `RATE_LIMIT_PER_MIN` | — | `250` |
| `pagination_type` | `str` | Hardcoded class const `PAGINATION_TYPE` | — | `"cursor"` |
| `api_version` | `str` | Hardcoded class const `API_VERSION` | — | `"v1"` |
| `allow_permanent_delete` | `bool` | **install_field** — `self.config.get("allow_permanent_delete", False)` | Optional | Default `False`; enables `DELETE` endpoint and `mail.google.com` scope |
| `known_message_ids` | `List[str]` | Runtime checkpoint — `self.config.get("known_message_ids", [])` | — | Persisted by `sync()` via `save_config()` |

**User-provided install fields**: only `allow_permanent_delete` is user-supplied.

---

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Must NOT contain |
|---|---|---|
| `connector.py` | Orchestration only: calls client methods, passes results to normalizer, calls `ingest_batch`, calls `set_token`, logs events | Raw HTTP, JSON parsing, regex, retry logic |
| `client/http_client.py` | All HTTP calls to Gmail API: `execute_list_messages`, `execute_get_message`, `execute_trash_message`, `execute_delete_message`, `execute_modify_message`, `execute_trash_thread`, `execute_delete_thread`; maps HTTP errors via `_map_http_error`; applies `@retry` decorator | Business logic, normalization, config reads beyond token/base_url |
| `helpers/normalizer.py` | `normalize_message(raw)→NormalizedDocument`: extracts headers, decodes body parts (text/plain > text/html), sets all `NormalizedDocument` fields | HTTP calls, config access |
| `helpers/utils.py` | `retry` decorator with exponential backoff + jitter; `load_known_ids(config)→Set[str]`; `save_known_ids(config, ids)→dict`; `extract_header(headers, name)→str` | HTTP calls, Gmail-specific logic |
| `exceptions.py` | `ConnectorError`, `ConnectorAuthError`, `ConnectorPermissionError`, `ConnectorNotFoundError`, `ConnectorRateLimitError` | Any logic |
| `config.py` | Class-level constants: `CLIENT_ID`, `CLIENT_SECRET`, `TOKEN_URL`, `AUTH_URL`, `BASE_URL`, `REQUIRED_SCOPES`, `RATE_LIMIT_PER_MIN`, `PAGINATION_TYPE`, `API_VERSION`, `ALLOW_PERMANENT_DELETE` | Any logic |

**OCP — each user-requested operation is a standalone `async def` method in `connector.py`**, never folded into `sync()`. New operations can be added without touching `BaseConnector` or existing methods. Config values come from `self.config.get()`. Retry/backoff is the `@retry` decorator in `utils.py`. Error mapping is in `exceptions.py` and `_map_http_error`.
