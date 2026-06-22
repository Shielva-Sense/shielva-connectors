# Gmail Connector — Implementation Plan

## 1. Overview

The Gmail connector integrates with the Google Gmail REST API (v1) to provide email reading, searching, modification, and sending capabilities for the Shielva platform. It wraps the Gmail API under the provider slug `google` / service `google_gmail_connector`.

**Auth type:** `oauth2_code` — OAuth 2.0 Authorization Code Grant with offline access (refresh tokens).

**Key capabilities:**
- Sync inbox messages into the Shielva knowledge base as `NormalizedDocument` objects
- List, retrieve, and modify individual messages
- Send emails via RFC 2822 MIME messages encoded as base64url
- Create draft emails
- Incremental sync via Gmail history API (`historyId` cursor)
- Built-in retry with exponential backoff and rate-limit handling

This is a **surgical modification** of an existing connector. All existing `GmailConnector` and `GmailHTTPClient` methods are preserved; the plan adds:
1. `https://www.googleapis.com/auth/gmail.send` to `REQUIRED_SCOPES`
2. `execute_send_message(raw_message)` to `GmailHTTPClient`
3. `send_email(to, subject, body, cc, bcc)` to `GmailConnector`
4. `read_email(message_id)`, `add_email(to, subject, body, cc, bcc)`, `post_email(to, subject, body, cc, bcc)` to `GmailConnector`

---

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `shielva-connector-sdk` | editable from monorepo | Provides `BaseConnector`, `TokenInfo`, `NormalizedDocument`, `SyncResult`, `ConnectorStatus` |
| `aiohttp` | `>=3.9` | Async HTTP client for Gmail API calls; mirrors existing http_client pattern |
| `structlog` | pre-installed | Structured logging throughout |
| `python-dateutil` | `>=2.8` | Robust ISO-8601 / RFC 2822 date parsing for email `Date:` headers |

**stdlib only (no new pip deps):**
- `email.mime.text.MIMEText` — build RFC 2822 MIME messages for send/draft
- `email.mime.multipart.MIMEMultipart` — multipart messages (cc/bcc headers)
- `base64.urlsafe_b64encode` — encode MIME payload per Gmail API spec
- `email.utils.formatdate` — RFC 2822 `Date:` header

No new pip packages are required beyond what's already installed in the shared venv.

---

## 3. Auth Flow

### Step-by-step OAuth 2.0 Authorization Code Grant

1. **install()** — validates `client_id` and `client_secret` from `self.config`. Returns `ConnectorStatus(auth_status=AuthStatus.MISSING_CREDENTIALS)` if either is absent.
2. **get_oauth_url()** — inherited from `BaseConnector`. Builds the Google OAuth2 authorization URL with `AUTH_URI`, `client_id`, `REQUIRED_SCOPES` (including `gmail.send`), `access_type=offline`, `prompt=consent`.
3. **authorize(auth_code, state)** — exchanges the code at `TOKEN_URI` (`https://oauth2.googleapis.com/token`) for `access_token` + `refresh_token`. Stores result via `await self.set_token(token_info)` which persists to Redis.
4. **_get_valid_token()** — checks `is_token_valid()`. If expired and `refresh_token` present, calls `on_token_refresh()` which POSTs to `TOKEN_URI` with `grant_type=refresh_token`. Stores the new token via `set_token()`. Raises `RefreshError` on failure.
5. All HTTP calls go through `_get_valid_token()` before execution — token is fetched once per request, not once per connector lifetime.

### Token storage
- `set_token()` → persists to Redis via `connector_store.save_connector_tokens()`
- `initialize()` → loads from Redis on connector startup

---

## 4. Data Model — NormalizedDocument Mapping

Gmail API message → `NormalizedDocument`:

| NormalizedDocument field | Gmail API source | Notes |
|---|---|---|
| `id` | `f"{self.connector_id}_{message['id']}"` | Namespaced to avoid collision |
| `source_id` | `message['id']` | Native Gmail message ID |
| `title` | `Subject` header | Extracted from `payload.headers` |
| `content` | Decoded body text | `payload.body.data` (base64url-decoded) or concatenated `parts[].body.data` |
| `content_type` | `"text"` | Always text; HTML stripped by normalizer |
| `source_url` | `f"https://mail.google.com/mail/u/0/#inbox/{message['id']}"` | Deep-link |
| `author` | `From` header | |
| `created_at` | `internalDate` | Unix ms → datetime |
| `updated_at` | `internalDate` | Same as created_at for Gmail |
| `metadata` | `{thread_id, label_ids, snippet, from, to, cc, bcc, date_header, history_id}` | All extra fields |
| `source` | `"google_gmail_connector"` | `CONNECTOR_TYPE` |
| `tenant_id` | `self.tenant_id` | |
| `connector_id` | `self.connector_id` | |

Body extraction order: `text/plain` part → `text/html` part (stripped) → `snippet` fallback.

---

## 5. Key API Endpoints & Methods

### 5.1 `install()`
- **Endpoint:** None (no API call at install time)
- **Logic:** Reads `client_id` and `client_secret` from `self.config`. If missing, returns `ConnectorStatus(connector_id=self.connector_id, health=ConnectorHealth.OFFLINE, auth_status=AuthStatus.MISSING_CREDENTIALS)`. If present, saves config and returns `ConnectorStatus(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.PENDING)`.
- **NormalizedDocument mapping:** N/A

### 5.2 `authorize(auth_code, state)`
- **Endpoint:** `POST https://oauth2.googleapis.com/token`
- **Request:** `{grant_type: authorization_code, code, client_id, client_secret, redirect_uri}`
- **Response:** `{access_token, refresh_token, expires_in, token_type, scope}`
- **Logic:** Builds `TokenInfo(access_token, refresh_token, expires_at=now+expires_in, scopes=scope.split())`, calls `set_token()`, returns `TokenInfo`.

### 5.3 `health_check()`
- **Endpoint:** `GET https://gmail.googleapis.com/gmail/v1/users/me/profile`
- **Logic:** Calls `http_client.get_profile()`. Success → `ConnectorStatus(health=HEALTHY, auth_status=CONNECTED)`. Auth error → `auth_status=TOKEN_EXPIRED`. Network error → `health=DEGRADED`.
- **NormalizedDocument mapping:** N/A

### 5.4 `sync(since, full, kb_id, webhook_url)`
- **Endpoints:**
  - Full: `GET /users/me/messages?maxResults=500&pageToken=<token>` (paginated)
  - Incremental: `GET /users/me/history?startHistoryId=<cursor>&historyTypes=messageAdded`
- **Pagination:** `nextPageToken` field; loop until absent.
- **Request params:** `maxResults=500`, `q="in:inbox"`, optional `pageToken`
- **Response:** `{messages: [{id, threadId}], nextPageToken, resultSizeEstimate}`
- **Logic:** For each message ID, call `get_message(id, format=full)`. Normalize via `helpers/normalizer.py`. Batch-ingest via `ingest_batch()`. Save `history_id` checkpoint via `set_metadata("last_history_id", ...)`.
- **Returns:** `SyncResult(status=COMPLETED, documents_synced=N, documents_found=N)`

### 5.5 `list_emails(query, max_results, page_token)`
- **Endpoint:** `GET /users/me/messages?q={query}&maxResults={max_results}&pageToken={page_token}`
- **Response:** `{messages: [{id, threadId}], nextPageToken}`
- **Returns:** Raw API response dict (not normalized — callers use `get_email()` for full docs)

### 5.6 `get_email(message_id)`
- **Endpoint:** `GET /users/me/messages/{message_id}?format=full`
- **Response:** Full Gmail message object with `payload`, `headers`, `body`, `parts`
- **Returns:** `NormalizedDocument` via normalizer

### 5.7 `modify_message(message_id, add_labels, remove_labels)`
- **Endpoint:** `POST /users/me/messages/{message_id}/modify`
- **Request body:** `{addLabelIds: [...], removeLabelIds: [...]}`
- **Response:** Updated message object
- **Returns:** Raw API response dict

### 5.8 `read_email(message_id)`
- **Endpoint:** `GET /users/me/messages/{message_id}?format=full` (delegates to `http_client.get_message()`)
- **Logic:** Thin wrapper that calls `self.http_client.get_message(message_id)` and returns the raw API response. No new HTTP path — reuses existing `get_message()` on `GmailHTTPClient`.
- **Returns:** Raw API response dict

### 5.9 `add_email(to, subject, body, cc, bcc)`
- **Endpoint:** `POST /users/me/drafts`
- **Request body:** `{message: {raw: <base64url-encoded RFC 2822 MIME string>}}`
- **Logic:** Builds `MIMEText` via `_build_mime_message(to, subject, body, cc, bcc)` (shared with `send_email`). Base64url-encodes with `rstrip('=')`. Delegates to `http_client.execute_create_draft(raw)`.
- **Returns:** Raw API draft response dict `{id, message: {id, threadId, labelIds}}`

### 5.10 `send_email(to, subject, body, cc, bcc)`
- **Endpoint:** `POST /users/me/messages/send`
- **Request body:** `{raw: <base64url-encoded RFC 2822 MIME string>}`
- **MIME construction:**
  - Use `email.mime.multipart.MIMEMultipart()` when `cc` or `bcc` are present; else `email.mime.text.MIMEText(body, 'plain')`
  - Set `To`, `From` (from token userinfo or `me`), `Subject`, `Date` headers
  - Attach cc/bcc headers if provided
  - `base64.urlsafe_b64encode(msg.as_bytes()).rstrip(b'=').decode('ascii')`
- **Delegates to:** `http_client.execute_send_message(raw_message)`
- **Error:** 403 → `PermissionError('gmail.send scope missing — re-authorize the connector')`
- **Returns:** Raw API response dict `{id, threadId, labelIds}`

### 5.11 `post_email(to, subject, body, cc, bcc)`
- **Logic:** Thin public alias — calls `return await self.send_email(to, subject, body, cc, bcc)`
- **Returns:** Same as `send_email`

---

## 6. Error Handling

| HTTP Status | Where caught | Exception raised | Fallback |
|---|---|---|---|
| 401 Unauthorized | `GmailHTTPClient` | `GmailConnectorError("Token expired")` | Trigger token refresh via `_get_valid_token()` |
| 403 Forbidden | `execute_send_message()` | `PermissionError("gmail.send scope missing — re-authorize the connector")` | Surface to caller |
| 403 Forbidden (other) | `GmailHTTPClient` | `GmailConnectorError("Permission denied")` | Log and re-raise |
| 400 Bad Request | `execute_send_message()` | `ValueError(<error_description from body>)` | Surface to caller |
| 429 Too Many Requests | `GmailHTTPClient` | Retry with exponential backoff (max 3 retries, base 1s, cap 32s) | `GmailConnectorError` after exhaustion |
| 5xx Server Error | `GmailHTTPClient` | Retry with exponential backoff | `GmailConnectorError` after exhaustion |
| Network timeout | `GmailHTTPClient` | `GmailConnectorError("Network timeout")` | Retry up to 3 times |

**Retry strategy:** Implemented as `helpers/utils.py::with_retry(coro, max_retries=3, base_delay=1.0, max_delay=32.0)`. Each retry waits `min(base_delay * 2^attempt + jitter, max_delay)`. Rate-limit (429) uses `Retry-After` header if present.

**Exception hierarchy** (`exceptions.py`):
```
GmailConnectorError(Exception)
  ├── GmailAuthError
  ├── GmailRateLimitError
  └── GmailAPIError
```

---

## 7. Dependencies

No new pip packages required. All send/draft functionality uses Python stdlib:

```bash
# Already in requirements.txt — no additions needed
# stdlib used: email.mime.text, email.mime.multipart, base64, email.utils
```

If `aiohttp` is not already in the shared venv:
```bash
pip install "aiohttp>=3.9"
```

The shared venv pre-installs: `pydantic`, `httpx`, `structlog`, `google-auth`, `pytest` plugins.

---

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | `string` | Required | install_field (user-provided) | OAuth2 Client ID from Google Cloud Console |
| `client_secret` | `secret` | Required | install_field (user-provided) | OAuth2 Client Secret |
| `scopes` | `string` | Optional | install_field (user-provided) | Space-separated scope list; defaults to `REQUIRED_SCOPES` |
| `auth_url` | `string` | Optional | install_field (user-provided) | Override `AUTH_URI`; defaults to Google's endpoint |
| `base_url` | `string` | Optional | install_field (user-provided) | Override Gmail API base; defaults to `https://gmail.googleapis.com/gmail/v1` |
| `rate_limit_per_min` | `integer` | Optional | install_field (user-provided) | Max requests/min; defaults to 250 |
| `pagination_type` | `string` | Optional | install_field (user-provided) | `"page_token"` (only valid value) |
| `api_version` | `string` | Optional | install_field (user-provided) | Gmail API version; defaults to `"v1"` |

**Hardcoded / internal (NOT user-provided, NOT install_fields):**
- `AUTH_URI = "https://accounts.google.com/o/oauth2/auth"` — class constant
- `TOKEN_URI = "https://oauth2.googleapis.com/token"` — class constant
- `REQUIRED_SCOPES` — class constant list
- OAuth redirect URIs — injected by the Shielva gateway at runtime
- Internal history/checkpoint keys stored in Redis metadata

---

## 9. SOC/OCP Architecture Plan

### File-by-file responsibility table

| File | Owns | Never contains |
|---|---|---|
| `connector.py` | Orchestration only: calls http_client + normalizer, returns SDK types (`ConnectorStatus`, `SyncResult`, `NormalizedDocument`), OAuth flow coordination | Raw HTTP calls, JSON parsing, MIME encoding logic |
| `client/http_client.py` | All HTTP: `get_message()`, `list_messages()`, `execute_modify_message()`, `execute_send_message()`, `execute_create_draft()`, `get_profile()`, `list_history()`. Owns retry logic, auth header injection, error-code-to-exception mapping | Business logic, normalization, SDK dataclass construction |
| `helpers/normalizer.py` | Response-to-`NormalizedDocument` transformation: header extraction, body decoding (base64url), HTML stripping, `internalDate` conversion | HTTP calls, authentication |
| `helpers/utils.py` | `with_retry()`, `build_mime_message()`, `base64url_encode()`, rate-limit token bucket | HTTP calls, normalization, SDK types |
| `exceptions.py` | `GmailConnectorError`, `GmailAuthError`, `GmailRateLimitError`, `GmailAPIError` | Logic |

### SOC compliance checklist (10/10)

1. ✅ `connector.py` — zero raw HTTP calls, zero JSON parsing
2. ✅ All HTTP calls in `client/http_client.py`
3. ✅ All response transformations in `helpers/normalizer.py`
4. ✅ All utilities (retry, MIME build, base64) in `helpers/utils.py`
5. ✅ `connector.py` imports from `client/` and `helpers/` — never reimplements
6. ✅ Each user operation is a standalone `async def` method — `send_email`, `read_email`, `add_email`, `post_email`, `modify_message`, `list_emails`, `get_email` are all independent methods, none folded into `sync()`
7. ✅ New operations (`send_email`, `add_email`, etc.) added without modifying `BaseConnector` or existing methods
8. ✅ All config values via `self.config.get("key")` — no hardcoded credentials or URLs
9. ✅ Retry + rate-limit implemented as `helpers/utils.py::with_retry()` — not inline
10. ✅ Error mapping in `exceptions.py` — `connector.py` catches `GmailConnectorError` only
