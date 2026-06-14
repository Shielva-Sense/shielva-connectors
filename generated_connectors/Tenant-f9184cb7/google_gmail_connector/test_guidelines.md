# Gmail Connector — Test Guidelines

## 1. Package Structure

```
tests/
├── __init__.py
├── conftest.py          # shared fixtures
└── test_connector.py    # unit tests for all connector methods
```

---

## 2. Connector Class Details

**Class name:** `GmailConnector`  
**Module:** `connector`  
**Import:** `from connector import GmailConnector`

**Constructor:**
```python
GmailConnector(
    tenant_id: str,
    connector_id: str,
    config: Dict[str, Any] = None,
)
```

**Public methods (all `async def`):**

| Method | Signature |
|---|---|
| `install` | `async def install(self, config: Dict[str, Any] = None) -> ConnectorStatus` |
| `authorize` | `async def authorize(self, auth_data: Dict[str, Any]) -> TokenInfo` |
| `health_check` | `async def health_check(self) -> ConnectorStatus` |
| `sync` | `async def sync(self, since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult` |
| `list_email` | `async def list_email(self, label_ids=None, query=None) -> List[Dict]` |
| `on_token_refresh` | `async def on_token_refresh(self) -> TokenInfo` |

---

## 3. Client/SDK Layer

The HTTP client is `client.http_client.GmailHTTPClient`. It wraps `googleapiclient.discovery.build()`.

**Mock strategy:** patch `GmailHTTPClient` at its import path in `connector`:
```
unittest.mock.patch("connector.GmailHTTPClient")
```

This replaces the class so that `_build_http_client()` (which calls `GmailHTTPClient(...)`) returns a `MagicMock`. Configure `execute_*` methods on the returned instance.

---

## 4. Required Packages

```
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.1
pytest-cov>=4.1.0
pytest-timeout==2.4.0
```

---

## 5. Fixture Blueprint — conftest.py

```python
"""Shared fixtures for Gmail connector unit tests."""
import sys
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add connector root to path — never use absolute paths
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GmailConnector
from shared.base_connector import AuthStatus, ConnectorHealth, TokenInfo


CONNECTOR_CONFIG = {
    "client_id":          "test_client_id",
    "client_secret":      "test_client_secret",
    "scopes":             "https://www.googleapis.com/auth/gmail.readonly",
    "auth_url":           "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url":          "https://oauth2.googleapis.com/token",
    "base_url":           "https://gmail.googleapis.com",
    "rate_limit_per_min": 100,
    "pagination_type":    "page_token",
    "api_version":        "v1",
    "redirect_uri":       "https://app.example.com/oauth/callback",
}


@pytest.fixture
def connector():
    """Return a GmailConnector instance pre-configured with test credentials."""
    return GmailConnector(
        tenant_id="test_tenant",
        connector_id="test_connector_id",
        config=dict(CONNECTOR_CONFIG),
    )


@pytest.fixture
def valid_token():
    """Return a valid (non-expired) TokenInfo."""
    return TokenInfo(
        access_token="test_access_token",
        refresh_token="test_refresh_token",
        expires_at=datetime(2099, 12, 31),
        token_type="Bearer",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )


@pytest.fixture
def connector_with_token(connector, valid_token):
    """Return a connector that already has a valid token set."""
    connector._token_info = valid_token
    return connector


@pytest.fixture
def mock_http_client():
    """Return a pre-configured mock GmailHTTPClient instance."""
    mock = MagicMock()
    mock.execute_get_profile = AsyncMock(return_value={
        "emailAddress": "user@gmail.com",
        "messagesTotal": 5,
    })
    mock.execute_list_messages = AsyncMock(return_value={
        "messages": [{"id": "msg1", "threadId": "t1"}],
        "nextPageToken": None,
    })
    mock.execute_get_message = AsyncMock(return_value={
        "id": "msg1",
        "threadId": "t1",
        "snippet": "Hello world",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Test Subject"},
                {"name": "From",    "value": "sender@example.com"},
                {"name": "Date",    "value": "Fri, 13 Jun 2026 10:00:00 +0000"},
            ]
        },
    })
    return mock
```

---

## 6. Mock Patterns

### Patching the HTTP client
```python
with patch("connector.GmailHTTPClient", return_value=mock_http_client):
    result = await connector_with_token.health_check()
```

### Patching aiohttp for authorize()
```python
mock_response = MagicMock()
mock_response.json = AsyncMock(return_value={
    "access_token": "new_access",
    "refresh_token": "new_refresh",
    "expires_in": 3600,
    "token_type": "Bearer",
    "scope": "https://www.googleapis.com/auth/gmail.readonly",
})
mock_response.raise_for_status = MagicMock()

mock_post_cm = MagicMock()
mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
mock_post_cm.__aexit__ = AsyncMock(return_value=None)

mock_session = MagicMock()
mock_session.post = MagicMock(return_value=mock_post_cm)
mock_session_cm = MagicMock()
mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
mock_session_cm.__aexit__ = AsyncMock(return_value=None)

with patch("connector.aiohttp.ClientSession", return_value=mock_session_cm):
    token = await connector.authorize({"code": "auth_code"})
```

### Patching on_token_refresh (for ensure_token path)
```python
connector._token_info = expired_token
with patch.object(connector, "on_token_refresh", AsyncMock(return_value=valid_token)):
    ...
```

### Patching google Credentials refresh (for on_token_refresh)
```python
mock_creds = MagicMock()
mock_creds.token = "new_access_token"
mock_creds.refresh_token = "new_refresh_token"
mock_creds.expiry = datetime(2099, 12, 31)
mock_creds.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

with patch("connector.Credentials", return_value=mock_creds), \
     patch("connector.GoogleAuthRequest", return_value=MagicMock()), \
     patch("asyncio.get_event_loop") as mock_loop:
    mock_loop.return_value.run_in_executor = AsyncMock(return_value=None)
    new_token = await connector_with_token.on_token_refresh()
```

---

## 7. Auth-Type Specific Rules

**Auth type:** `oauth2_code`

**What to mock:**
- `aiohttp.ClientSession.post` for the token exchange endpoint in `authorize()`
- `google.oauth2.credentials.Credentials` and its `refresh()` call in `on_token_refresh()`
- `connector.GmailHTTPClient` for all methods that call the Gmail API

**What NOT to test:**
- The actual OAuth2 redirect flow (browser-side)
- Google's OAuth2 servers
- The Shielva platform's `set_token()` Redis persistence (trust BaseConnector)
- `ensure_token()` internals (BaseConnector method — not our code)

---

## 8. Do NOT Test

- `BaseConnector` methods: `save_config`, `get_token`, `set_token`, `clear_token`, `ensure_token`, `ingest_batch`, `report_status`
- `googleapiclient.discovery.build` internals
- `google.auth.transport.requests.Request` internals
- Network-level behavior (timeouts, DNS)
- `helpers/utils.py` tenacity retry decoration (use side_effect with call counts instead)
- Python stdlib modules

---

## 9. Per-Method Test Specifications

### 9.1 `install(config)`

**Signature:** `async def install(self, config: Dict[str, Any] = None) -> ConnectorStatus`

**Happy path:**
- Input: `config = {"client_id": "cid", "client_secret": "csec"}`
- Mock: none needed (no HTTP calls)
- Assert: `result.health == ConnectorHealth.HEALTHY`, `result.auth_status == AuthStatus.PENDING`
- Assert: `result.message == "Authorization required — click Authorize to continue"`

**Error path — missing client_id:**
- Input: `config = {"client_secret": "csec"}`
- Assert: `result.health == ConnectorHealth.UNHEALTHY`, `result.auth_status == AuthStatus.MISSING_CREDENTIALS`
- Assert: `"client_id" in result.message`

**Error path — missing client_secret:**
- Input: `config = {"client_id": "cid"}`
- Assert: `result.health == ConnectorHealth.UNHEALTHY`, `result.auth_status == AuthStatus.MISSING_CREDENTIALS`
- Assert: `"client_secret" in result.message`

---

### 9.2 `authorize(auth_data)`

**Signature:** `async def authorize(self, auth_data: Dict[str, Any]) -> TokenInfo`

**Happy path:**
- Input: `auth_data = {"code": "auth_code_123"}`
- Mock: `aiohttp.ClientSession` POST returns `{"access_token": "ya29...", "refresh_token": "1//...", "expires_in": 3600, "scope": "https://www.googleapis.com/auth/gmail.readonly"}`
- Assert: returned `TokenInfo.access_token == "ya29..."`
- Assert: returned `TokenInfo.refresh_token == "1//..."`
- Assert: `len(returned.scopes) > 0`
- Assert: connector's `_token_info` is set (call `await connector.get_token()`)

**Error path — HTTP error from token endpoint:**
- Mock: `aiohttp.ClientSession.post` raises `aiohttp.ClientResponseError`
- Assert: exception propagates (connector does not swallow it)

---

### 9.3 `health_check()`

**Signature:** `async def health_check(self) -> ConnectorStatus`

**Happy path:**
- Pre-condition: connector has valid token
- Mock: `GmailHTTPClient` instance with `execute_get_profile` returning `{"emailAddress": "user@gmail.com"}`
- Assert: `result.health == ConnectorHealth.HEALTHY`
- Assert: `result.auth_status == AuthStatus.CONNECTED`
- Assert: `"user@gmail.com" in result.message`

**Error path — GmailAuthError (401):**
- Mock: `execute_get_profile` raises `GmailAuthError("HTTP 401: ...")`
- Also mock `ensure_token` to succeed so we reach the execute call
- Assert: `result.health == ConnectorHealth.UNHEALTHY`
- Assert: `result.auth_status == AuthStatus.TOKEN_EXPIRED`

**Error path — unexpected exception:**
- Mock: `execute_get_profile` raises `RuntimeError("network down")`
- Assert: `result.health == ConnectorHealth.UNHEALTHY`
- Assert: `result.auth_status == AuthStatus.FAILED`
- Assert: `"network down" in result.message`

---

### 9.4 `sync(since, full, kb_id, webhook_url)`

**Signature:** `async def sync(self, since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult`

**Happy path — full sync:**
- Mock: `list_email` returns 2 raw message dicts; `ingest_batch` AsyncMock returns `True`
- Input: `full=True`
- Assert: `result.status == SyncStatus.COMPLETED`
- Assert: `result.documents_found == 2`, `result.documents_synced == 2`

**Happy path — incremental sync with since:**
- Mock: `list_email` returns 1 message; capture the `query` argument
- Input: `since=datetime(2026, 6, 1)`, `full=False`
- Assert: `list_email` was called with a `query` containing `"after:"`
- Assert: `result.status == SyncStatus.COMPLETED`

**Happy path — zero results:**
- Mock: `list_email` returns `[]`
- Assert: `result.status == SyncStatus.COMPLETED`, `result.documents_synced == 0`

**Error path — GmailAuthError during list_email:**
- Mock: `list_email` raises `GmailAuthError("auth failed")`
- Assert: `result.status == SyncStatus.FAILED`
- Assert: `"auth failed" in result.message`

---

### 9.5 `list_email(label_ids, query)`

**Signature:** `async def list_email(self, label_ids=None, query=None) -> List[Dict]`

**Happy path — single page:**
- Mock: `GmailHTTPClient.execute_list_messages` returns `{"messages": [{"id": "m1"}], "nextPageToken": None}`
- Mock: `GmailHTTPClient.execute_get_message` returns full message dict
- Assert: returns list with 1 message dict containing `"id": "m1"`
- Assert: `execute_get_message` called with `msg_id="m1"`

**Happy path — multi-page pagination:**
- Mock: `execute_list_messages` side_effect:
  - first call returns `{"messages": [{"id": "m1"}], "nextPageToken": "tok2"}`
  - second call returns `{"messages": [{"id": "m2"}], "nextPageToken": None}`
- Assert: returns list with 2 message dicts

**Happy path — empty inbox:**
- Mock: `execute_list_messages` returns `{"messages": [], "nextPageToken": None}`
- Assert: returns `[]`

**Error path — single message fetch fails (others succeed):**
- Two stubs `[{id: m1}, {id: m2}]`; `execute_get_message` raises on m1, succeeds on m2
- Assert: returns list with 1 message (m1 skipped, not raised)

---

### 9.6 `on_token_refresh()`

**Signature:** `async def on_token_refresh(self) -> TokenInfo`

**Happy path:**
- Pre-condition: `connector._token_info` has a `refresh_token`
- Mock: `google.oauth2.credentials.Credentials`, its `.refresh()` call via `run_in_executor`
- Assert: returned `TokenInfo.access_token` matches mocked `creds.token`
- Assert: `connector._token_info` is updated (call `await connector.get_token()`)

**Error path — no refresh token:**
- Pre-condition: `connector._token_info = TokenInfo(access_token="x", refresh_token=None, ...)`
- Assert: raises `GmailAuthError` with message containing "No refresh token"

**Error path — no token at all:**
- Pre-condition: `connector._token_info = None`
- Assert: raises `GmailAuthError`

---

## 10. Test Dependencies

Add to `requirements.txt` under `# ── Test dependencies ──`:
```
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.1
pytest-cov>=4.1.0
```
(`pytest-timeout==2.4.0` is already present in requirements.txt.)
