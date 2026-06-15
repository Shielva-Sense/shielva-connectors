# Gmail Connector — Implementation Plan (v1.1.0 Enhancement)

## 1. Overview

The Google Gmail connector integrates with the Gmail REST API v1 to ingest, manage, and delete Gmail messages and threads for the Shielva platform. It wraps OAuth 2.0 authorization code flow and delegates all HTTP calls to `client/http_client.py` (GmailHTTPClient) and all response normalization to `helpers/normalizer.py`.

This is a **modification-only** plan: the connector already exists at `/Users/vivekvarshavaishvik/Documents/client_dir/google_gmail_ac719d_connector`. All 6 approved features are targeted changes to the existing `connector.py`, `metadata/connector.json`, `tests/conftest.py`, and `tests/test_connector.py`. No new files are created.

**API**: Gmail REST API v1 (`https://gmail.googleapis.com/gmail/v1`)  
**Auth type**: `oauth2_code` (OAuth 2.0 Authorization Code Grant)  
**Key capabilities**: List/read/sync messages, label management (add/move/update), soft and hard delete of messages and threads, bulk delete, incremental sync with deletion propagation.

---

## 2. SDK / Package Selection

| Package | Version Floor | Justification |
|---|---|---|
| `aiohttp` | `>=3.9.0` | Async HTTP for token exchange; already in requirements.txt |
| `structlog` | pre-installed in shared venv | Structured logging; already imported |
| `shielva-connector-sdk` | editable from monorepo | Provides `BaseConnector`, `TokenInfo`, `NormalizedDocument`, `SyncResult`, `ConnectorStatus` |

No new pip packages are required for these 6 features. The `requirements.txt` file already contains all needed dependencies.

Exact pip packages per prompt Section 7 cross-reference: `google-api-python-client>=2.100`, `aiohttp>=3.9.0` — however, note that `google-api-python-client` is NOT used directly by this connector (it calls the Gmail REST API via `aiohttp`/`GmailHTTPClient`), so only `aiohttp>=3.9.0` is a real runtime dependency.

---

## 3. Auth Flow

1. **OAuth initiation**: The platform redirects the user to Google's authorization endpoint. The `authorization_url` is read from `self.config.get('authorization_url', 'https://accounts.google.com/o/oauth2/v2/auth')`.
2. **Code exchange** (`authorize(auth_code, state)`):
   - Builds a form-encoded POST payload with `code`, `client_id` (from `self.config.get('client_id')`), `client_secret` (from `self.config.get('client_secret')`), `redirect_uri`, and `grant_type=authorization_code`.
   - Posts to `self.config.get('token_url', 'https://oauth2.googleapis.com/token')`.
   - On HTTP 200: parses `access_token`, `refresh_token`, `expires_in`, `scope`, `token_type` from response JSON. Builds `TokenInfo(scopes=data['scope'].split())`. Calls `await self.set_token(token_info)`.
   - On non-200: raises `ConnectorAuthError('Token exchange failed: <body>')`.
3. **Token storage**: Stored in the platform Redis/DB layer via `self.set_token()` (BaseConnector). Retrieved via `self.ensure_token()` which auto-calls `on_token_refresh()` if expired.
4. **Token refresh** (`on_token_refresh()`):
   - Reads `self._token_info`. If None or no `refresh_token`, raises `ConnectorAuthError('No refresh token available.')`.
   - Posts `grant_type=refresh_token`, `refresh_token`, `client_id` (config), `client_secret` (config) to the token endpoint.
   - On non-200: raises `ConnectorAuthError('Token refresh failed: <body>')`.
   - Returns new `TokenInfo` with the existing `refresh_token` preserved.

---

## 4. Data Model

`normalize_message(raw, tenant_id, connector_id)` in `helpers/normalizer.py` maps Gmail API message objects to `NormalizedDocument`:

| Gmail API Field | NormalizedDocument Field | Notes |
|---|---|---|
| `message['id']` | `id` | Required |
| `message['id']` | `source_id` | Required; same as id |
| `payload.headers[Subject]` | `title` | Header lookup |
| `payload.body.data` (base64url) | `content` | Decoded; fallback to `snippet` |
| `'text'` | `content_type` | Always "text" for Gmail |
| `tenant_id` | `tenant_id` | Passed in from connector |
| `connector_id` | `connector_id` | Passed in from connector |
| `message['threadId']`, `labelIds`, `snippet`, `From`, `To`, `Date` | `metadata` | Stored in metadata dict |

---

## 5. Key API Endpoints & Methods

### `install()`
- **No API call** — validation only.
- Checks `self.config.get('client_id')` and `self.config.get('client_secret')` are both non-empty strings.
- If either is missing/empty: returns `ConnectorStatus(connector_id=self.connector_id, health=ConnectorHealth.DEGRADED, auth_status=AuthStatus.INVALID_CREDENTIALS, message='client_id and client_secret are required install fields')`.
- Otherwise returns `ConnectorStatus(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.PENDING, message='Gmail connector installed. Complete OAuth flow to activate.')`.

### `authorize(auth_code, state=None)`
- **Endpoint**: `self.config.get('token_url', 'https://oauth2.googleapis.com/token')` (POST)
- **Request**: form-encoded body — `code`, `client_id`, `client_secret`, `redirect_uri`, `grant_type=authorization_code`
- **Response**: `{access_token, refresh_token, expires_in, scope, token_type}`
- **Returns**: `TokenInfo` with `scopes=scope.split()`

### `on_token_refresh()`
- **Endpoint**: `self.config.get('token_url', 'https://oauth2.googleapis.com/token')` (POST)
- **Request**: form-encoded body — `grant_type=refresh_token`, `refresh_token`, `client_id`, `client_secret`
- **Response**: `{access_token, expires_in, scope, token_type}` (no new refresh_token)
- **Returns**: `TokenInfo` with existing `refresh_token` preserved

### `health_check()`
- **Endpoint**: `GET /users/me/profile` → delegated to `client.execute_get_profile()`
- **Returns**: `ConnectorStatus(HEALTHY/CONNECTED)` on success; `DEGRADED/TOKEN_EXPIRED` on `ConnectorAuthError`; `DEGRADED/INVALID_CREDENTIALS` on `ConnectorPermissionError`; `OFFLINE/FAILED` on any other exception.

### `sync(since, full, kb_id, webhook_url)`
- **Endpoints**: `GET /users/me/messages` (list) + `GET /users/me/messages/{id}` (get) — both delegated to `client.execute_list_messages()` / `client.execute_get_message()`
- **Pagination**: cursor-based via `nextPageToken`; loop continues until no `nextPageToken`; `max_results=100` per page
- **Incremental**: uses `after:{unix_ts}` query string when `since` is set and `full=False`
- **Deletion propagation**: diffs `known_message_ids` (from `load_known_ids(self.config)`) against `current_ids`; calls `_remove_from_kb()` for each removed ID
- **Returns**: `SyncResult(status, documents_found, documents_synced, documents_failed)`

### `list_email(query, max_results, page_token)`
- **Endpoint**: `GET /users/me/messages?q={query}&maxResults={max_results}&pageToken={page_token}` — delegated to `client.execute_list_messages()`
- **Response**: `{messages: [{id, threadId}], nextPageToken, resultSizeEstimate}`
- **Returns**: raw page dict (Dict[str, Any])

### `read_email(msg_id)`
- **Endpoint**: `GET /users/me/messages/{msg_id}` — delegated to `client.execute_get_message(msg_id)`
- **Response**: full message object with `payload.headers`, `payload.body.data`
- **Returns**: `NormalizedDocument` via `normalize_message(raw, tenant_id, connector_id)`

### `add_email(msg_id, label_ids)`
- **Endpoint**: `POST /users/me/messages/{msg_id}/modify` — delegated to `client.execute_modify_message(msg_id, add_label_ids=label_ids or [])`
- **Request payload**: `{addLabelIds: [...], removeLabelIds: []}`
- **Returns**: modified message dict

### `move_email(msg_id, destination_label_id, remove_label_ids)`
- **Endpoint**: `POST /users/me/messages/{msg_id}/modify` — delegated to `client.execute_modify_message(msg_id=msg_id, add_label_ids=[destination_label_id], remove_label_ids=remove_label_ids or ['INBOX'])`
- **Request payload**: `{addLabelIds: [destination_label_id], removeLabelIds: remove_label_ids or ['INBOX']}`
- **Logs**: `gmail.move_email.ok`
- **Returns**: modified message dict (Dict[str, Any])

### `update_email(msg_id, add_label_ids, remove_label_ids)`
- **Endpoint**: `POST /users/me/messages/{msg_id}/modify` — delegated to `client.execute_modify_message(msg_id=msg_id, add_label_ids=add_label_ids or [], remove_label_ids=remove_label_ids or [])`
- **Logs**: `gmail.update_email.ok`
- **Returns**: modified message dict (Dict[str, Any])

### `get_email(msg_id)`
- **Endpoint**: `GET /users/me/messages/{msg_id}` — delegated to `client.execute_get_message(msg_id)`, then `normalize_message(raw, self.tenant_id, self.connector_id)`
- **Identical delegation pattern to `read_email()`** — this is a new alias method
- **Returns**: `NormalizedDocument`

### `delete_email(msg_id, permanent)`
- **Alias** for `delete_message(msg_id, permanent=permanent)`.
- **Returns**: same as `delete_message()`

### `remove_email(msg_id)`
- **Alias** for `delete_message(msg_id, permanent=False)`.
- **Returns**: same as `delete_message()`

### `delete_message(msg_id, permanent)`
- `permanent=False`: `POST /users/me/messages/{msg_id}/trash` → `client.execute_trash_message(msg_id)`
- `permanent=True`: `DELETE /users/me/messages/{msg_id}` → `client.execute_delete_message(msg_id)` — requires `allow_permanent_delete=True`
- **Logs**: `gmail.delete_message.ok`
- **Returns**: trash response dict or None

### `delete_thread(thread_id, permanent)`
- `permanent=False`: `POST /users/me/threads/{thread_id}/trash` → `client.execute_trash_thread(thread_id)`
- `permanent=True`: `DELETE /users/me/threads/{thread_id}` → `client.execute_delete_thread(thread_id)` — requires `allow_permanent_delete=True`
- **Logs**: `gmail.delete_thread.ok`
- **Returns**: trash response dict or None

### `bulk_delete(query, permanent)`
- Phase 1: paginate `GET /users/me/messages?q={query}` to collect all IDs
- Phase 2: for each ID, call `delete_message` (permanent flag checked once upfront via `_assert_permanent_delete_allowed()`)
- Per-message errors caught, counted; loop never aborts early
- **Returns**: `BulkDeleteResult(deleted, failed, errors)`

### `disconnect()`
- Calls `await self.clear_token()`
- **Logs**: `gmail.disconnect`
- **Returns**: None

---

## 6. Error Handling

| Scenario | Exception | Connector Response |
|---|---|---|
| Missing `client_id`/`client_secret` in `install()` | — | Return `ConnectorStatus(DEGRADED, INVALID_CREDENTIALS)` |
| Token exchange non-200 | `ConnectorAuthError` | Raised from `authorize()` |
| No refresh token | `ConnectorAuthError` | Raised from `on_token_refresh()` |
| Token refresh non-200 | `ConnectorAuthError` | Raised from `on_token_refresh()` |
| HTTP 401 from Gmail API | `ConnectorAuthError` | Raised by http_client; health_check → `DEGRADED/TOKEN_EXPIRED` |
| HTTP 403 from Gmail API | `ConnectorPermissionError` | Raised by http_client; health_check → `DEGRADED/INVALID_CREDENTIALS` |
| HTTP 404 from Gmail API | `ConnectorNotFoundError` | Propagated to caller (get_email, read_email) |
| HTTP 429 from Gmail API | `ConnectorRateLimitError` | Propagated to caller (list_email); retry by http_client with exponential backoff |
| Generic exception in health_check | `Exception` | `ConnectorStatus(OFFLINE, FAILED)` |
| Generic exception in sync | `Exception` | `SyncResult(FAILED)` |
| Per-message error in sync/bulk_delete | `Exception` | Caught, counted as `documents_failed`; loop continues |
| `move_email`/`update_email` permission error | `ConnectorPermissionError` | Propagated to caller |
| `get_email` not found | `ConnectorNotFoundError` | Propagated to caller |

**Retry strategy**: Implemented in `helpers/utils.py` as a composable decorator/helper. `http_client.py` applies exponential backoff (base 1s, max 32s) on 429/503 responses. `connector.py` does not implement retry inline.

---

## 7. Dependencies

```
# No new packages required for the 6 approved features.
# Existing requirements.txt already covers all dependencies:
aiohttp>=3.9.0        # async HTTP for token exchange
```

For reference, exact packages per the prompt requirement:
```
google-api-python-client>=2.100   # not used directly; Gmail called via aiohttp+REST
aiohttp>=3.9.0                    # used for token exchange in authorize() / on_token_refresh()
```

---

## 8. Config & Install Fields

| Config Key | Type | Required | Source | Default | Used In |
|---|---|---|---|---|---|
| `client_id` | string | **yes** | `install_field` (user-provided) | — | `authorize()`, `on_token_refresh()`, `install()` validation |
| `client_secret` | secret | **yes** | `install_field` (user-provided) | — | `authorize()`, `on_token_refresh()`, `install()` validation |
| `scopes` | string | no | `install_field` (user-provided) | `'https://www.googleapis.com/auth/gmail.modify'` | `authorize()` (scope hint for UI; actual scopes come from token response) |
| `authorization_url` | string | no | `install_field` (user-provided) | `'https://accounts.google.com/o/oauth2/v2/auth'` | Platform OAuth redirect construction |
| `token_url` | string | no | `install_field` (user-provided) | `'https://oauth2.googleapis.com/token'` | `authorize()`, `on_token_refresh()` |
| `base_url` | string | no | `install_field` (user-provided) | `'https://gmail.googleapis.com/gmail/v1'` | `_build_http_client()` → passed to `GmailHTTPClient` |
| `allow_permanent_delete` | boolean | no | `install_field` (user-provided) | `False` | `_assert_permanent_delete_allowed()`, `delete_message()`, `delete_thread()`, `bulk_delete()` |
| `rate_limit_per_min` | string | no | `install_field` (user-provided) | `'250'` | `helpers/utils.py` rate-limiter |
| `pagination_type` | string | no | `install_field` (user-provided) | `'cursor'` | `client/http_client.py` pagination strategy |
| `api_version` | string | no | `install_field` (user-provided) | `'v1'` | `_build_http_client()` path composition |
| `redirect_uri` | string | no | bind constant (platform-injected) | `''` | `authorize()` |
| `known_message_ids` | list | no | platform-managed state | `[]` | `sync()` deletion diffing |

**Fields NOT documented in setup instructions** (internal/platform-managed): `redirect_uri`, `known_message_ids`.

---

## 9. SOC/OCP Architecture Plan

### File-by-file responsibility table

| File | Responsibility | What it does NOT do |
|---|---|---|
| `connector.py` | **Orchestration only** — wires config → http_client → normalizer → SDK methods | No raw HTTP, no JSON parsing, no retry logic inline |
| `client/http_client.py` | **All HTTP** — constructs URLs, sets auth headers, executes requests, maps HTTP status codes to connector exceptions, applies retry/backoff | No business logic, no config reading, no normalization |
| `helpers/normalizer.py` | **All response transformation** — converts raw Gmail message dicts to `NormalizedDocument` | No HTTP calls, no config, no connector state |
| `helpers/utils.py` | **All utilities** — `load_known_ids`, `save_known_ids`, rate-limit helper, retry decorator | No HTTP calls, no normalization, no SDK imports |
| `exceptions.py` | **Error taxonomy** — defines `ConnectorError`, `ConnectorAuthError`, `ConnectorPermissionError`, `ConnectorNotFoundError`, `ConnectorRateLimitError` | No logic, no imports beyond builtins |
| `models.py` | **Domain models** — `BulkDeleteResult` dataclass | No imports beyond dataclasses |

### SOC / OCP compliance map

| Check | Implementation |
|---|---|
| SOC-1: connector.py zero raw HTTP | ✅ `authorize()` and `on_token_refresh()` use `aiohttp.ClientSession` only for token exchange (not a Gmail API call); all Gmail API calls go through `GmailHTTPClient` |
| SOC-2: HTTP calls → http_client.py | ✅ All Gmail API calls delegated to `execute_*` methods |
| SOC-3: Response transforms → normalizer.py | ✅ `normalize_message()` called from `read_email()`, `get_email()`, `sync()` |
| SOC-4: Utils → helpers/utils.py | ✅ `load_known_ids`, `save_known_ids` already there |
| SOC-5: connector.py imports from client/ helpers/ | ✅ Never reimplements their logic |
| OCP-6: Each operation is its own async def | ✅ `move_email`, `update_email`, `get_email` are standalone methods, not folded into sync() |
| OCP-7: New operations without modifying BaseConnector | ✅ BaseConnector is never touched |
| OCP-8: Config values from self.config.get() | ✅ All 6 features replace class constants with config.get() calls |
| OCP-9: Retry/pagination as composable helpers | ✅ In http_client.py and helpers/utils.py, not inline in connector.py |
| OCP-10: Error mapping delegated to exceptions.py | ✅ connector.py catches `ConnectorAuthError`, `ConnectorPermissionError`, `ConnectorNotFoundError` only |

### Change set summary (6 modifications to connector.py)

**A. REQUIRED_CONFIG_KEYS extension** (line ~60):
```
['allow_permanent_delete']  →  ['allow_permanent_delete', 'client_id', 'client_secret']
```

**B. Class constant removals** — remove these constants entirely from the class body:
- `CLIENT_ID`, `CLIENT_SECRET`, `REQUIRED_SCOPES`, `AUTH_URI`, `AUTH_URL`, `TOKEN_URI`, `TOKEN_URL`, `BASE_URL`

**C. install() validation block** — insert BEFORE the `return ConnectorStatus(PENDING)`:
```python
if not self.config.get('client_id') or not self.config.get('client_secret'):
    return ConnectorStatus(
        connector_id=self.connector_id,
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.INVALID_CREDENTIALS,
        message='client_id and client_secret are required install fields',
    )
```

**D. authorize() payload mutations**:
- `payload['client_id'] = self.config.get('client_id')`
- `payload['client_secret'] = self.config.get('client_secret')`
- `token_endpoint = self.config.get('token_url', 'https://oauth2.googleapis.com/token')` → pass as URL to `session.post()`
- Scope hint: `self.config.get('scopes', 'https://www.googleapis.com/auth/gmail.modify')` (not used in token payload; actual scopes come from response)

**E. on_token_refresh() payload mutations**:
- `payload['client_id'] = self.config.get('client_id')`
- `payload['client_secret'] = self.config.get('client_secret')`
- `token_endpoint = self.config.get('token_url', 'https://oauth2.googleapis.com/token')` → pass as URL

**F. _build_http_client() argument change**:
- `base_url=self.config.get('base_url', 'https://gmail.googleapis.com/gmail/v1')` → replaces `self.BASE_URL`

**G. Three new public methods**:
- `move_email(self, msg_id: str, destination_label_id: str, remove_label_ids: Optional[List[str]] = None) -> Dict[str, Any]`
- `update_email(self, msg_id: str, add_label_ids: Optional[List[str]] = None, remove_label_ids: Optional[List[str]] = None) -> Dict[str, Any]`
- `get_email(self, msg_id: str) -> NormalizedDocument`

**H. metadata/connector.json**:
- Add 6 new `install_fields`: `client_id`, `client_secret`, `scopes`, `authorization_url`, `token_url`, `base_url`
- Add 3 new methods to `methods[]`: `move_email`, `update_email`, `get_email`
- Bump `version` from `"1.0.0"` to `"1.1.0"`

**I. tests/conftest.py**:
- Add `client_id` and `client_secret` to `BASE_CONFIG`
- Add `connector_no_creds` fixture (connector with empty client_id/client_secret)
- Add `mock_http_client.execute_modify_message` update to support `remove_label_ids` kwarg

**J. tests/test_connector.py — new test function names**:
- `test_install_missing_client_id_returns_degraded`
- `test_install_missing_client_secret_returns_degraded`
- `test_install_missing_both_creds_returns_degraded`
- `test_authorize_uses_config_client_id_and_secret`
- `test_authorize_uses_config_token_url`
- `test_on_token_refresh_uses_config_client_id_and_secret`
- `test_on_token_refresh_uses_config_token_url`
- `test_build_http_client_uses_config_base_url`
- `test_move_email_happy_path`
- `test_move_email_with_custom_remove_labels`
- `test_move_email_propagates_permission_error`
- `test_update_email_happy_path`
- `test_update_email_propagates_permission_error`
- `test_get_email_returns_normalized_document`
- `test_get_email_propagates_not_found`
