# Gmail Connector — Test Guidelines

---

## 1. Package Structure

```
tests/
├── __init__.py
├── conftest.py          # shared fixtures
└── test_connector.py   # all unit tests
```

---

## 2. Connector Class Details

**Class name**: `GmailConnector`  
**Module**: `connector`  
**Constructor**: `GmailConnector(tenant_id: str, connector_id: str, config: Optional[Dict[str, Any]] = None)`

**Public methods**:

| Method | Signature |
|---|---|
| `install` | `async def install(self) -> ConnectorStatus` |
| `authorize` | `async def authorize(self, auth_code: str, state: Optional[str] = None) -> TokenInfo` |
| `on_token_refresh` | `async def on_token_refresh(self) -> TokenInfo` |
| `health_check` | `async def health_check(self) -> ConnectorStatus` |
| `sync` | `async def sync(self, since=None, full=False, kb_id="", webhook_url=None) -> SyncResult` |
| `list_email` | `async def list_email(self, query="", max_results=100, page_token=None) -> Dict` |
| `read_email` | `async def read_email(self, msg_id: str) -> NormalizedDocument` |
| `add_email` | `async def add_email(self, msg_id: str, label_ids=None) -> Dict` |
| `delete_email` | `async def delete_email(self, msg_id: str, permanent=False) -> Any` |
| `remove_email` | `async def remove_email(self, msg_id: str) -> Any` |
| `delete_message` | `async def delete_message(self, msg_id: str, permanent=False) -> Any` |
| `delete_thread` | `async def delete_thread(self, thread_id: str, permanent=False) -> Any` |
| `bulk_delete` | `async def bulk_delete(self, query: str, permanent=False) -> BulkDeleteResult` |
| `disconnect` | `async def disconnect(self) -> None` |

---

## 3. Client/SDK Layer

The HTTP layer is encapsulated in `client.http_client.GmailHTTPClient`. `GmailConnector` creates instances via `_build_http_client()`. All tests must patch `GmailHTTPClient` at the import path used in `connector.py`:

```
connector.GmailHTTPClient
```

or equivalently patch individual methods on an instance returned by a mock factory.

**Patch path**: `connector.GmailHTTPClient`

---

## 4. Required Packages

```
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.1
pytest-cov>=4.1.0
pytest-timeout==2.4.0
aiohttp>=3.9.0
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
from shared.base_connector import TokenInfo, ConnectorHealth, AuthStatus


TENANT_ID = "test-tenant"
CONNECTOR_ID = "test-connector"
BASE_CONFIG = {
    "allow_permanent_delete": False,
    "redirect_uri": "https://example.com/callback",
}


@pytest.fixture
def connector():
    return GmailConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=BASE_CONFIG.copy(),
    )


@pytest.fixture
def connector_with_perm_delete():
    cfg = {**BASE_CONFIG, "allow_permanent_delete": True}
    return GmailConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )


@pytest.fixture
def valid_token():
    from datetime import datetime, timedelta
    return TokenInfo(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )


@pytest.fixture
def authed_connector(connector, valid_token):
    """Connector with a pre-set valid token."""
    connector._token_info = valid_token
    return connector


@pytest.fixture
def mock_http_client():
    """Pre-configured mock of GmailHTTPClient with sensible defaults."""
    client = MagicMock()
    client.execute_get_profile = AsyncMock(return_value={"emailAddress": "user@example.com"})
    client.execute_list_messages = AsyncMock(return_value={"messages": [], "nextPageToken": None})
    client.execute_get_message = AsyncMock(return_value={
        "id": "msg1",
        "threadId": "thread1",
        "labelIds": ["INBOX"],
        "snippet": "Hello world",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Test Subject"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "To", "value": "recv@example.com"},
                {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": "SGVsbG8gd29ybGQ="},  # "Hello world" base64
        },
    })
    client.execute_trash_message = AsyncMock(return_value={"id": "msg1", "labelIds": ["TRASH"]})
    client.execute_delete_message = AsyncMock(return_value=None)
    client.execute_modify_message = AsyncMock(return_value={"id": "msg1", "labelIds": ["INBOX", "STARRED"]})
    client.execute_trash_thread = AsyncMock(return_value={"id": "thread1", "messages": []})
    client.execute_delete_thread = AsyncMock(return_value=None)
    return client


# aiohttp async context manager mock pattern (for authorize / on_token_refresh):
# session.post must be MagicMock (NOT AsyncMock) — AsyncMock returns a coroutine, not a CM
def make_aiohttp_post_mock(response_data: dict, status: int = 200):
    mock_response = AsyncMock()
    mock_response.status = status
    mock_response.json = AsyncMock(return_value=response_data)
    mock_response.text = AsyncMock(return_value=str(response_data))

    mock_post_cm = MagicMock()
    mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_post_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_post_cm)

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)
    return mock_session_cm
```

---

## 6. Mock Patterns

| Method | Patch path | Mock setup |
|---|---|---|
| `install` | n/a (no external calls) | call directly |
| `authorize` | `connector.aiohttp.ClientSession` | use `make_aiohttp_post_mock(token_data)` |
| `on_token_refresh` | `connector.aiohttp.ClientSession` | use `make_aiohttp_post_mock(token_data)` |
| `health_check` | `connector.GmailHTTPClient` | `mock_http_client.execute_get_profile` |
| `sync` | `connector.GmailHTTPClient` | `mock_http_client.execute_list_messages`, `execute_get_message` |
| `list_email` | `connector.GmailHTTPClient` | `mock_http_client.execute_list_messages` |
| `read_email` | `connector.GmailHTTPClient` | `mock_http_client.execute_get_message` |
| `add_email` | `connector.GmailHTTPClient` | `mock_http_client.execute_modify_message` |
| `delete_email` | `connector.GmailHTTPClient` | `mock_http_client.execute_trash_message` |
| `remove_email` | `connector.GmailHTTPClient` | `mock_http_client.execute_trash_message` |
| `delete_message` | `connector.GmailHTTPClient` | `execute_trash_message` or `execute_delete_message` |
| `delete_thread` | `connector.GmailHTTPClient` | `execute_trash_thread` or `execute_delete_thread` |
| `bulk_delete` | `connector.GmailHTTPClient` | `execute_list_messages` + `execute_trash_message` |
| `disconnect` | n/a (calls `clear_token()` on self) | assert `_token_info is None` after call |

---

## 7. Auth-Type Specific Rules

Auth type: `oauth2_code`

**What to mock:**
- `aiohttp.ClientSession` for `authorize()` and `on_token_refresh()` calls.
- `connector._token_info` — set directly in fixtures to skip the OAuth flow in most tests.
- `connector.ensure_token()` — patch with `AsyncMock(return_value=valid_token)` when testing methods that call `_build_http_client`.

**What NOT to test:**
- The actual Google OAuth 2.0 server response — this is integration testing territory.
- Token persistence to Redis (`set_token` internals) — BaseConnector responsibility.
- `get_oauth_url()` — already tested by BaseConnector; no need to retest.

---

## 8. Do NOT Test

- `BaseConnector` methods: `ensure_token`, `set_token`, `get_token`, `clear_token`, `save_config`, `ingest_batch`, `ingest_document`, `publish_event`, `report_status`.
- `aiohttp` internals (connection pooling, SSL, etc.).
- `GmailHTTPClient` in connector unit tests — mock at the boundary.
- `helpers/normalizer.py` directly in connector tests — test it in isolation if needed.
- Live Gmail API calls — those belong in integration tests.

---

## 9. Per-Method Test Specifications

### `install()`
- **Happy path**: call `connector.install()` → `ConnectorStatus.health == HEALTHY`, `auth_status == PENDING`.
- **Error path**: n/a (no external calls; cannot fail).

### `authorize(auth_code, state)`
- **Happy path**: mock `aiohttp.ClientSession.post` to return `{access_token, refresh_token, expires_in, scope}`; assert returned `TokenInfo.access_token == "test-access-token"` and `token_info.refresh_token` is set.
- **Error path**: mock response `status=400`; assert `ConnectorAuthError` is raised.

### `on_token_refresh()`
- **Happy path**: set `connector._token_info` with a valid refresh_token; mock POST to return new `{access_token, expires_in}`; assert returned `TokenInfo.access_token` matches.
- **Error path**: set `connector._token_info = None`; assert `ConnectorAuthError` raised.

### `health_check()`
- **Happy path**: mock `_build_http_client` → `mock_http_client` with `execute_get_profile` returning `{emailAddress: "u@e.com"}`; assert `health == HEALTHY`, `auth_status == CONNECTED`.
- **Error path**: `execute_get_profile` raises `ConnectorAuthError`; assert `health == DEGRADED`, `auth_status == TOKEN_EXPIRED`.

### `sync(since, full, kb_id, webhook_url)`
- **Happy path**: mock `execute_list_messages` returning 1 page with 2 message stubs, `execute_get_message` returning full message; assert `SyncResult.documents_synced == 2`.
- **Deletion propagation**: set `known_message_ids = ["old-id"]`, mock list returning only `["new-id"]`; assert `_remove_from_kb` called with `"old-id"` and `save_config` called with updated `known_message_ids`.
- **Error path**: `execute_list_messages` raises `ConnectorError`; assert `SyncResult.status == FAILED`.

### `list_email(query, max_results, page_token)`
- **Happy path**: mock `execute_list_messages` returning `{messages: [{id: "m1", threadId: "t1"}], nextPageToken: None}`; assert returned dict matches.
- **Error path**: `execute_list_messages` raises `ConnectorRateLimitError`; assert it propagates.

### `read_email(msg_id)`
- **Happy path**: mock `execute_get_message` returning full message dict; assert returned `NormalizedDocument.id == "msg1"`, `title == "Test Subject"`, `content` non-empty.
- **Error path**: `execute_get_message` raises `ConnectorNotFoundError`; assert it propagates.

### `add_email(msg_id, label_ids)`
- **Happy path**: mock `execute_modify_message` returning `{id: "msg1", labelIds: ["INBOX","STARRED"]}`; assert result contains expected label IDs.
- **Error path**: `execute_modify_message` raises `ConnectorPermissionError`; assert it propagates.

### `delete_email(msg_id, permanent=False)`
- **Happy path**: assert this delegates to `delete_message(msg_id, permanent=False)`.
- Already covered by `delete_message` tests.

### `remove_email(msg_id)`
- **Happy path**: assert this delegates to `delete_message(msg_id, permanent=False)`.

### `delete_message(msg_id, permanent=False)`
- **Happy path (soft)**: mock `execute_trash_message` → `{id: "msg1", labelIds: ["TRASH"]}`; `permanent=False`; assert trash method called, result returned.
- **Happy path (hard)**: use `connector_with_perm_delete`; mock `execute_delete_message`; `permanent=True`; assert delete method called, result is `None`.
- **Error path — permanent blocked**: use default `connector` (perm_delete=False); call `delete_message(msg_id, permanent=True)`; assert `ConnectorPermissionError` raised before any HTTP call.
- **Error path — 404**: `execute_trash_message` raises `ConnectorNotFoundError`; assert it propagates.

### `delete_thread(thread_id, permanent=False)`
- **Happy path (soft)**: mock `execute_trash_thread`; assert result returned.
- **Happy path (hard)**: use `connector_with_perm_delete`; mock `execute_delete_thread`; assert `None` returned.
- **Error path — permanent blocked**: default connector with `permanent=True`; assert `ConnectorPermissionError`.

### `bulk_delete(query, permanent=False)`
- **Happy path**: mock `execute_list_messages` returning 2 stubs (1 page); mock `execute_trash_message` succeeding; assert `BulkDeleteResult.deleted == 2`, `failed == 0`.
- **Partial failure**: second `execute_trash_message` call raises `ConnectorError`; assert `deleted == 1`, `failed == 1`, `errors` has 1 entry.
- **Multi-page**: mock two pages (first returns `nextPageToken`, second returns `None`); assert all IDs from both pages are processed.
- **Hard delete blocked**: default connector, `permanent=True`; assert `ConnectorPermissionError`.

### `disconnect()`
- **Happy path**: set `connector._token_info = valid_token`; call `disconnect()`; assert `connector._token_info is None`.

---

## 10. Test Dependencies

Add to `requirements.txt` under `# ── Test dependencies ──`:

```
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.1
pytest-cov>=4.1.0
pytest-timeout==2.4.0
```
