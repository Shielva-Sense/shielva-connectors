# Gmail Connector — Implementation Plan

## 1. Overview

This connector integrates Google Gmail with the Shielva platform, enabling bidirectional email operations for enterprise tenants. It wraps the Gmail REST API v1 via the `google-api-python-client` SDK.

**Provider:** Google
**Service:** shielva_gmail
**Auth Type:** OAuth2 Authorization Code Flow
**Key Capabilities:**
- List, fetch, search, and normalize Gmail messages into `NormalizedDocument`
- Send emails with optional attachments (up to 25 MB total)
- Delete (trash or permanent) emails
- Incremental sync using Gmail search query `after:{epoch}` for time-bounded fetches
- Cursor-based pagination via `nextPageToken`
- Token refresh using stored `refresh_token`
- Multi-tenancy via tenant-scoped document IDs and per-token API isolation

---

## 2. SDK / Package Selection

| Package | Version | Purpose |
|---|---|---|
| `google-api-python-client` | `>=2.100` | Official Gmail REST API client; provides `build('gmail','v1')` and `googleapiclient.errors.HttpError` |
| `google-auth` | `>=2.20` | OAuth2 credential management; `google.oauth2.credentials.Credentials`; `google.auth.exceptions.RefreshError` |
| `google-auth-oauthlib` | `>=1.0` | OAuth2 Authorization Code flow helpers; `Flow.from_client_config()` |
| `google-auth-httplib2` | `>=0.1` | httplib2 transport adapter for google-auth + google-api-python-client |
| `structlog` | `>=23.0` | Structured logging bound with `tenant_id` and `connector_id` |
| `aiohttp` | `>=3.9` | Async HTTP for token endpoint calls (authorize/refresh) |

**Justification:**
- `google-api-python-client` is the official, maintained Google client library for all Google REST APIs.
- `google-auth` + `google-auth-oauthlib` handle the OAuth2 code exchange and credential refresh natively.
- `structlog` provides consistent, machine-readable logs per Shielva logging policy.

---

## 3. Auth Flow

### 3.1 OAuth2 Authorization Code Flow

**Constants (connector.py module-level):**
```
AUTH_TYPE       = 'oauth2_code'
AUTH_URI        = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URI       = 'https://oauth2.googleapis.com/token'
REQUIRED_SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.send'
]
```

### 3.2 install()

1. Call `self.save_config(config)` to persist user-provided fields.
2. Return `ConnectorStatus(connector_id=self.connector_id, health=ConnectorHealth.PENDING, auth_status=AuthStatus.PENDING, message='Click Authorize to connect your Gmail account')`.
3. No OAuth flow initiated yet.

### 3.3 authorize(auth_data)

1. Read `client_id` = `self.config.get('client_id')`, `client_secret` = `self.config.get('client_secret')`.
2. If either is missing, return `ConnectorStatus(..., auth_status=AuthStatus.MISSING_CREDENTIALS)`.
3. Read `redirect_uri` = `self.config.get('redirect_uri')` — NEVER hardcoded.
4. Read `scopes` = `self.config.get('scopes') or REQUIRED_SCOPES`.
5. Exchange authorization `code` from `auth_data` at `TOKEN_URI` using `aiohttp.ClientSession.post`.
6. Parse response: extract `access_token`, `refresh_token`, `expires_in` (convert to `expires_at = now + timedelta(seconds=expires_in)`), `token_type`, `scope`.
7. Build `TokenInfo(access_token=..., refresh_token=..., expires_at=..., token_type=..., scopes=[...])`.
8. Call `self.set_token(token_info)` to persist in Redis.
9. Return `ConnectorStatus(..., auth_status=AuthStatus.AUTHENTICATED)`.

### 3.4 Token Refresh

1. Before every client construction in `_get_client()`, call `self.get_token()`.
2. If `token.expires_at` is within 60 seconds of now, POST to `TOKEN_URI` with `grant_type=refresh_token` and `refresh_token`.
3. On success: update `TokenInfo` and call `self.set_token(updated_token)`.
4. On `google.auth.exceptions.RefreshError` or HTTP 401 response: raise `GmailAuthError('Token refresh failed')`.
5. Return a new `GmailHttpClient` built with the fresh credentials.

### 3.5 _get_client()

1. Retrieve token via `self.get_token()`.
2. If `None`, raise `GmailAuthError('No token found')`.
3. Refresh if near expiry (as above).
4. Build `google.oauth2.credentials.Credentials` from token fields.
5. Instantiate `GmailHttpClient(credentials)` and cache on `self._client`.
6. Return `self._client`.

---

## 4. Data Model

### NormalizedDocument Field Mapping

| NormalizedDocument Field | Source (Gmail message resource) | Notes |
|---|---|---|
| `id` | `f'{tenant_id}:{connector_id}:{message.id}'` | Globally unique across tenants |
| `source_id` | `message['id']` | Raw Gmail message ID |
| `title` | `headers['Subject']` | From `payload.headers` list |
| `content` | Decoded body text | `payload.parts[*].body.data` (base64url decoded); prefer `text/plain`; fall back to `text/html` |
| `content_type` | `"text"` | Always `"text"` after normalization |
| `metadata.from` | `headers['From']` | Sender address |
| `metadata.to` | `headers['To']` | Recipient address(es) |
| `metadata.cc` | `headers['Cc']` | Optional |
| `metadata.date` | `headers['Date']` | RFC 2822 date string |
| `metadata.labels` | `message['labelIds']` | e.g. `['INBOX', 'UNREAD']` |
| `metadata.thread_id` | `message['threadId']` | Gmail thread grouping |
| `metadata.snippet` | `message['snippet']` | Short preview |
| `metadata.next_page_token` | `response['nextPageToken']` | Set on list results only |

---

## 5. Key API Endpoints & Methods

### 5.1 install()

**Purpose:** Persist config; prompt user to authorize.
**API Endpoint:** None (no external call).
**Steps:**
1. `self.save_config(config)`
2. Return `ConnectorStatus(connector_id=self.connector_id, health=ConnectorHealth.PENDING, auth_status=AuthStatus.PENDING, message='Click Authorize to connect your Gmail account')`

---

### 5.2 authorize(auth_data)

**Purpose:** Exchange OAuth2 code for tokens; persist via `set_token()`.
**API Endpoint:** `POST https://oauth2.googleapis.com/token`
**Request Payload:**
```
{
  "code": auth_data["code"],
  "client_id": self.config.get("client_id"),
  "client_secret": self.config.get("client_secret"),
  "redirect_uri": self.config.get("redirect_uri"),
  "grant_type": "authorization_code"
}
```
**Response Schema:**
```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_in": 3600,
  "token_type": "Bearer",
  "scope": "https://www.googleapis.com/auth/gmail.modify ..."
}
```
**Pagination:** None.
**NormalizedDocument:** Not applicable.

---

### 5.3 health_check()

**Purpose:** Verify token validity and API reachability.
**API Endpoint:** `GET https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=1`
**Steps:**
1. Call `self.get_token()`; if `None` → return `ConnectorStatus(..., health=ConnectorHealth.OFFLINE, auth_status=AuthStatus.MISSING_CREDENTIALS)`.
2. Call `client.list_messages(max_results=1)` as a probe.
3. On success → return `ConnectorStatus(..., health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)`.
4. On `GmailAuthError` → return `ConnectorStatus(..., health=ConnectorHealth.DEGRADED, auth_status=AuthStatus.EXPIRED)`.
5. On other error → return `ConnectorStatus(..., health=ConnectorHealth.DEGRADED, message=str(e))`.

**Pagination:** None (maxResults=1).
**NormalizedDocument:** Not applicable.

---

### 5.4 sync()

**Purpose:** Incremental full-mailbox sync using `after:{epoch}` Gmail search query.
**API Endpoint:** `GET https://gmail.googleapis.com/gmail/v1/users/me/messages?q=after:{epoch}&pageToken={token}&maxResults={page_size}`
**Request Parameters:**
- `q`: `after:{epoch}` where epoch = last sync timestamp (Unix seconds); defaults to 0 for full sync.
- `pageToken`: cursor from previous page's `nextPageToken`.
- `maxResults`: `self.config.get('page_size', 20)`.
**Response Schema:**
```json
{
  "messages": [{"id": "...", "threadId": "..."}],
  "nextPageToken": "...",
  "resultSizeEstimate": 100
}
```
**Pagination Strategy:** Loop calling `client.list_messages(q=..., page_token=next_token)` until `nextPageToken` absent.
**Steps:**
1. Compute `since_epoch` from last sync state.
2. Paginate through all message IDs.
3. Batch-fetch full messages via `client.get_message(message_id)`.
4. Normalize each via `normalize_message(raw, tenant_id, connector_id)`.
5. Call `self.save_documents(normalized_docs)`.
6. Update last sync timestamp.
7. Return `SyncResult(status=SyncStatus.COMPLETED, documents_synced=n, documents_found=total, documents_failed=failed_count)`.

---

### 5.5 list_emails(page_token=None, max_results=20, query=None)

**Purpose:** Return a page of email summaries.
**API Endpoint:** `GET https://gmail.googleapis.com/gmail/v1/users/me/messages`
**Request Parameters:**
- `pageToken`: optional cursor.
- `maxResults`: page size.
- `q`: optional Gmail search query string.
**Response Schema:** Same as sync() list response.
**Pagination Strategy:** Single-page; returns `nextPageToken` in metadata of each `NormalizedDocument`.
**NormalizedDocument mapping:** Fetch full message for each ID via `client.get_message()`; normalize via `normalize_message()`.

---

### 5.6 list_email(message_id)

**Purpose:** Fetch a single email by Gmail message ID.
**API Endpoint:** `GET https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}?format=full`
**Request Parameters:**
- `id`: Gmail message ID.
- `format`: `full`.
**Response Schema:** Full Gmail message resource (id, threadId, labelIds, snippet, payload, ...).
**Pagination:** None.
**NormalizedDocument mapping:** Normalize via `normalize_message(raw, tenant_id, connector_id)`. Raises `GmailMessageNotFoundError` on HTTP 404.

---

### 5.7 search_email(query, page_token=None, max_results=20)

**Purpose:** Search Gmail messages with a query string.
**API Endpoint:** `GET https://gmail.googleapis.com/gmail/v1/users/me/messages?q={query}&pageToken={token}&maxResults={n}`
**Request Parameters:**
- `q`: Gmail search query (e.g. `from:foo@example.com subject:hello`).
- `pageToken`: optional cursor.
- `maxResults`: page size.
**Response Schema:** Same as list_emails.
**Pagination Strategy:** Single-page; `nextPageToken` returned in metadata.
**NormalizedDocument mapping:** Full fetch + normalize per message ID.

---

### 5.8 send_email(to, subject, body, cc=None, bcc=None, reply_to=None, attachments=None)

**Purpose:** Send an email from the authenticated user's Gmail account.
**API Endpoint:** `POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send`
**Pre-send Validation (in helpers/utils.py):**
1. `validate_email_address(to)` — RFC 5322 regex; raise `GmailValidationError` on failure.
2. `calculate_attachment_size(attachments)` — raise `GmailAttachmentError` if total > 25 MB.
3. `build_raw_email_message(to, subject, body, cc, bcc, reply_to, attachments)` — returns base64url-encoded RFC 2822 message string.
**Request Payload:**
```json
{"raw": "<base64url_encoded_mime_message>"}
```
**Response Schema:**
```json
{"id": "...", "threadId": "...", "labelIds": ["SENT"]}
```
**Pagination:** None.
**NormalizedDocument:** Not applicable.
**Error:** HTTP 400 → `GmailAPIError('Invalid MIME content format for send operation')`.

---

### 5.9 delete_email(message_id, permanent=False)

**Purpose:** Trash or permanently delete an email.
**API Endpoints:**
- Trash: `POST https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}/trash`
- Permanent: `DELETE https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}`
**Request Parameters:**
- `message_id`: Gmail message ID.
- `permanent`: bool; if `False` → trash; if `True` → permanent delete.
**Response Schema:**
- Trash: full message resource with `TRASH` label.
- Permanent: HTTP 204 No Content.
**Pagination:** None.
**NormalizedDocument:** Not applicable.
**Error:** HTTP 404 → `GmailMessageNotFoundError('Invalid messageId: {id}')`.

---

## 6. Error Handling

### 6.1 Exception Hierarchy (exceptions.py)

```
GmailConnectorError (base)
├── GmailAuthError            — token missing, refresh failed, 403 insufficient scope
├── GmailAPIError             — generic API error, 400 malformed MIME
├── GmailMessageNotFoundError — 404 message not found
├── GmailRateLimitError       — 429 too many requests
├── GmailAttachmentError      — attachment > 25 MB
└── GmailValidationError      — invalid email address format
```

### 6.2 HTTP Status → Exception Mapping (http_client.py)

| HTTP Status | Exception |
|---|---|
| 400 | `GmailAPIError('Invalid MIME content format for send operation')` |
| 401 | `GmailAuthError('OAuth token invalid or expired')` |
| 403 | `GmailAuthError('OAuth token lacks required Gmail API permissions')` |
| 404 | `GmailMessageNotFoundError('Invalid messageId: {id}')` |
| 429 | `GmailRateLimitError('Too many requests')` |
| 5xx | `GmailAPIError('Gmail API server error: {status}')` |

### 6.3 Retry Strategy (http_client.py)

- `MAX_RETRIES = 3`, `INITIAL_BACKOFF_S = 1.0`, `BACKOFF_FACTOR = 2.0`
- Retry on HTTP 429 and 5xx.
- On 429: read `Retry-After` header; sleep for `max(header_value, backoff_seconds)`.
- On 5xx: sleep for `backoff_seconds`; backoff = `INITIAL_BACKOFF_S * BACKOFF_FACTOR^attempt`.
- After `MAX_RETRIES` exhausted: raise the mapped exception.
- `google.auth.exceptions.RefreshError` → raise `GmailAuthError('Token refresh failed')`.

---

## 7. Dependencies

### requirements.txt
```
google-api-python-client>=2.100
google-auth>=2.20
google-auth-oauthlib>=1.0
google-auth-httplib2>=0.1
aiohttp>=3.9
structlog>=23.0
```

### Install command
```bash
pip install google-api-python-client>=2.100 google-auth>=2.20 google-auth-oauthlib>=1.0 google-auth-httplib2>=0.1 aiohttp>=3.9 structlog>=23.0
```

---

## 8. Config & Install Fields

| Key | Type | Required | Source | Description |
|---|---|---|---|---|
| `client_id` | str | Yes | install_field (user-provided) | Google OAuth2 Client ID from Google Cloud Console |
| `client_secret` | str | Yes | install_field (user-provided) | Google OAuth2 Client Secret from Google Cloud Console |
| `scopes` | list[str] | No | install_field (user-provided) | OAuth2 scopes; defaults to `REQUIRED_SCOPES` if not set |
| `auth_url` | str | No | install_field (user-provided) | Authorization URL; defaults to `AUTH_URI` constant |
| `token_url` | str | No | install_field (user-provided) | Token exchange URL; defaults to `TOKEN_URI` constant |
| `base_url` | str | No | install_field (user-provided) | Gmail API base URL; defaults to `https://gmail.googleapis.com/gmail/v1` |
| `rate_limit_per_min` | int | No | install_field (user-provided) | Max requests per minute; used by rate limiter in http_client.py |
| `pagination_type` | str | No | install_field (user-provided) | Pagination strategy; defaults to `cursor` (nextPageToken) |
| `api_version` | str | No | install_field (user-provided) | Gmail API version; defaults to `v1` |
| `redirect_uri` | str | Internal | Bound by Shielva platform at runtime | OAuth2 redirect URI; read via `self.config.get('redirect_uri')` — NEVER hardcoded |
| `page_size` | int | No | Derived default | Default page size; defaults to 20 |

**Hardcoded constants (NOT in install_fields; NOT documented to users):**
- `AUTH_TYPE = 'oauth2_code'`
- `AUTH_URI = 'https://accounts.google.com/o/oauth2/v2/auth'`
- `TOKEN_URI = 'https://oauth2.googleapis.com/token'`
- `REQUIRED_SCOPES` list
- `MAX_ATTACHMENT_SIZE_MB = 25`
- `DEFAULT_PAGE_SIZE = 20`

---

## 9. SOC/OCP Architecture Plan

### File Responsibility Table

| File | Responsibilities | MUST NOT contain |
|---|---|---|
| `connector.py` | Orchestration only: call `_get_client()`, delegate to `client/` and `helpers/`, call `self.save_documents()`, return `ConnectorStatus`/`SyncResult`/`NormalizedDocument`. | Raw HTTP calls, JSON parsing, base64 encoding, retry logic, direct `build()` or `.execute()` calls |
| `client/http_client.py` | All HTTP calls via `google-api-python-client`; wraps `build('gmail','v1')`; exposes named async methods (`list_messages`, `get_message`, `send_message`, `trash_message`, `delete_message_permanent`); HTTP error mapping to domain exceptions; retry + backoff logic; `Retry-After` header parsing; rate limiting | Business logic, document normalization, config reads |
| `helpers/normalizer.py` | Transform raw Gmail message resource into `NormalizedDocument`; header extraction; body decoding (base64url); content_type selection (`text/plain` > `text/html`) | HTTP calls, config reads, encoding helpers |
| `helpers/utils.py` | `build_raw_email_message()` — MIME construction + base64url encoding; `validate_email_address()` — RFC 5322 regex; `calculate_attachment_size()` — size guard | HTTP calls, normalization, SDK imports |
| `exceptions.py` | Define exception hierarchy: `GmailConnectorError`, `GmailAuthError`, `GmailAPIError`, `GmailMessageNotFoundError`, `GmailRateLimitError`, `GmailAttachmentError`, `GmailValidationError` | Logic of any kind |
| `connector.py` imports | `from client.http_client import GmailHttpClient`, `from helpers.normalizer import normalize_message`, `from helpers.utils import build_raw_email_message, validate_email_address, calculate_attachment_size`, `from exceptions import GmailAuthError, GmailMessageNotFoundError, ...` | Reimplementing logic from any imported module |

### SOC Compliance Checklist

1. ✅ `connector.py` — zero raw HTTP calls, zero JSON parsing.
2. ✅ All HTTP delegated to `client/http_client.py`.
3. ✅ All response transformations in `helpers/normalizer.py`.
4. ✅ All utilities in `helpers/utils.py`.
5. ✅ `connector.py` imports from `client/` and `helpers/` only.

### OCP Compliance Checklist

6. ✅ Each operation (`list_emails`, `send_email`, `delete_email`, `list_email`, `search_email`) is a standalone `async def` — not folded into `sync()`.
7. ✅ New operations added without modifying `BaseConnector` or existing methods.
8. ✅ All config via `self.config.get("key")` — no hardcoded credentials or URLs in methods.
9. ✅ Retry, pagination, rate-limiting implemented as helpers in `http_client.py` — not inline in `connector.py`.
10. ✅ Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only.
