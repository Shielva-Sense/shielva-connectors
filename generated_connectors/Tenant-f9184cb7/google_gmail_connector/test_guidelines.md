# Test Guidelines — Gmail Connector

## 1. Package Structure

```
google_gmail_c996c4_connector/
├── tests/
│   ├── __init__.py
│   ├── conftest.py          # fixtures, mocks, shared setup
│   └── test_connector.py   # unit tests for GmailConnector
```

---

## 2. Connector Class Details

**Class:** `GmailConnector` (in `connector.py`)

**Constructor:**
```python
GmailConnector(tenant_id: str, connector_id: str, config: Dict[str, Any] = None)
```

**Public methods (all async):**
```python
async def install(self) -> ConnectorStatus
async def authorize(self, auth_code: str, state: str = None) -> TokenInfo
async def health_check(self) -> ConnectorStatus
async def sync(self, since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult
async def list_emails(self, query="", max_results=500, page_token=None) -> Dict[str, Any]
async def get_email(self, message_id: str) -> NormalizedDocument
async def modify_message(self, message_id: str, add_labels=None, remove_labels=None) -> Dict[str, Any]
async def read_email(self, message_id: str) -> Dict[str, Any]
async def send_email(self, to: str, subject: str, body: str, cc=None, bcc=None) -> Dict[str, Any]
async def add_email(self, to: str, subject: str, body: str, cc=None, bcc=None) -> Dict[str, Any]
async def post_email(self, to: str, subject: str, body: str, cc=None, bcc=None) -> Dict[str, Any]
```

**Internal helpers (do not test directly):**
```python
async def _get_valid_token(self) -> str
async def on_token_refresh(self) -> TokenInfo
async def _collect_all_message_ids(self, access_token: str) -> tuple[List[str], Optional[str]]
async def _collect_history_ids(self, access_token: str, start_history_id: str) -> List[str]
```

---

## 3. Client/SDK Layer

**HTTP client:** `GmailHTTPClient` in `client/http_client.py`

**Key methods to mock:**
- `get_profile(access_token) -> Dict`
- `list_messages(access_token, query, max_results, page_token) -> Dict`
- `get_message(access_token, message_id, fmt) -> Dict`
- `execute_modify_message(access_token, message_id, add_label_ids, remove_label_ids) -> Dict`
- `execute_send_message(access_token, raw_message) -> Dict`
- `execute_create_draft(access_token, raw_message) -> Dict`
- `list_history(access_token, start_history_id, history_types, max_results, page_token) -> Dict`
- `post_form_data(url, payload, context) -> Dict`

**Mock path:** `connector.GmailHTTPClient` or patch `connector_instance.http_client`

**aiohttp async context manager mock pattern:**
```python
# session.post must be MagicMock (NOT AsyncMock) — AsyncMock returns a coroutine, not a CM
mock_post_cm = MagicMock()
mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
mock_post_cm.__aexit__ = AsyncMock(return_value=None)
mock_session_instance.post = MagicMock(return_value=mock_post_cm)
```

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

## 5. Fixture Blueprint

```python
# tests/conftest.py
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from connector import GmailConnector


TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"
TEST_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "scopes": "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send",
    "auth_url": "https://accounts.google.com/o/oauth2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "base_url": "https://gmail.googleapis.com/gmail/v1",
    "rate_limit_per_min": 250,
    "pagination_type": "page_token",
    "api_version": "v1",
}


@pytest.fixture
def connector():
    """Instantiate GmailConnector with full config."""
    return GmailConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=TEST_CONFIG,
    )


@pytest.fixture
def mock_http_client():
    """Return an AsyncMock replacing GmailHTTPClient on the connector."""
    return MagicMock(
        get_profile=AsyncMock(),
        list_messages=AsyncMock(),
        get_message=AsyncMock(),
        execute_modify_message=AsyncMock(),
        execute_send_message=AsyncMock(),
        execute_create_draft=AsyncMock(),
        list_history=AsyncMock(),
        post_form_data=AsyncMock(),
    )


@pytest.fixture
def connector_with_mock_client(connector, mock_http_client):
    """Connector with its http_client replaced by a mock."""
    connector.http_client = mock_http_client
    return connector


@pytest.fixture
def valid_token():
    """Inject a valid, non-expired token into the connector."""
    from datetime import datetime, timedelta, timezone
    from shared.base_connector import TokenInfo

    return TokenInfo(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        token_type="Bearer",
        scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ],
    )


@pytest.fixture
def authed_connector(connector_with_mock_client, valid_token):
    """Connector pre-loaded with a valid token."""
    connector_with_mock_client._token_info = valid_token
    return connector_with_mock_client


SAMPLE_MESSAGE = {
    "id": "msg123",
    "threadId": "thread456",
    "labelIds": ["INBOX"],
    "snippet": "Hello world",
    "historyId": "99000",
    "internalDate": "1700000000000",
    "payload": {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Subject", "value": "Test Subject"},
            {"name": "From", "value": "sender@example.com"},
            {"name": "To", "value": "recipient@example.com"},
            {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
        ],
        "body": {
            "data": "SGVsbG8gd29ybGQ=",  # "Hello world" base64
        },
        "parts": [],
    },
}
```

---

## 6. Mock Patterns

```python
# install()
connector._token_info = None  # no token needed

# authorize()
mock_http_client.post_form_data.return_value = {
    "access_token": "new-token",
    "refresh_token": "new-refresh",
    "expires_in": 3600,
    "token_type": "Bearer",
    "scope": "https://www.googleapis.com/auth/gmail.readonly",
}

# health_check()
mock_http_client.get_profile.return_value = {"emailAddress": "user@example.com"}

# sync()
mock_http_client.list_messages.return_value = {
    "messages": [{"id": "msg1"}, {"id": "msg2"}],
    "nextPageToken": None,
    "historyId": "12345",
}
mock_http_client.get_message.return_value = SAMPLE_MESSAGE  # from conftest

# list_emails()
mock_http_client.list_messages.return_value = {"messages": [{"id": "msg1"}]}

# get_email() / read_email()
mock_http_client.get_message.return_value = SAMPLE_MESSAGE

# modify_message()
mock_http_client.execute_modify_message.return_value = {"id": "msg1", "labelIds": ["INBOX", "STARRED"]}

# send_email() / post_email()
mock_http_client.execute_send_message.return_value = {"id": "sent123", "threadId": "thread1", "labelIds": ["SENT"]}

# add_email()
mock_http_client.execute_create_draft.return_value = {"id": "draft1", "message": {"id": "msg456"}}

# send_email() — 403 PermissionError
mock_http_client.execute_send_message.side_effect = PermissionError(
    "gmail.send scope missing — re-authorize the connector"
)

# send_email() — 400 ValueError
mock_http_client.execute_send_message.side_effect = ValueError("Invalid recipient address")
```

---

## 7. Auth-Type Specific Rules (oauth2_code)

**DO mock:**
- `connector._token_info` — inject a valid `TokenInfo` with `access_token` set
- `connector.http_client.post_form_data` — for authorize() and on_token_refresh() tests

**DO NOT mock:**
- `BaseConnector.ensure_token()` — use `connector._token_info` to bypass it naturally
- `BaseConnector.set_token()` — tested indirectly via authorize()
- `BaseConnector.get_oauth_url()` — not part of connector-specific logic

**Token injection pattern:**
```python
from datetime import datetime, timedelta, timezone
from shared.base_connector import TokenInfo, AuthStatus

connector._token_info = TokenInfo(
    access_token="fake-token",
    refresh_token="fake-refresh",
    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    scopes=["https://www.googleapis.com/auth/gmail.send"],
)
connector._status.auth_status = AuthStatus.CONNECTED
```

---

## 8. Do NOT Test

- `BaseConnector` internals: `ensure_token()`, `set_token()`, `get_token()`, `ingest_batch()`, `publish_event()`
- `aiohttp` internals: session creation, SSL, connection pooling
- `GmailHTTPClient._raise_for_status()` internal error mapping (tested indirectly)
- Private helpers: `_collect_all_message_ids()`, `_collect_history_ids()` — tested indirectly via `sync()`
- Redis persistence: `connector_store` — not available in unit tests
- Actual Google OAuth endpoints — all network calls must be mocked

---

## 9. Per-Method Test Specifications

### `install()`
**Signature:** `async def install(self) -> ConnectorStatus`

**Happy path — credentials present:**
```python
result = await connector.install()
assert result.health == ConnectorHealth.HEALTHY
assert result.auth_status == AuthStatus.PENDING
```

**Error path — missing client_id:**
```python
connector.config.pop("client_id", None)
result = await connector.install()
assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
assert result.health == ConnectorHealth.OFFLINE
```

**Error path — missing client_secret:**
```python
connector.config.pop("client_secret", None)
result = await connector.install()
assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
```

---

### `authorize(auth_code, state)`
**Signature:** `async def authorize(self, auth_code: str, state: str = None) -> TokenInfo`

**Happy path:**
```python
mock_http_client.post_form_data.return_value = {
    "access_token": "acc123", "refresh_token": "ref456",
    "expires_in": 3600, "token_type": "Bearer",
    "scope": "https://www.googleapis.com/auth/gmail.readonly"
}
result = await authed_connector.authorize("code123")
assert result.access_token == "acc123"
assert result.refresh_token == "ref456"
assert isinstance(result.scopes, list)
```

**Error path — http_client raises GmailAuthError:**
```python
from exceptions import GmailAuthError
mock_http_client.post_form_data.side_effect = GmailAuthError("invalid_client")
with pytest.raises(GmailAuthError):
    await authed_connector.authorize("bad-code")
```

---

### `health_check()`
**Signature:** `async def health_check(self) -> ConnectorStatus`

**Happy path:**
```python
mock_http_client.get_profile.return_value = {"emailAddress": "u@example.com"}
result = await authed_connector.health_check()
assert result.health == ConnectorHealth.HEALTHY
assert result.auth_status == AuthStatus.CONNECTED
```

**Error path — GmailAuthError (token expired):**
```python
from exceptions import GmailAuthError
mock_http_client.get_profile.side_effect = GmailAuthError("401")
result = await authed_connector.health_check()
assert result.auth_status == AuthStatus.TOKEN_EXPIRED
assert result.health == ConnectorHealth.DEGRADED
```

---

### `sync()`
**Signature:** `async def sync(self, since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult`

**Happy path — full sync:**
```python
mock_http_client.list_messages.return_value = {
    "messages": [{"id": "msg1"}, {"id": "msg2"}],
    "nextPageToken": None,
    "historyId": "5000",
}
mock_http_client.get_message.return_value = SAMPLE_MESSAGE
with patch.object(authed_connector, "ingest_document", new=AsyncMock()):
    result = await authed_connector.sync(full=True)
assert result.status == SyncStatus.COMPLETED
assert result.documents_synced == 2
assert result.documents_found == 2
```

**Error path — get_message raises:**
```python
mock_http_client.list_messages.return_value = {"messages": [{"id": "m1"}], "nextPageToken": None}
mock_http_client.get_message.side_effect = Exception("network error")
with patch.object(authed_connector, "ingest_document", new=AsyncMock()):
    result = await authed_connector.sync(full=True)
assert result.status == SyncStatus.PARTIAL
assert result.documents_failed == 1
```

---

### `list_emails()`
**Signature:** `async def list_emails(self, query="", max_results=500, page_token=None) -> Dict`

**Happy path:**
```python
mock_http_client.list_messages.return_value = {
    "messages": [{"id": "m1", "threadId": "t1"}],
    "nextPageToken": None,
}
result = await authed_connector.list_emails(query="in:inbox")
assert "messages" in result
assert result["messages"][0]["id"] == "m1"
mock_http_client.list_messages.assert_awaited_once()
```

---

### `get_email()`
**Signature:** `async def get_email(self, message_id: str) -> NormalizedDocument`

**Happy path:**
```python
mock_http_client.get_message.return_value = SAMPLE_MESSAGE
result = await authed_connector.get_email("msg123")
assert result.source_id == "msg123"
assert result.title == "Test Subject"
assert "Hello world" in result.content
```

---

### `modify_message()`
**Signature:** `async def modify_message(self, message_id, add_labels=None, remove_labels=None) -> Dict`

**Happy path:**
```python
mock_http_client.execute_modify_message.return_value = {"id": "msg1", "labelIds": ["STARRED"]}
result = await authed_connector.modify_message("msg1", add_labels=["STARRED"])
assert result["id"] == "msg1"
mock_http_client.execute_modify_message.assert_awaited_once_with(
    "test-access-token", message_id="msg1", add_label_ids=["STARRED"], remove_label_ids=None
)
```

---

### `read_email()`
**Signature:** `async def read_email(self, message_id: str) -> Dict`

**Happy path:**
```python
mock_http_client.get_message.return_value = SAMPLE_MESSAGE
result = await authed_connector.read_email("msg123")
assert result["id"] == "msg123"
assert result["payload"]["headers"] is not None
```

**Note:** `read_email()` returns the raw API dict; `get_email()` returns a NormalizedDocument.

---

### `send_email()`
**Signature:** `async def send_email(self, to, subject, body, cc=None, bcc=None) -> Dict`

**Happy path:**
```python
mock_http_client.execute_send_message.return_value = {
    "id": "sent1", "threadId": "thread1", "labelIds": ["SENT"]
}
result = await authed_connector.send_email(
    to="recipient@example.com", subject="Hello", body="World"
)
assert result["id"] == "sent1"
assert result["labelIds"] == ["SENT"]
mock_http_client.execute_send_message.assert_awaited_once()
# Verify raw message is base64url-encoded (no padding)
call_args = mock_http_client.execute_send_message.call_args
raw = call_args[0][1]  # second positional arg
assert "=" not in raw
```

**Error path — 403 (missing scope):**
```python
mock_http_client.execute_send_message.side_effect = PermissionError(
    "gmail.send scope missing — re-authorize the connector"
)
with pytest.raises(PermissionError, match="gmail.send scope missing"):
    await authed_connector.send_email("to@ex.com", "sub", "body")
```

**Error path — 400 (bad request):**
```python
mock_http_client.execute_send_message.side_effect = ValueError("Invalid recipient")
with pytest.raises(ValueError, match="Invalid recipient"):
    await authed_connector.send_email("bad@", "sub", "body")
```

**Test with cc/bcc:**
```python
mock_http_client.execute_send_message.return_value = {"id": "s2"}
result = await authed_connector.send_email(
    "to@ex.com", "sub", "body", cc="cc@ex.com", bcc="bcc@ex.com"
)
assert result["id"] == "s2"
```

---

### `add_email()`
**Signature:** `async def add_email(self, to, subject, body, cc=None, bcc=None) -> Dict`

**Happy path:**
```python
mock_http_client.execute_create_draft.return_value = {
    "id": "draft1", "message": {"id": "msg456", "threadId": "t1"}
}
result = await authed_connector.add_email("to@ex.com", "Draft", "body text")
assert result["id"] == "draft1"
assert result["message"]["id"] == "msg456"
```

---

### `post_email()`
**Signature:** `async def post_email(self, to, subject, body, cc=None, bcc=None) -> Dict`

**Happy path (alias test):**
```python
mock_http_client.execute_send_message.return_value = {"id": "sent2"}
result = await authed_connector.post_email("to@ex.com", "Hi", "body")
# Must call execute_send_message — same as send_email
mock_http_client.execute_send_message.assert_awaited_once()
assert result["id"] == "sent2"
```

---

## 10. Test Dependencies

These packages must be in `requirements.txt` under `# ── Test dependencies ──`:

```
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.1
pytest-cov>=4.1.0
```
