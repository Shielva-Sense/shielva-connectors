# Gmail Connector — Implementation Plan (Scope Upgrade + Delete/Trash Methods)

## 1. Overview

The Google Gmail connector wraps the Gmail REST API v1 to ingest email messages into the Shielva platform. It authenticates via OAuth2 Authorization Code flow (with offline access for token refresh), stores tokens via the `set_token()` / `get_token()` SDK abstractions, and surfaces email content as `NormalizedDocument` records for downstream indexing.

**This plan is ADDITIVE**: all existing functionality (install, authorize, health_check, sync, list_email, on_token_refresh) is preserved unchanged. Three new standalone methods are appended to the existing files:
- `trash_email(msg_id)` — moves a message to Trash (reversible)
- `delete_email(msg_id)` — permanently deletes a single message
- `batch_delete_emails(msg_ids)` — permanently deletes a batch of messages in one API call

The OAuth scope set is upgraded from `gmail.readonly` to `gmail.modify` + `https://mail.google.com/` to cover all write and delete operations.

---

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `google-api-python-client` | `>=2.100.0` | Provides `googleapiclient.discovery.build()` and the typed Gmail service object already used in `http_client.py` |
| `google-auth-httplib2` | `>=0.2.0` | Transport adapter required by google-api-python-client |
| `google-auth-oauthlib` | `>=1.1.0` | OAuth2 helpers; `google.oauth2.credentials.Credentials` already imported in connector.py |
| `aiohttp` | `>=3.9.0` | Used in `authorize()` for the token exchange POST; already a dependency |
| `tenacity` | `>=8.2.0` | Retry decorators `retry_on_rate_limit` / `retry_on_server_error` already in utils.py |
| `structlog` | (existing) | Structured logging already used throughout |

No new packages required. All three new methods reuse existing service construction and retry infrastructure.

---

## 3. Auth Flow

1. **install()** — admin provides `client_id` and `client_secret` (plus optional overrides). Config is validated and persisted via `save_config()`. Returns `PENDING` auth status.
2. **authorize()** — platform redirects user to Google consent screen with the upgraded `REQUIRED_SCOPES`. On redirect-back, platform calls `authorize(auth_data={"code": "..."})`. The connector POSTs to the token endpoint (aiohttp) and stores the returned `TokenInfo` via `set_token()`.
3. **ensure_token()** (SDK-provided) — before every API call, `_build_http_client()` calls `ensure_token()`. If the access token is expired, the SDK calls `on_token_refresh()`.
4. **on_token_refresh()** — uses `google.oauth2.credentials.Credentials.refresh()` with the stored `refresh_token`. Updates the stored token via `set_token()`.

Token storage: delegated entirely to the SDK (`set_token` / `get_token`) — connector.py never touches Redis directly.

---

## 4. Data Model

Gmail REST API `users.messages.get` (format=metadata) → `NormalizedDocument`:

| NormalizedDocument field | Gmail API source |
|---|---|
| `id` | `f"{tenant_id}_{message['id']}"` |
| `source_id` | `message['id']` |
| `title` | `payload.headers[name='Subject']` or "(no subject)" |
| `content` | `message['snippet']` (truncated to 200 chars) |
| `content_type` | `"text"` (hardcoded) |
| `source_url` | `f"https://mail.google.com/mail/u/0/#inbox/{id}"` |
| `author` | `payload.headers[name='From']` |
| `created_at` | `payload.headers[name='Date']` parsed via `parse_gmail_date()` |
| `metadata.sender` | same as author |
| `metadata.thread_id` | `message['threadId']` |
| `metadata.source_url` | same as source_url |
| `metadata.labels` | `message['labelIds']` |
| `metadata.date` | `parsed_date.isoformat()` if available |

The three new destructive methods (`trash_email`, `delete_email`, `batch_delete_emails`) do not produce `NormalizedDocument` output — they return `None` on success and raise on error.

---

## 5. Key API Endpoints & Methods

### 5.1 `install(config)`
- **Endpoint**: none (local validation only)
- **Parameters**: `config: Dict[str, Any]` — must contain `client_id`, `client_secret`
- **Returns**: `ConnectorStatus(health=HEALTHY, auth_status=PENDING)` on success; `UNHEALTHY + MISSING_CREDENTIALS` if either required key is absent
- **NormalizedDocument**: N/A

### 5.2 `authorize(auth_data)`
- **Endpoint**: `POST https://oauth2.googleapis.com/token` (or `token_url` override)
- **Payload**: `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `client_secret`
- **Response**: `{ access_token, refresh_token, expires_in, scope, token_type }`
- **Returns**: `TokenInfo(access_token, refresh_token, expires_at, token_type, scopes)`
- **NormalizedDocument**: N/A

### 5.3 `health_check()`
- **Endpoint**: `GET /gmail/v1/users/me/profile` (via `execute_get_profile()`)
- **Parameters**: none
- **Response**: `{ emailAddress, messagesTotal, threadsTotal, historyId }`
- **Returns**: `ConnectorStatus(health=HEALTHY, auth_status=CONNECTED, metadata={"email": ...})` on success
- **Error mapping**: 401 → `TOKEN_EXPIRED`; 403 → `MISSING_CREDENTIALS`

### 5.4 `sync(since, full, kb_id, webhook_url)`
- **Delegates to**: `list_email()` then `normalizer.normalize_batch()` then `ingest_batch()`
- **Incremental**: builds `after:{epoch}` query via `build_after_query(since)` when `full=False` and `since` is provided
- **Full**: no query filter — fetches all INBOX+UNREAD messages
- **Returns**: `SyncResult(status, documents_found, documents_synced, documents_failed)`
- **NormalizedDocument**: produced by `normalizer.normalize()`

### 5.5 `read_email(msg_id)`
- **Endpoint**: `GET /gmail/v1/users/me/messages/{id}` via `execute_get_message()`
- **Parameters**: `msg_id: str`, `format="full"`, `metadata_headers=["Subject","From","Date"]`
- **Response**: full message resource
- **Returns**: raw dict from the API
- **NormalizedDocument**: caller normalizes if needed

### 5.6 `add_email(raw_message_b64)`
- **Endpoint**: `POST /gmail/v1/users/me/messages/import` (Gmail users.messages.import)
- **Parameters**: `raw: str` — base64url-encoded RFC 2822 message
- **Response**: minimal message resource `{ id, threadId, labelIds }`
- **Returns**: raw dict
- **NormalizedDocument**: N/A — import, not indexing

### 5.7 `delete_email(msg_id)`
- **Endpoint**: `DELETE /gmail/v1/users/me/messages/{id}` via `execute_delete_message(msg_id)`
- **Parameters**: `msg_id: str`
- **Scope required**: `https://mail.google.com/`
- **Response**: HTTP 204 No Content on success (no body)
- **Error mapping**: 404 → `GmailNotFoundError`; 403 → `GmailAuthError` (insufficient scope)
- **Audit log**: `logger.warning("gmail.delete_email.audit", op="delete_email", msg_id=msg_id, tenant_id=self.tenant_id, connector_id=self.connector_id)`
- **Returns**: `None`
- **NormalizedDocument**: N/A

### 5.8 `remove_email(msg_id)`
- **Endpoint**: Alias to `delete_email()` — delegates directly
- **Returns**: `None`
- **NormalizedDocument**: N/A

### 5.9 `list_message(label_ids, query, page_token, max_results)`
- **Endpoint**: `GET /gmail/v1/users/me/messages` via `execute_list_messages()`
- **Parameters**: `label_ids`, `q` (query string), `pageToken`, `maxResults` (max 500)
- **Response**: `{ messages: [{id, threadId}], nextPageToken, resultSizeEstimate }`
- **Pagination**: cursor-based via `nextPageToken`; loop until absent
- **Returns**: flat list of message stub dicts
- **NormalizedDocument**: N/A (stubs only)

### 5.10 `update_email(msg_id, add_label_ids, remove_label_ids)`
- **Endpoint**: `POST /gmail/v1/users/me/messages/{id}/modify` (users.messages.modify)
- **Payload**: `{ addLabelIds: [...], removeLabelIds: [...] }`
- **Response**: full message resource with updated `labelIds`
- **Returns**: raw dict
- **NormalizedDocument**: N/A

### 5.11 `label_email(msg_id, label_ids)`
- **Endpoint**: `POST /gmail/v1/users/me/messages/{id}/modify` via `execute_modify_message()`
- **Payload**: `{ addLabelIds: label_ids, removeLabelIds: [] }`
- **Response**: full message resource
- **Returns**: raw dict
- **NormalizedDocument**: N/A

### 5.12 `trash_email(msg_id)`
- **Endpoint**: `POST /gmail/v1/users/me/messages/{id}/trash` via `execute_trash_message(msg_id)`
- **Parameters**: `msg_id: str`
- **Scope required**: `https://www.googleapis.com/auth/gmail.modify`
- **Response**: full message resource with `TRASH` added to `labelIds`
- **Error mapping**: 404 → `GmailNotFoundError`; 403 → `GmailAuthError`
- **Returns**: raw dict (message resource)
- **NormalizedDocument**: N/A — reversible operation, not an ingestion event

### 5.13 `batch_delete_emails(msg_ids)`
- **Endpoint**: `POST /gmail/v1/users/me/messages/batchDelete` via `execute_batch_delete_messages(msg_ids)`
- **Payload**: `{ ids: [msg_id, ...] }` — max 1000 IDs per call (Gmail API contract)
- **Scope required**: `https://mail.google.com/`
- **Response**: HTTP 204 No Content on success
- **Validation** (in connector.py, before delegating): `len(msg_ids) == 0` → `ValueError`; `len(msg_ids) > 1000` → `ValueError`
- **Audit log**: `logger.warning("gmail.batch_delete_emails.audit", op="batch_delete_emails", count=len(msg_ids), tenant_id=self.tenant_id, connector_id=self.connector_id)`
- **Error mapping**: 403 → `GmailAuthError` (insufficient scope); 400 → `GmailAPIError`
- **Returns**: `None`
- **NormalizedDocument**: N/A

---

## 6. Error Handling

| HTTP Status | Trigger | Exception raised | Handler |
|---|---|---|---|
| 401 | Invalid/expired token | `GmailAuthError` | `_map_http_error()` in http_client.py |
| 403 | Insufficient OAuth scope | `GmailAuthError` | `_map_http_error()` in http_client.py |
| 404 | Message not found | `GmailNotFoundError` (new) | `_map_http_error()` in http_client.py |
| 429 | Rate limit exceeded | `GmailRateLimitError` | `retry_on_rate_limit` decorator (existing) |
| 5xx | Server error | `GmailAPIError` | `retry_on_server_error` decorator (existing) |
| Transport | Network failure | `GmailAPIError` | Caught in each execute_* method |

**New exception to add to exceptions.py:**
```
class GmailNotFoundError(GmailBaseError): ...
```

**_HTTP_ERROR_MAP update in http_client.py:**
```
404: (GmailNotFoundError, "HTTP 404 not found")
```

**Connector-layer validation (before http_client delegation):**
- `batch_delete_emails([])` → `raise ValueError("msg_ids must be a non-empty list")`
- `batch_delete_emails(list of 1001)` → `raise ValueError("batch_delete_emails supports at most 1000 message IDs per call")`

**Retry strategy:**
- Rate-limit errors (429): `retry_on_rate_limit` — up to 5 attempts, exponential backoff, max 60 s
- Server errors (5xx): `retry_on_server_error` — up to 3 attempts, max 30 s
- Auth errors (401/403): not retried — surfaced immediately to the caller
- 404: not retried — surfaced immediately as `GmailNotFoundError`

---

## 7. Dependencies

All packages already declared in `requirements.txt`. No new packages are needed for the three new methods:

```
pip install google-api-python-client>=2.100.0
pip install google-auth-httplib2>=0.2.0
pip install google-auth-oauthlib>=1.1.0
pip install aiohttp>=3.9.0
pip install tenacity>=8.2.0
```

The new methods call `service.users().messages().trash()`, `.delete()`, and `.batchDelete()` — all provided by `google-api-python-client` with no additional SDK.

---

## 8. Config & Install Fields

| Config key | Type | Required | Source | Read via |
|---|---|---|---|---|
| `client_id` | string | Yes | install_field (user-provided) | `self.config.get("client_id")` |
| `client_secret` | secret | Yes | install_field (user-provided) | `self.config.get("client_secret")` |
| `scopes` | string | No | install_field (user-provided, space-separated) | `self.config.get("scopes")` |
| `auth_url` | string | No | install_field (user-provided override) | `self.config.get("auth_url")` |
| `token_url` | string | No | install_field (user-provided override) | `self.config.get("token_url")` |
| `base_url` | string | No | install_field (user-provided override) | `self.config.get("base_url")` |
| `rate_limit_per_min` | string | No | install_field (user-provided) | `self.config.get("rate_limit_per_min")` |
| `pagination_type` | string | No | install_field (user-provided) | `self.config.get("pagination_type")` |
| `api_version` | string | No | install_field (user-provided, default "v1") | `self.config.get("api_version")` |
| `redirect_uri` | string | No | bind constant (set by platform at OAuth callback) | `self.config.get("redirect_uri")` |

**Hardcoded / internal constants (NOT install_fields, NOT documented to users):**
- `AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"` (overridable via `auth_url`)
- `TOKEN_URI = "https://oauth2.googleapis.com/token"` (overridable via `token_url`)
- `REQUIRED_SCOPES` — not user-facing; shown in consent screen by Google

---

## 9. SOC/OCP Architecture Plan

### File responsibility table

| File | Owns | Must NOT contain |
|---|---|---|
| `connector.py` | Orchestration; calls http_client + normalizer; validates input; emits audit logs; returns SDK types | Raw HTTP calls; JSON parsing; retry logic |
| `client/http_client.py` | All `execute_*` methods; builds Gmail service object; maps HttpError → custom exceptions | Token refresh; OAuth flow; business logic |
| `helpers/normalizer.py` | `normalize()` and `normalize_batch()`; field mapping from raw dict to NormalizedDocument | HTTP calls; config access |
| `helpers/utils.py` | `parse_gmail_date`, `extract_header`, `truncate_preview`, `build_after_query`, `make_retry_decorator`, pre-built decorators | HTTP calls; API-specific logic |
| `exceptions.py` | Exception hierarchy: `GmailBaseError`, `GmailAuthError`, `GmailRateLimitError`, `GmailAPIError`, `GmailNotFoundError` (new) | Logic of any kind |

### SOC compliance — 5 checks

1. `connector.py` orchestrates only — zero raw HTTP calls: **PASS** (all calls via `_build_http_client()` → `http_client.execute_*`)
2. HTTP calls delegated to `client/http_client.py`: **PASS** (three new `execute_*` methods added there)
3. Response transformations in `helpers/normalizer.py`: **PASS** (new destructive methods return None, no normalization needed)
4. Utilities in `helpers/utils.py`: **PASS** (retry decorators reused, no new util code needed)
5. `connector.py` imports from `client/` and `helpers/` only: **PASS**

### OCP compliance — 5 checks

6. Each operation is a standalone `async def` — not folded into `sync()`: **PASS** (`trash_email`, `delete_email`, `batch_delete_emails` are independent methods)
7. New operations added without modifying `BaseConnector` or existing methods: **PASS** (append-only changes)
8. Config from `self.config.get("key")` — no hardcoded credentials: **PASS**
9. Retry implemented as composable decorators (`retry_on_rate_limit`, `retry_on_server_error`): **PASS** (reused from utils.py)
10. Error mapping in `exceptions.py` + `_map_http_error()` — connector.py catches custom exceptions only: **PASS**

**Projected SOC/OCP score: 10/10**

### Additive changes summary

| File | Change type | Description |
|---|---|---|
| `connector.py` | Edit | Upgrade `REQUIRED_SCOPES`; append `trash_email()`, `delete_email()`, `remove_email()`, `batch_delete_emails()` |
| `client/http_client.py` | Edit | Add `404` to `_HTTP_ERROR_MAP`; append `execute_trash_message()`, `execute_delete_message()`, `execute_batch_delete_messages()` |
| `exceptions.py` | Edit | Append `GmailNotFoundError` |
| `metadata/connector.json` | Edit | Update `scopes` field default text; add new method entries |
| `tests/test_connector.py` | Edit | Append unit tests for the three new connector methods |
| `tests/conftest.py` | Edit if needed | Add mock fixtures for the three new http_client methods |
