# Gmail Connector — Implementation Blueprint

## 1. Overview

This connector integrates Google Gmail with the Shielva platform, enabling ingestion of email messages from a user's Gmail inbox. It wraps the Gmail REST API v1 via the `google-api-python-client` Python library. Authentication is handled via OAuth2 Authorization Code flow, with scoped read-only access. The connector supports incremental sync (via `after:` query filter), full sync, token refresh, health checking, and per-message normalization into `NormalizedDocument`.

**Provider:** google  
**Service:** google_gmail_connector  
**Auth type:** `oauth2_code`  
**Key capabilities:** list emails, incremental sync, full sync, token refresh, health check, rate-limit handling, exponential backoff, pagination, structured logging.

---

## 2. SDK / Package Selection

| Package | Min version | Justification |
|---|---|---|
| `google-api-python-client` | 2.100.0 | Official Google Discovery client; provides `googleapiclient.discovery.build()` and `googleapiclient.errors.HttpError` |
| `google-auth` | 2.23.0 | Provides `google.oauth2.credentials.Credentials`, `google.auth.transport.requests.Request`, `google.auth.exceptions.RefreshError`, `google.auth.exceptions.TransportError` |
| `google-auth-httplib2` | 0.2.0 | Bridges `google-auth` with `httplib2` transport layer used by `google-api-python-client` |
| `google-auth-oauthlib` | 1.1.0 | OAuth2 flow helpers; used for auth code exchange |
| `aiohttp` | 3.9.0 | Async HTTP for token exchange POST to TOKEN_URI |
| `tenacity` | 8.2.0 | Composable retry/backoff decorator used in `helpers/utils.py` |

**Install command:**
```
pip install google-api-python-client>=2.100.0 google-auth>=2.23.0 google-auth-httplib2>=0.2.0 google-auth-oauthlib>=1.1.0 aiohttp>=3.9.0 tenacity>=8.2.0
```

---

## 3. Auth Flow

### Class-level constants (connector.py)
```
AUTH_TYPE    = 'oauth2_code'
AUTH_URI     = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URI    = 'https://oauth2.googleapis.com/token'
REQUIRED_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
```

### Step-by-step flow

**Step 1 — install():**  
Admin provides `client_id`, `client_secret` (and optionally `scopes`, `auth_url`, `token_url`, `base_url`, `rate_limit_per_min`, `pagination_type`, `api_version`) via the Shielva UI. `install()` validates `client_id` and `client_secret` are present, calls `self.save_config(config)`, and returns a `ConnectorStatus` with `health=HEALTHY`, `auth_status=PENDING`, `message="Authorization required — click Authorize to continue"`.

**Step 2 — authorize():**  
The Shielva platform redirects the user to `AUTH_URI` with query params: `client_id`, `redirect_uri` (read from `self.config.get('redirect_uri')`), `response_type=code`, `scope` (joined `REQUIRED_SCOPES`), `access_type=offline`, `prompt=consent`. After the user grants consent, Google redirects to the `redirect_uri` with `?code=...&state=...`.

`authorize(auth_data)` receives `auth_data['code']` and `auth_data.get('state')`. It reads `redirect_uri = self.config.get('redirect_uri')` (never hardcoded). It POSTs to `TOKEN_URI` with `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `client_secret`. The response contains `access_token`, `refresh_token`, `expires_in`, `token_type`, `scope`. It builds a `TokenInfo` with `expires_at = now + timedelta(seconds=expires_in)` and `scopes = scope.split()`. It stores via `self.set_token(token_info)` and returns the `TokenInfo`.

**Step 3 — Token refresh (http_client.py):**  
Before every API call, `http_client.py` builds a `google.oauth2.credentials.Credentials` object from the stored `TokenInfo` fields: `token=access_token`, `refresh_token=refresh_token`, `token_uri=TOKEN_URI`, `client_id=client_id`, `client_secret=client_secret`, `scopes=scopes`. If `credentials.expired` and `credentials.refresh_token` is set, it calls `credentials.refresh(google.auth.transport.requests.Request())`. After a successful refresh, it reads the updated `credentials.token`, `credentials.expiry`, and `credentials.scopes`, rebuilds a new `TokenInfo`, and calls the `set_token_callback` (passed in at construction from `connector.py`) to persist the new token.

**Token storage:** Tokens are stored in Redis via `self.set_token()` / `self.get_token()` provided by `BaseConnector`. The connector never manages token persistence directly.

---

## 4. Data Model

Raw Gmail message dict shape (from `execute_get_message` with `format='metadata'`):

```
{
  "id": "18abc...",
  "threadId": "18abc...",
  "snippet": "Short body preview text...",
  "payload": {
    "headers": [
      {"name": "Subject", "value": "Hello World"},
      {"name": "From",    "value": "sender@example.com"},
      {"name": "Date",    "value": "Fri, 13 Jun 2026 10:00:00 +0000"}
    ]
  }
}
```

### NormalizedDocument field mapping

| NormalizedDocument field | Source | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{message['id']}"` | Multi-tenant isolation; format defined in normalizer |
| `source_id` | `message['id']` | Raw Gmail message ID |
| `title` | `extract_header(headers, 'Subject')` or `"(no subject)"` | Falls back to empty string if missing |
| `content` | `truncate_preview(message['snippet'], max_chars=200)` | Body preview; snippet is already pre-truncated by Gmail |
| `content_type` | `"text"` | Always `"text"` for email previews |
| `metadata.sender` | `extract_header(headers, 'From')` | Raw From header string |
| `metadata.date` | `parse_gmail_date(extract_header(headers, 'Date'))` | Parsed to ISO-8601 datetime string |
| `metadata.thread_id` | `message['threadId']` | For thread-level grouping |
| `metadata.source_url` | `f"https://mail.google.com/mail/u/0/#inbox/{message['id']}"` | Deep link to message |
| `metadata.labels` | `message.get('labelIds', [])` | e.g. `['INBOX', 'UNREAD']` |

---

## 5. Key API Endpoints & Methods

### 5.1 `install(config: dict) -> ConnectorStatus`

**Purpose:** Validate and persist connector configuration.

**API calls:** None (no HTTP calls in install).

**Logic:**
- Check `config.get('client_id')` — if missing, return `ConnectorStatus(health=UNHEALTHY, auth_status=MISSING_CREDENTIALS, message="client_id is required")`
- Check `config.get('client_secret')` — if missing, same pattern
- Call `self.save_config(config)`
- Return `ConnectorStatus(connector_id=self.connector_id, health=HEALTHY, auth_status=PENDING, message="Authorization required — click Authorize to continue")`

---

### 5.2 `authorize(auth_data: dict) -> TokenInfo`

**Purpose:** Exchange OAuth2 authorization code for access + refresh tokens.

**Endpoint:** `POST https://oauth2.googleapis.com/token`

**Request params:**
```
grant_type=authorization_code
code=auth_data['code']
redirect_uri=self.config.get('redirect_uri')
client_id=self.config.get('client_id')
client_secret=self.config.get('client_secret')
```

**Response schema:**
```json
{
  "access_token": "ya29...",
  "refresh_token": "1//0...",
  "expires_in": 3599,
  "token_type": "Bearer",
  "scope": "https://www.googleapis.com/auth/gmail.readonly"
}
```

**Logic:**
- POST to TOKEN_URI with `aiohttp.ClientSession`
- Parse response JSON
- Compute `expires_at = datetime.utcnow() + timedelta(seconds=expires_in)`
- Build `TokenInfo(access_token=..., refresh_token=..., expires_at=expires_at, token_type=..., scopes=scope.split())`
- Call `self.set_token(token_info)`
- Return `token_info`

**Pagination:** N/A

---

### 5.3 `health_check() -> ConnectorStatus`

**Purpose:** Verify token validity and Gmail API reachability.

**Endpoint:** `GET https://gmail.googleapis.com/gmail/v1/users/me/profile`  
Via: `service.users().getProfile(userId='me').execute()`

**Request params:** None (auth token implicit via credentials object).

**Response schema:**
```json
{
  "emailAddress": "user@gmail.com",
  "messagesTotal": 12345,
  "threadsTotal": 5678,
  "historyId": "9876"
}
```

**Logic:**
- Call `http_client.execute_get_profile()`
- On success: return `ConnectorStatus(connector_id=self.connector_id, health=HEALTHY, auth_status=CONNECTED, message=f"Connected as {profile['emailAddress']}")`
- On `google.auth.exceptions.RefreshError`: return `ConnectorStatus(health=DEGRADED, auth_status=TOKEN_EXPIRED, message="Token refresh failed")`
- On `HttpError` 4xx/5xx: return `ConnectorStatus(health=UNHEALTHY, auth_status=FAILED, message=str(e))`

**Pagination:** N/A

---

### 5.4 `list_email(page_token: Optional[str] = None, label_ids: Optional[List[str]] = None) -> List[Dict]`

**Purpose:** Fetch metadata for emails in the inbox. Handles full pagination internally.

**Endpoint — list:** `GET https://gmail.googleapis.com/gmail/v1/users/me/messages`  
Via: `service.users().messages().list(userId='me', labelIds=['INBOX','UNREAD'], maxResults=100, pageToken=nextPageToken)`

**Endpoint — get per message:** `GET https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}`  
Via: `service.users().messages().get(userId='me', id=msg_id, format='metadata', metadataHeaders=['Subject','From','Date'])`

**Request params (list):**
```
userId='me'
labelIds=['INBOX', 'UNREAD']   (default; overridable)
maxResults=100
pageToken=<nextPageToken from previous page>
q=<optional query string, e.g. 'after:1718000000'>
```

**Request params (get):**
```
userId='me'
id=<message id>
format='metadata'
metadataHeaders=['Subject', 'From', 'Date']
```
The `snippet` field is returned automatically by the API alongside `format='metadata'`.

**Response schema (list):**
```json
{
  "messages": [{"id": "...", "threadId": "..."}],
  "nextPageToken": "optional string",
  "resultSizeEstimate": 100
}
```

**Pagination strategy:**  
Loop: call `execute_list_messages(page_token=None)`. If `nextPageToken` in response, call again with `page_token=response['nextPageToken']`. Continue until no `nextPageToken`. Collect all `messages` arrays.

**Per-message fetch:**  
For each `{id}` from the paginated list, call `execute_get_message(msg_id, format='metadata', metadata_headers=['Subject','From','Date'])`. Return list of raw message dicts.

**Return:** `List[Dict]` — one dict per message, with keys: `id`, `threadId`, `snippet`, `payload.headers`.

---

### 5.5 `sync(full: bool = False, since: Optional[datetime] = None) -> SyncResult`

**Purpose:** Orchestrate email ingestion into Shielva — incremental or full.

**Endpoint:** same as `list_email()` — delegates entirely to it.

**Logic:**
- If `full=True` or `since is None`: call `list_email()` with no date filter
- If `full=False` and `since` is provided: pass `q=build_after_query(since)` (e.g. `'after:1718000000'`) to `list_email()`
- Iterate results, call `normalizer.normalize(raw_message, tenant_id=self.tenant_id)` for each
- Call `self.index_documents(normalized_docs)` (BaseConnector method)
- Track `documents_found = len(raw_messages)`, `documents_synced = len(successfully indexed)`, `documents_failed = failures`
- Return `SyncResult(status=COMPLETED, documents_synced=..., documents_found=..., documents_failed=...)`
- On exception: return `SyncResult(status=FAILED, message=str(e))`

**Pagination:** Handled inside `list_email()` — sync receives the complete flat list.

**NormalizedDocument mapping:** Delegated to `helpers/normalizer.py` — connector.py never performs field mapping.

---

## 6. Error Handling

### HTTP error mapping (HttpError from googleapiclient.errors)

| HTTP status | Action |
|---|---|
| 401 | Attempt token refresh via `credentials.refresh()`; if refresh succeeds, retry once; if `RefreshError`, raise `GmailAuthError` |
| 403 | Raise `GmailAuthError` → maps to `AuthStatus.MISSING_CREDENTIALS` |
| 429 | Raise `GmailRateLimitError` → `tenacity` retry with exponential backoff (initial=1s, multiplier=2, max=60s, max_attempts=5) |
| 5xx | Retry up to 3 times with exponential backoff (initial=1s, multiplier=2); if still failing, raise `GmailAPIError` |

### Google Auth exceptions

| Exception | Handling |
|---|---|
| `google.auth.exceptions.RefreshError` | Caught in `http_client.py`; `connector.py` receives it and returns `ConnectorStatus(health=DEGRADED, auth_status=TOKEN_EXPIRED)` |
| `google.auth.exceptions.TransportError` | Wrapped in `GmailAPIError`; logged with `tenant_id`, `connector_id`; re-raised |

### Exception hierarchy (exceptions.py)
```
GmailBaseError(Exception)
├── GmailAuthError(GmailBaseError)       # 401/403 → MISSING_CREDENTIALS / TOKEN_EXPIRED
├── GmailRateLimitError(GmailBaseError)  # 429 → triggers backoff
└── GmailAPIError(GmailBaseError)        # 5xx / transport errors
```

### Retry strategy
- Implemented as a composable `retry_with_backoff(func, *args)` helper in `helpers/utils.py` using `tenacity.retry` with `wait_exponential`, `stop_after_attempt`, and `retry_if_exception_type`.
- `http_client.py` calls this helper — retry logic is never inlined in `connector.py`.

---

## 7. Dependencies

```
# requirements.txt
google-api-python-client>=2.100.0
google-auth>=2.23.0
google-auth-httplib2>=0.2.0
google-auth-oauthlib>=1.1.0
aiohttp>=3.9.0
tenacity>=8.2.0
```

**Install command:**
```bash
pip install -r requirements.txt
```

---

## 8. Config & Install Fields

| Config key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | str | Required | install_field (user-provided) | Google OAuth2 Client ID from GCP Console |
| `client_secret` | str | Required | install_field (user-provided) | Google OAuth2 Client Secret from GCP Console |
| `scopes` | str | Optional | install_field (user-provided) | Space-separated OAuth scopes; defaults to `REQUIRED_SCOPES` |
| `auth_url` | str | Optional | install_field (user-provided) | Override for AUTH_URI; defaults to `https://accounts.google.com/o/oauth2/v2/auth` |
| `token_url` | str | Optional | install_field (user-provided) | Override for TOKEN_URI; defaults to `https://oauth2.googleapis.com/token` |
| `base_url` | str | Optional | install_field (user-provided) | Override for Gmail API base; defaults to `https://gmail.googleapis.com` |
| `rate_limit_per_min` | int | Optional | install_field (user-provided) | Max quota units per minute; used by rate-limiter in utils.py |
| `pagination_type` | str | Optional | install_field (user-provided) | Pagination strategy identifier; defaults to `"page_token"` |
| `api_version` | str | Optional | install_field (user-provided) | Gmail API version; defaults to `"v1"` |
| `redirect_uri` | str | Internal | Bound by platform at runtime | Set by Shielva platform; never user-configured; never hardcoded |

**Note on `redirect_uri`:** This is injected into `self.config` by the Shielva platform at runtime. The connector reads it via `self.config.get('redirect_uri')` exclusively. It is NOT an install_field and must NOT be documented as one.

---

## 9. SOC/OCP Architecture Plan

### File responsibility table

| File | Sole responsibility | Must NOT contain |
|---|---|---|
| `connector.py` | Orchestration only — calls http_client methods, calls normalizer, calls BaseConnector lifecycle hooks (`save_config`, `set_token`, `index_documents`), returns SDK dataclasses (`ConnectorStatus`, `SyncResult`, `TokenInfo`) | Raw HTTP calls, JSON parsing, field mapping, retry logic, token refresh logic |
| `client/http_client.py` | Build `google.oauth2.credentials.Credentials`; call `credentials.refresh()` when expired; build `googleapiclient.discovery.build()` service object; expose `execute_list_messages()`, `execute_get_message()`, `execute_get_profile()` methods; map `HttpError` status codes to custom exceptions from `exceptions.py`; call `retry_with_backoff` from `helpers/utils.py` | Business logic, data normalization, response field mapping, `NormalizedDocument` construction |
| `helpers/normalizer.py` | Map raw Gmail message dicts to `NormalizedDocument`; call `extract_header()`, `parse_gmail_date()`, `truncate_preview()` from `helpers/utils.py`; construct `id = f"{tenant_id}_{message_id}"`; construct `source_url` | HTTP calls, auth logic, config access |
| `helpers/utils.py` | `parse_gmail_date(header_value) -> Optional[datetime]`; `extract_header(headers, name) -> Optional[str]`; `truncate_preview(text, max_chars=200) -> str`; `build_after_query(since: datetime) -> str`; `retry_with_backoff` tenacity decorator factory | State, HTTP calls, NormalizedDocument construction |
| `exceptions.py` | Define `GmailBaseError`, `GmailAuthError`, `GmailRateLimitError`, `GmailAPIError` exception classes | Logic of any kind |

### SOC compliance mapping (5 checks)

1. **connector.py ONLY orchestrates** — all methods delegate to http_client / normalizer; connector.py has zero `aiohttp` / `requests` / `googleapiclient` imports
2. **All HTTP calls in client/http_client.py** — the only file that imports `googleapiclient.discovery` and `google.oauth2.credentials`
3. **All response transformations in helpers/normalizer.py** — the only file that constructs `NormalizedDocument`
4. **All utilities in helpers/utils.py** — the only file that implements date parsing, header extraction, truncation, query building, and retry composition
5. **connector.py imports from client/ and helpers/** — never reimplements their logic inline

### OCP compliance mapping (5 checks)

6. **Each operation is a standalone `async def`** — `list_email()`, `health_check()`, `install()`, `authorize()`, `sync()` are independent methods; `list_email()` is NOT folded into `sync()`
7. **New operations can be added** — adding `list_drafts()` or `list_sent()` requires only a new method in connector.py + a new execute_ method in http_client.py; BaseConnector and existing methods untouched
8. **Config via `self.config.get("key")`** — no hardcoded client IDs, secrets, URLs, or scopes anywhere in the codebase
9. **Features as composable helpers** — retry/backoff is `retry_with_backoff` in utils.py; rate limiting is a `RateLimiter` class in utils.py; pagination loop is in http_client.py; none of these are inlined in connector.py
10. **Error mapping in exceptions.py** — http_client.py raises `GmailAuthError` / `GmailRateLimitError` / `GmailAPIError`; connector.py catches only these custom exceptions, never raw `HttpError`
