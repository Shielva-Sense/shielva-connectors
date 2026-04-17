# Gmail Connector — Test Guidelines

## 1. Package Structure

```
tests/
├── __init__.py
├── conftest.py            # fixtures, path setup, mock factories
├── test_connector.py      # unit tests for all connector methods
└── test_integration.py    # real API tests (skipped unless INTEGRATION_TEST=true)
```

---

## 2. Connector Class Details

**Class name:** `GmailConnector`
**Module:** `connector`
**Constructor:**
```python
GmailConnector(
    tenant_id: str,
    connector_id: str,
    config: Dict[str, Any] = None
)
```

**Public methods (all async):**

| Method | Signature |
|---|---|
| `install` | `async def install(self, config: Dict[str, Any] = None) -> ConnectorStatus` |
| `authorize` | `async def authorize(self, auth_data: Dict[str, Any]) -> TokenInfo` |
| `health_check` | `async def health_check(self) -> ConnectorStatus` |
| `sync` | `async def sync(self, since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult` |
| `list_emails` | `async def list_emails(self, page_token=None, max_results=20, query=None) -> List[NormalizedDocument]` |
| `list_email` | `async def list_email(self, message_id: str) -> NormalizedDocument` |
| `search_email` | `async def search_email(self, query: str, page_token=None, max_results=20) -> List[NormalizedDocument]` |
| `send_email` | `async def send_email(self, to, subject, body, cc=None, bcc=None, reply_to=None, attachments=None) -> Dict` |
| `delete_email` | `async def delete_email(self, message_id: str, permanent=False) -> None` |

**Private helpers (tested indirectly):**
- `_get_client()` — builds GmailHttpClient with valid credentials
- `_refresh_token(token)` — exchanges refresh_token for new access token
- `_resolve_scopes()` — resolves scopes from config or class constant
- `_resolve_token_url()` — resolves token URL from config or constant

---

## 3. Client/SDK Layer

**HTTP client class:** `GmailHttpClient` in `client/http_client.py`

The client wraps `google-api-python-client` (`googleapiclient.discovery.build`).
In unit tests, mock `GmailHttpClient` at the `connector._get_client` level so connector.py
never instantiates the real client.

**Mock approach:** Patch `connector.GmailConnector._get_client` to return an `AsyncMock`
whose attributes (`list_messages`, `get_message`, `send_message`, `trash_message`,
`delete_message_permanent`, `get_profile`) are `AsyncMock` instances.

---

## 4. Required Packages

```
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.1
pytest-cov>=4.1.0
pytest-timeout==2.4.0
```

All tests use `@pytest.mark.asyncio` and `asyncio_mode = "auto"` (set in `pytest.ini`).

---

## 5. Fixture Blueprint

### conftest.py (complete, copy-pasteable)

```python
"""
Pytest fixtures for GmailConnector unit tests.
Path setup uses relative resolution — no machine-specific absolute paths.
"""
import sys
import os

# Ensure the connector root is on sys.path so imports work without installation
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


TENANT_ID = "test-tenant"
CONNECTOR_ID = "test-connector-gmail"

BASE_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "base_url": "https://gmail.googleapis.com/gmail/v1",
    "rate_limit_per_min": 100,
    "pagination_type": "cursor",
    "api_version": "v1",
    "redirect_uri": "https://example.com/callback",
}


@pytest.fixture
def connector():
    """Return a GmailConnector instance with all config keys populated."""
    from connector import GmailConnector
    return GmailConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=BASE_CONFIG.copy(),
    )


@pytest.fixture
def mock_token():
    """Return a valid TokenInfo for tests that require an authorized connector."""
    from datetime import datetime, timedelta, timezone
    from shared.base_connector import TokenInfo
    return TokenInfo(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        token_type="Bearer",
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )


@pytest.fixture
def mock_http_client():
    """
    Return a fully mocked GmailHttpClient with all async methods as AsyncMock.
    Patch connector._get_client to return this mock.
    """
    client = MagicMock()
    client.list_messages = AsyncMock()
    client.get_message = AsyncMock()
    client.send_message = AsyncMock()
    client.trash_message = AsyncMock()
    client.delete_message_permanent = AsyncMock()
    client.get_profile = AsyncMock()
    return client


@pytest.fixture
def raw_message():
    """A minimal raw Gmail message resource for normalization tests."""
    return {
        "id": "msg001",
        "threadId": "thread001",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Hello world snippet",
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Test Subject"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "To", "value": "recipient@example.com"},
                {"name": "Date", "value": "Mon, 14 Nov 2023 10:00:00 +0000"},
            ],
            "body": {
                "data": "SGVsbG8gd29ybGQ="  # base64("Hello world")
            },
        },
    }
```

---

## 6. Mock Patterns

### Pattern: mock _get_client (used by all data methods)

```python
@pytest.mark.asyncio
async def test_example(connector, mock_token, mock_http_client):
    connector._token_info = mock_token
    with patch.object(connector, "_get_client", return_value=mock_http_client):
        # now call connector methods
        ...
```

### Pattern: mock get_token (used by health_check, sync)

```python
connector.get_token = AsyncMock(return_value=mock_token)
connector.set_token = AsyncMock()
```

### Pattern: mock aiohttp for authorize/refresh

```python
from unittest.mock import AsyncMock, MagicMock, patch

mock_response = MagicMock()
mock_response.status = 200
mock_response.json = AsyncMock(return_value={
    "access_token": "new-token",
    "refresh_token": "new-refresh",
    "expires_in": 3600,
    "token_type": "Bearer",
    "scope": "https://www.googleapis.com/auth/gmail.modify",
})

mock_cm = MagicMock()
mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
mock_cm.__aexit__ = AsyncMock(return_value=None)

mock_session_instance = MagicMock()
mock_session_instance.post = MagicMock(return_value=mock_cm)

mock_session_cm = MagicMock()
mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session_instance)
mock_session_cm.__aexit__ = AsyncMock(return_value=None)

with patch("connector.aiohttp.ClientSession", return_value=mock_session_cm):
    ...
```

---

## 7. Auth-Type Specific Rules

**Auth type:** `oauth2_code`

**Mock:** Always provide `client_id`, `client_secret`, `redirect_uri` in config.
**Token exchange:** Mock `aiohttp.ClientSession` in connector.py (patch `connector.aiohttp.ClientSession`).
**Token storage:** Mock `connector.set_token` and `connector.get_token` as `AsyncMock`.
**Do NOT test:** Actual Google OAuth endpoints, real token exchange, actual redirect flows.

---

## 8. Do NOT Test

- `BaseConnector` internals (`save_config`, `ingest_batch`, `publish_event`, `report_status`)
- `google-api-python-client` internals (`build()`, `.execute()`, `HttpError` construction)
- `google-auth` credential refresh internals
- Network calls (all HTTP must be mocked)
- `conftest.py` fixtures themselves
- Private methods directly (test via public method behaviour)

---

## 9. Per-Method Test Specifications

### 9.1 `install(config)`

**Signature:** `async def install(self, config: Dict[str, Any] = None) -> ConnectorStatus`

**Happy path:**
- Input: valid config dict with `client_id`, `client_secret`
- Mock: `connector.save_config = AsyncMock()`
- Assert: returns `ConnectorStatus` with `health == ConnectorHealth.OFFLINE`, `auth_status == AuthStatus.PENDING`
- Assert: `message` contains "Authorize"

**Error path:**
- Input: `config=None`
- Assert: still returns `ConnectorStatus` with `auth_status == AuthStatus.PENDING` (no error raised)

---

### 9.2 `authorize(auth_data)`

**Signature:** `async def authorize(self, auth_data: Dict[str, Any]) -> TokenInfo`

**Happy path:**
- Input: `{"code": "auth-code-123", "redirect_uri": "https://example.com/callback"}`
- Mock: `aiohttp.ClientSession.post` → 200 with token JSON
- Mock: `connector.set_token = AsyncMock()`
- Assert: returns `TokenInfo` with `access_token == "new-token"`
- Assert: `set_token` was called once

**Error path — missing code:**
- Input: `{}`
- Assert: raises `GmailAuthError`

**Error path — token endpoint 400:**
- Mock: `aiohttp.ClientSession.post` → status 400
- Assert: raises `GmailAuthError`

**Error path — missing client_id:**
- Config has no `client_id`
- Assert: raises `GmailAuthError`

---

### 9.3 `health_check()`

**Signature:** `async def health_check(self) -> ConnectorStatus`

**Happy path:**
- Mock: `get_token` returns valid `TokenInfo`
- Mock: `_get_client` returns `mock_http_client`; `mock_http_client.get_profile` returns `{"emailAddress": "user@example.com"}`
- Assert: returns `ConnectorStatus` with `health == ConnectorHealth.HEALTHY`, `auth_status == AuthStatus.CONNECTED`

**Error path — no token:**
- Mock: `get_token` returns `None`
- Assert: returns `ConnectorStatus` with `health == ConnectorHealth.OFFLINE`, `auth_status == AuthStatus.MISSING_CREDENTIALS`

**Error path — auth error:**
- Mock: `_get_client` raises `GmailAuthError("token expired")`
- Assert: returns `ConnectorStatus` with `health == ConnectorHealth.DEGRADED`, `auth_status == AuthStatus.EXPIRED`

---

### 9.4 `sync(since, full, kb_id, webhook_url)`

**Signature:** `async def sync(self, since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult`

**Happy path — incremental:**
- Mock: `_get_client` → `mock_http_client`
- `mock_http_client.list_messages` → `{"messages": [{"id": "msg001", "threadId": "t1"}], "nextPageToken": None}`
- `mock_http_client.get_message` → raw_message fixture
- Mock: `ingest_batch = AsyncMock()`
- Assert: returns `SyncResult` with `status == SyncStatus.COMPLETED`, `documents_synced == 1`

**Happy path — pagination:**
- First `list_messages` call returns `nextPageToken="page2"`
- Second call returns no `nextPageToken`
- Assert: `list_messages` called twice; `documents_synced == 2`

**Error path — auth error:**
- `_get_client` raises `GmailAuthError`
- Assert: returns `SyncResult` with `status == SyncStatus.FAILED`

**Error path — individual message fetch fails:**
- `get_message` raises `GmailMessageNotFoundError`
- Assert: `documents_failed == 1`; overall `status == SyncStatus.COMPLETED`

---

### 9.5 `list_emails(page_token, max_results, query)`

**Signature:** `async def list_emails(self, page_token=None, max_results=20, query=None) -> List[NormalizedDocument]`

**Happy path:**
- `list_messages` → `{"messages": [{"id": "msg001"}], "nextPageToken": "tok2"}`
- `get_message` → `raw_message` fixture
- Assert: returns list of 1 `NormalizedDocument`; `doc.source_id == "msg001"`
- Assert: `doc.metadata["next_page_token"] == "tok2"`

**Empty result:**
- `list_messages` → `{"messages": []}`
- Assert: returns `[]`

---

### 9.6 `list_email(message_id)`

**Signature:** `async def list_email(self, message_id: str) -> NormalizedDocument`

**Happy path:**
- `get_message("msg001")` → `raw_message` fixture
- Assert: returns `NormalizedDocument` with `source_id == "msg001"`, `title == "Test Subject"`

**Error path — not found:**
- `get_message` raises `GmailMessageNotFoundError`
- Assert: `GmailMessageNotFoundError` propagates

---

### 9.7 `search_email(query, page_token, max_results)`

**Signature:** `async def search_email(self, query: str, page_token=None, max_results=20) -> List[NormalizedDocument]`

**Happy path:**
- `list_messages(query="from:test@example.com")` → 1 message stub
- `get_message` → `raw_message` fixture
- Assert: returns 1 `NormalizedDocument`

**Empty result:**
- `list_messages` → `{"messages": []}`
- Assert: returns `[]`

---

### 9.8 `send_email(to, subject, body, ...)`

**Signature:** `async def send_email(self, to, subject, body, cc=None, bcc=None, reply_to=None, attachments=None) -> Dict`

**Happy path:**
- Input: `to="user@example.com"`, `subject="Hi"`, `body="Hello"`
- `send_message` → `{"id": "sent001", "threadId": "t1", "labelIds": ["SENT"]}`
- Assert: returns dict with `id == "sent001"`

**Error path — invalid email:**
- Input: `to="not-an-email"`
- Assert: raises `GmailValidationError` (before any API call)

**Error path — attachment too large:**
- `attachments=[{"filename": "big.bin", "data": b"x" * (26 * 1024 * 1024), "mimetype": "application/octet-stream"}]`
- Assert: raises `GmailAttachmentError` (before any API call)

**Error path — API 400:**
- `send_message` raises `GmailAPIError`
- Assert: `GmailAPIError` propagates

---

### 9.9 `delete_email(message_id, permanent)`

**Signature:** `async def delete_email(self, message_id: str, permanent=False) -> None`

**Happy path — trash:**
- `permanent=False`
- Assert: `trash_message("msg001")` called once; `delete_message_permanent` NOT called

**Happy path — permanent:**
- `permanent=True`
- Assert: `delete_message_permanent("msg001")` called once; `trash_message` NOT called

**Error path — not found (trash):**
- `trash_message` raises `GmailMessageNotFoundError`
- Assert: `GmailMessageNotFoundError` propagates

**Error path — not found (permanent):**
- `delete_message_permanent` raises `GmailMessageNotFoundError`
- Assert: `GmailMessageNotFoundError` propagates

---

## 10. Test Dependencies

Add to `requirements.txt` under `# ── Test dependencies ──`:

```
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.1
pytest-cov>=4.1.0
```

(`pytest-timeout==2.4.0` is already in requirements.txt under connector deps.)
