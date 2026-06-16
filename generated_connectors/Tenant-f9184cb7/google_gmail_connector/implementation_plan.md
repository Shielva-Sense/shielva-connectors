# Gmail Connector — Implementation Plan

## 1. Overview

The Google Gmail connector integrates with the Gmail REST API v1 to ingest, manage, search, and send email messages for a given Google account. It uses OAuth 2.0 Authorization Code Flow and delegates all HTTP calls to `GmailHTTPClient`, all response transforms to `helpers/normalizer.py`, and all utilities to `helpers/utils.py`.

**This is a MODIFICATION plan.** The connector already exists at `/Users/vivekvarshavaishvik/Documents/client_dir/google_gmail_c996c4_connector`. Every step below is an additive surgical change to the existing code. No file is deleted or replaced.

**Auth type:** `oauth2_code`

**Key capabilities (existing + new):**
- List, fetch, normalize, and sync Gmail messages
- Label management (add/remove labels via modify)
- Trash and permanent delete (messages and threads)
- Bulk delete by query
- **NEW:** Send email (RFC 2822 MIME, base64url-encoded) via `POST /users/me/messages/send`
- **NEW:** Create draft via `POST /users/me/drafts`
- **NEW:** `post_email()` public alias for `send_email()`
- **NEW:** `modify_message()` public method exposing the existing modify path directly

---

## 2. SDK / Package Selection

| Package | Version | Justification |
|---------|---------|---------------|
| `aiohttp` | `>=3.9.0` | Already in use for all HTTP calls |
| `structlog` | current | Already in use for structured logging |

**No new pip packages are required for this feature.** `send_email()` uses only Python stdlib:
- `email.mime.text.MIMEText` — construct RFC 2822 message
- `email.mime.multipart.MIMEMultipart` — support cc/bcc headers
- `base64.urlsafe_b64encode` — base64url encoding required by Gmail API

---

## 3. Auth Flow

1. **Install:** User provides `client_id` and `client_secret` via install_fields. The connector validates these are non-empty and returns `AuthStatus.PENDING`.

2. **Authorize:** The platform redirects the user to `AUTH_URI` with `client_id`, `scope` (now includes `gmail.send`), and `redirect_uri`. On callback, `authorize(auth_code)` POSTs to `TOKEN_URI` and receives `access_token`, `refresh_token`, `expires_in`, and `scope`.

3. **Token storage:** `set_token(TokenInfo(...))` persists the token via the SDK's Redis-backed store.

4. **Token refresh:** `on_token_refresh()` POSTs `grant_type=refresh_token` to `TOKEN_URI` using the stored `refresh_token`, `client_id`, and `client_secret`. The SDK's `ensure_token()` calls this automatically when `expires_at` is in the past.

5. **Send path:** `send_email()` calls `_build_http_client()` → `ensure_token()` → `GmailHTTPClient(access_token=...)`. Token refresh is fully handled before the send HTTP call is made.

---

## 4. Data Model

Gmail `messages.get` (format=full) → `NormalizedDocument`

| NormalizedDocument field | Gmail source |
|--------------------------|-------------|
| `id` | `message.id` |
| `source_id` | `message.id` |
| `title` | Header `Subject` (fallback: `"(no subject)"`) |
| `content` | Body part: `text/plain` preferred, `text/html` fallback, top-level body last resort; base64url-decoded |
| `content_type` | `"text"` for plain, `"html"` for HTML |
| `metadata.from` | Header `From` |
| `metadata.to` | Header `To` |
| `metadata.date` | Header `Date` |
| `metadata.labels` | `message.labelIds` |
| `metadata.thread_id` | `message.threadId` |
| `metadata.snippet` | `message.snippet` |

`send_email()` / `post_email()` return the raw Gmail API response dict (not a NormalizedDocument) — the response contains `{id, threadId}` of the sent message.

---

## 5. Key API Endpoints & Methods

### 5.1 `install()`
- **Endpoint:** None (validation only)
- **Logic:** Check `self.config.get("client_id")` and `self.config.get("client_secret")` are non-empty. Return `ConnectorStatus(health=HEALTHY, auth_status=PENDING)` if valid, else `(DEGRADED, INVALID_CREDENTIALS)`.
- **Already implemented.** No change needed.

### 5.2 `authorize()`
- **Endpoint:** `POST https://oauth2.googleapis.com/token` (or `self.config.get("token_url")`)
- **Request payload:** `{code, client_id, client_secret, redirect_uri, grant_type="authorization_code"}`
- **Response:** `{access_token, refresh_token, expires_in, scope, token_type}`
- **Storage:** `await self.set_token(TokenInfo(...))`
- **Already implemented.** No change needed.

### 5.3 `health_check()`
- **Endpoint:** `GET /users/me/profile`
- **Response:** `{emailAddress, messagesTotal, threadsTotal, historyId}`
- **Maps to:** `ConnectorStatus(health=HEALTHY, auth_status=CONNECTED, message="Connected as {emailAddress}")`
- **Already implemented.** No change needed.

### 5.4 `sync()`
- **Endpoints:** `GET /users/me/messages` (list) + `GET /users/me/messages/{id}?format=full` (fetch)
- **Pagination:** cursor-based via `nextPageToken`; loop until absent
- **Incremental:** `since` → `after:{unix_ts}` query param
- **Deletion propagation:** diff `known_ids` vs `current_ids`; call `_remove_from_kb()` for removed IDs
- **Returns:** `SyncResult(status, documents_found, documents_synced, documents_failed)`
- **Already implemented.** No change needed.

### 5.5 `list_emails()`
- **Note:** Existing method is named `list_email()`. The plan documents this as the canonical list operation.
- **Endpoint:** `GET /users/me/messages`
- **Request params:** `{q: query, maxResults: max_results, pageToken: page_token}`
- **Response:** `{messages: [{id, threadId}], nextPageToken, resultSizeEstimate}`
- **Returns:** raw page dict (caller loops via `nextPageToken`)
- **Delegates to:** `client.execute_list_messages()`
- **Already implemented.** No change needed.

### 5.6 `get_email(msg_id)`
- **Endpoint:** `GET /users/me/messages/{id}?format=full`
- **Response:** Full message resource with `payload.headers`, `payload.parts`, `body.data`
- **Returns:** `NormalizedDocument` via `normalize_message()`
- **Delegates to:** `client.execute_get_message()` → `normalize_message()`
- **Already implemented.** No change needed.

### 5.7 `modify_message(msg_id, add_label_ids, remove_label_ids)` ← NEW
- **Endpoint:** `POST /users/me/messages/{id}/modify`
- **Request payload:** `{addLabelIds: [...], removeLabelIds: [...]}`
- **Response:** Updated message resource `{id, threadId, labelIds}`
- **Returns:** raw response dict
- **Delegates to:** `client.execute_modify_message(msg_id, add_label_ids, remove_label_ids)`
- **New standalone public method** that directly exposes the modify path without going through add_email/update_email.

### 5.8 `read_email(msg_id)`
- **Endpoint:** `GET /users/me/messages/{id}?format=full`
- **Returns:** `NormalizedDocument`
- **Delegates to:** `client.execute_get_message()` → `normalize_message()`
- **Already implemented.** No change needed.

### 5.9 `add_email(msg_id, label_ids)`
- **Endpoint:** `POST /users/me/messages/{id}/modify`
- **Request payload:** `{addLabelIds: label_ids}`
- **Response:** Updated message resource
- **Returns:** raw response dict
- **Delegates to:** `client.execute_modify_message(msg_id, add_label_ids=label_ids)`
- **Already implemented.** No change needed.

### 5.10 `send_email(to, subject, body, cc, bcc)` ← NEW
- **Endpoint:** `POST /users/me/messages/send`
- **Request payload:** `{"raw": "<base64url-encoded RFC 2822 message>"}`
- **Response:** `{id: "<message_id>", threadId: "<thread_id>", labelIds: ["SENT"]}`
- **Pagination:** N/A (single send operation)
- **MIME construction:** Uses `_build_mime_raw()` helper (in `helpers/utils.py`):
  - Construct `MIMEText(body, "plain")` (or `MIMEMultipart` if cc/bcc present)
  - Set headers: `To`, `Subject`, `Cc` (if provided), `Bcc` (if provided)
  - Encode: `base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")`
- **Delegates to:** `client.execute_send_message(raw_message)`
- **Requires scope:** `https://www.googleapis.com/auth/gmail.send`
- **Error:** 403 → `ConnectorPermissionError("gmail.send scope missing — re-authorize the connector")`

### 5.11 `post_email(to, subject, body, cc, bcc)` ← NEW
- **Thin alias for `send_email()`** — same signature, same return type.
- Added for API surface completeness as explicitly requested.
- Delegates: `return await self.send_email(to, subject, body, cc, bcc)`

---

## 6. Error Handling

| HTTP Status | Exception | Retry? | Notes |
|-------------|-----------|--------|-------|
| 400 | `ConnectorError` with `error_description` | No | Bad MIME or malformed request |
| 401 | `ConnectorAuthError` | No | Token invalid; triggers re-auth |
| 403 | `ConnectorPermissionError` | No | Missing scope (esp. gmail.send) |
| 404 | `ConnectorNotFoundError` | No | Message/thread not found |
| 429 | `ConnectorRateLimitError` | Yes (exponential backoff) | Rate limited |
| 5xx | `ConnectorError` | Yes (up to 3 attempts) | Transient server error |

**`execute_send_message()` specific:**
- 403 → raise `ConnectorPermissionError("gmail.send scope missing — re-authorize the connector")`
- 400 → parse response JSON for `error.message`; raise `ConnectorError(message)`
- Other non-200 → `_raise_for_status()` (existing pattern)

**Retry strategy:** Existing `@retry()` decorator in `helpers/utils.py` handles exponential backoff with jitter. Applied to all `execute_*` methods in `GmailHTTPClient`. Non-retryable exceptions bypass the retry loop.

---

## 7. Dependencies

No new packages needed. All MIME encoding uses Python stdlib. Requirements file is unchanged:
```
aiohttp>=3.9.0
structlog
shielva-connector-sdk
```

---

## 8. Config & Install Fields

| Key | Type | Required | Source | Usage |
|-----|------|----------|--------|-------|
| `client_id` | string | ✓ | install_field (user) | OAuth token exchange and refresh |
| `client_secret` | secret | ✓ | install_field (user) | OAuth token exchange and refresh |
| `scopes` | string | ✗ | install_field (user) | Space-separated OAuth scopes; defaults to `REQUIRED_SCOPES` |
| `auth_url` / `authorization_url` | string | ✗ | install_field (user) | Override AUTH_URI |
| `token_url` | string | ✗ | install_field (user) | Override TOKEN_URI |
| `base_url` | string | ✗ | install_field (user) | Override Gmail API base URL |
| `rate_limit_per_min` | string | ✗ | install_field (user) | Override default 250 req/min |
| `pagination_type` | string | ✗ | install_field (user) | Override default "cursor" |
| `api_version` | string | ✗ | install_field (user) | Override default "v1" |
| `allow_permanent_delete` | boolean | ✗ | install_field (user) | Enable permanent delete |
| `redirect_uri` | string | ✗ | bind (platform) | Set by platform during OAuth flow |
| `known_message_ids` | list | ✗ | internal (persisted) | Delta-sync checkpoint |

**User-provided fields** (documented in setup instructions, appear in install_fields): `client_id`, `client_secret`, `scopes`, `auth_url`, `base_url`, `rate_limit_per_min`, `pagination_type`, `api_version`.

**Hardcoded/internal fields** (NEVER shown in setup instructions): `redirect_uri`, `known_message_ids`, `allow_permanent_delete`.

---

## 9. SOC/OCP Architecture Plan

### File-by-file responsibility

| File | Responsibility | What it NEVER does |
|------|---------------|-------------------|
| `connector.py` | Orchestration: calls `_build_http_client()`, invokes `execute_*` methods, calls `normalize_message()`, calls `load/save_known_ids()`, raises/catches typed exceptions, returns SDK types (`ConnectorStatus`, `SyncResult`, `NormalizedDocument`) | No raw HTTP, no JSON parsing, no retry logic |
| `client/http_client.py` | ALL HTTP calls via aiohttp; `_raise_for_status()` maps status codes to exceptions; `@retry()` decorator applied to every execute_* method | No business logic, no normalization, no config reads |
| `helpers/normalizer.py` | ALL response → `NormalizedDocument` transforms; MIME priority logic; base64url decoding of message bodies | No HTTP, no config |
| `helpers/utils.py` | `retry()` decorator, `load/save_known_ids()`, `extract_header()`, **NEW:** `_build_mime_raw()` MIME message builder | No HTTP, no normalization |
| `exceptions.py` | Exception hierarchy: `ConnectorError` → `ConnectorAuthError`, `ConnectorPermissionError`, `ConnectorNotFoundError`, `ConnectorRateLimitError` | No logic |

### SOC checks

1. ✅ `connector.py` zero raw HTTP — all calls via `client.execute_*()`
2. ✅ All HTTP in `client/http_client.py`
3. ✅ All transforms in `helpers/normalizer.py`
4. ✅ All utilities (retry, MIME builder, known-IDs) in `helpers/utils.py`
5. ✅ `connector.py` imports from `client/` and `helpers/` only

### OCP checks

6. ✅ Each operation is a standalone `async def` method; `sync()` is not overloaded
7. ✅ New operations (`send_email`, `post_email`, `modify_message`) added without modifying `BaseConnector` or any existing method
8. ✅ All config via `self.config.get("key")` — no hardcoded credentials
9. ✅ Retry/backoff in `helpers/utils.py`; rate limiting via decorator — not inlined
10. ✅ Error mapping in `exceptions.py` + `_ERROR_MAP` in `http_client.py`; `connector.py` catches only typed exceptions

### Surgical changes (5 total)

1. **`connector.py` line ~42:** Extend `REQUIRED_SCOPES` list to append `"https://www.googleapis.com/auth/gmail.send"`.

2. **`client/http_client.py`:** Add `execute_send_message(raw_message: str) -> Dict[str, Any]` — mirrors `execute_modify_message()` pattern: POST `{"raw": raw_message}` to `/users/me/messages/send`; catch 403 and raise `ConnectorPermissionError("gmail.send scope missing — re-authorize the connector")`.

3. **`helpers/utils.py`:** Add `_build_mime_raw(to, subject, body, cc, bcc) -> str` — builds `MIMEText`/`MIMEMultipart`, sets headers, base64url-encodes with `rstrip("=")`.

4. **`connector.py`:** Add three new public `async def` methods: `send_email()`, `post_email()`, `modify_message()`. All delegate to `_build_http_client()` then the appropriate `client.execute_*()` or `_build_mime_raw()`.

5. **`metadata/connector.json`:** Extend `"methods"` array with entries for `send_email`, `post_email`, `modify_message`; add `"https://www.googleapis.com/auth/gmail.send"` to the scopes documentation in the `install_fields[scopes].help` text.
