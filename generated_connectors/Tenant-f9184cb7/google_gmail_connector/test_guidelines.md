# Gmail Connector — Test Guidelines

## 1. Package Structure

```
tests/
├── __init__.py
├── conftest.py          # shared fixtures, BASE_CONFIG, mock factories
└── test_connector.py    # all unit tests (fully mocked, zero real I/O)
```

---

## 2. Connector Class Details

**Class**: `GmailConnector` (in `connector.py`)

**Constructor**:
```python
GmailConnector(
    tenant_id: str,
    connector_id: str,
    config: Optional[Dict[str, Any]] = None,
)
```

**Public methods**:
```python
async def install() -> ConnectorStatus
async def authorize(auth_code: str, state: Optional[str] = None) -> TokenInfo
async def on_token_refresh() -> TokenInfo
async def health_check() -> ConnectorStatus
async def sync(since=None, full=False, kb_id="", webhook_url=None) -> SyncResult
async def list_email(query="", max_results=100, page_token=None) -> Dict[str, Any]
async def read_email(msg_id: str) -> NormalizedDocument
async def add_email(msg_id: str, label_ids=None) -> Dict[str, Any]
async def move_email(msg_id: str, destination_label_id: str, remove_label_ids=None) -> Dict[str, Any]
async def update_email(msg_id: str, add_label_ids=None, remove_label_ids=None) -> Dict[str, Any]
async def get_email(msg_id: str) -> NormalizedDocument
async def send_email(to: str, subject: str, body: str, cc=None, bcc=None) -> Dict[str, Any]
async def post_email(to: str, subject: str, body: str, cc=None, bcc=None) -> Dict[str, Any]
async def modify_message(msg_id: str, add_label_ids=None, remove_label_ids=None) -> Dict[str, Any]
async def delete_email(msg_id: str, permanent=False) -> Any
async def remove_email(msg_id: str) -> Any
async def delete_message(msg_id: str, permanent=False) -> Any
async def delete_thread(thread_id: str, permanent=False) -> Any
async def bulk_delete(query: str, permanent=False) -> BulkDeleteResult
async def disconnect() -> None
```

---

## 3. Client/SDK Layer

**HTTP client**: `client.http_client.GmailHTTPClient`

Instantiated by `connector._build_http_client()` using:
- `access_token` from `ensure_token()`
- `base_url` from `self.config.get('base_url', 'https://gmail.googleapis.com/gmail/v1')`

**Mock path**: `connector.GmailHTTPClient` (patch the class; constructor returns your mock)

**Methods to mock on GmailHTTPClient**:
- `execute_get_profile() -> Dict`
- `execute_list_messages(query, max_results, page_token) -> Dict`
- `execute_get_message(msg_id) -> Dict`
- `execute_modify_message(msg_id, add_label_ids, remove_label_ids) -> Dict`
- `execute_trash_message(msg_id) -> Dict`
- `execute_delete_message(msg_id) -> None`
- `execute_send_message(raw_message: str) -> Dict`
- `execute_trash_thread(thread_id) -> Dict`
- `execute_delete_thread(thread_id) -> None`

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
"""Unit-test conftest for Gmail connector — clean mocks, zero real I/O."""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GmailConnector
from shared.base_connector import AuthStatus, ConnectorHealth, TokenInfo

TENANT_ID = "test-tenant"
CONNECTOR_ID = "test-connector"

BASE_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "allow_permanent_delete": False,
    "redirect_uri": "https://example.com/callback",
    "known_message_ids": [],
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    mocker.patch.object(GmailConnector, "get_token", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(GmailConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_document", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def valid_token() -> TokenInfo:
    return TokenInfo(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )


@pytest.fixture
def connector() -> GmailConnector:
    return GmailConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=BASE_CONFIG.copy(),
    )


@pytest.fixture
def connector_no_creds() -> GmailConnector:
    cfg = {**BASE_CONFIG, "client_id": "", "client_secret": ""}
    return GmailConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)


@pytest.fixture
def connector_with_perm_delete() -> GmailConnector:
    cfg = {**BASE_CONFIG, "allow_permanent_delete": True}
    return GmailConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)


@pytest.fixture
def authed_connector(connector: GmailConnector, valid_token: TokenInfo) -> GmailConnector:
    connector._token_info = valid_token
    return connector


@pytest.fixture
def authed_perm_delete(connector_with_perm_delete: GmailConnector, valid_token: TokenInfo) -> GmailConnector:
    connector_with_perm_delete._token_info = valid_token
    return connector_with_perm_delete


@pytest.fixture
def mock_http_client() -> MagicMock:
    client = MagicMock()
    client.execute_get_profile = AsyncMock(
        return_value={"emailAddress": "user@example.com", "messagesTotal": 42}
    )
    client.execute_list_messages = AsyncMock(
        return_value={"messages": [{"id": "msg1", "threadId": "t1"}], "resultSizeEstimate": 1}
    )
    client.execute_get_message = AsyncMock(
        return_value={
            "id": "msg1",
            "threadId": "t1",
            "labelIds": ["INBOX"],
            "snippet": "Hello world",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "Test Subject"},
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "recv@example.com"},
                    {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                ],
                "body": {"data": "SGVsbG8gd29ybGQ="},
            },
        }
    )
    client.execute_trash_message = AsyncMock(return_value={"id": "msg1", "labelIds": ["TRASH"]})
    client.execute_delete_message = AsyncMock(return_value=None)
    client.execute_modify_message = AsyncMock(
        return_value={"id": "msg1", "labelIds": ["INBOX", "STARRED"]}
    )
    client.execute_send_message = AsyncMock(
        return_value={"id": "sent1", "threadId": "t1", "labelIds": ["SENT"]}
    )
    client.execute_trash_thread = AsyncMock(return_value={"id": "t1", "messages": []})
    client.execute_delete_thread = AsyncMock(return_value=None)
    return client


def make_aiohttp_post_mock(response_data: dict, status: int = 200) -> MagicMock:
    """Build a MagicMock aiohttp.ClientSession whose .post() returns response_data.

    session.post MUST be MagicMock (NOT AsyncMock) — it is used as an async context manager.
    """
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

```python
# Patch the HTTP client class so _build_http_client() returns your mock:
def patch_http_client(mocker, mock_http_client):
    mock_cls = mocker.patch("connector.GmailHTTPClient")
    mock_cls.return_value = mock_http_client
    return mock_cls

# Patch aiohttp for authorize() / on_token_refresh():
mock_session = make_aiohttp_post_mock({"access_token": "tok", ...}, status=200)
mocker.patch("connector.aiohttp.ClientSession", return_value=mock_session)

# Patch _remove_from_kb (internal helper):
mocker.patch.object(connector, "_remove_from_kb", new_callable=AsyncMock)
```

---

## 7. Auth-Type Specific Rules (oauth2_code)

**Mock these**:
- `client_id` and `client_secret` in `BASE_CONFIG`
- `aiohttp.ClientSession.post()` for token exchange and refresh via `make_aiohttp_post_mock`
- `connector._token_info` for pre-seeding a valid token in `authed_connector` fixture

**Do NOT test**:
- Real Google OAuth authorization redirects
- Actual token exchange with Google's servers
- BaseConnector `ensure_token()` or `set_token()` internals — mock via `mock_storage`

---

## 8. Do NOT Test

- `BaseConnector` internals (`ensure_token`, `set_token`, `clear_token`, `save_config`, `ingest_document`)
- `GmailHTTPClient` internals — test those in a separate `test_http_client.py`
- `helpers/normalizer.py` directly — test indirectly via `read_email()` / `get_email()` assertions
- Real aiohttp/network I/O
- Google token validation logic

---

## 9. Per-Method Test Specifications

### `install()`
- **Happy path**: `connector` with `client_id` + `client_secret` → `health=HEALTHY`, `auth_status=PENDING`, `connector_id=CONNECTOR_ID`
- **Missing client_id**: `connector_no_creds` → `health=DEGRADED`, `auth_status=INVALID_CREDENTIALS`
- **Missing client_secret only**: config `{"client_id": "x", "client_secret": ""}` → `health=DEGRADED`
- **Missing both**: `connector_no_creds` → `health=DEGRADED`

### `authorize(auth_code, state)`
- **Happy path**: mock 200 → returns `TokenInfo` with `access_token`, `refresh_token`; `scopes` from response `scope`
- **Uses config client_id/secret**: assert `session.post` payload contains `client_id="test-client-id"`, `client_secret="test-client-secret"`
- **Uses config token_url**: custom `token_url` in config → `session.post` called at that URL
- **Error non-200**: mock 400 → raises `ConnectorAuthError` matching "Token exchange failed"

### `on_token_refresh()`
- **Happy path**: `authed_connector` → mock 200 → `access_token` updated, existing `refresh_token` preserved
- **Uses config client_id/secret**: payload contains config credentials
- **Uses config token_url**: custom `token_url` → correct URL used
- **No token raises**: bare `connector` → raises `ConnectorAuthError`
- **Non-200 raises**: mock 400 → raises `ConnectorAuthError` matching "Token refresh failed"

### `health_check()`
- **Happy path**: `execute_get_profile` returns profile → `HEALTHY/CONNECTED`, message contains email
- **ConnectorAuthError**: → `DEGRADED/TOKEN_EXPIRED`
- **ConnectorPermissionError**: → `DEGRADED/INVALID_CREDENTIALS`
- **Generic exception**: → `OFFLINE/FAILED`

### `sync()`
- **Happy path**: one page, one message → `COMPLETED`, `documents_synced=1`, `documents_found=1`
- **Deletion propagation**: `known_message_ids=["old-id","msg1"]` + API returns only `msg1` → `_remove_from_kb("old-id")` called
- **Saves current IDs**: `save_config` called with `known_message_ids` containing `"msg1"`
- **Partial failure**: `execute_get_message` raises → `PARTIAL`, `documents_failed=1`
- **List failure**: `execute_list_messages` raises → `FAILED`
- **Incremental**: `since` set + `full=False` → query starts with `"after:"`
- **Full sync**: `full=True` → `query=""`
- **Multi-page**: two pages → `execute_list_messages` called twice

### `list_email()`
- **Happy path**: returns page dict; `execute_list_messages` called with correct kwargs
- **Page token forwarded**: `page_token="tok123"` → forwarded to client
- **Rate limit propagated**: raises `ConnectorRateLimitError` → caller sees it

### `read_email()`
- **Happy path**: `NormalizedDocument` with `id="msg1"`, `title="Test Subject"`, content "Hello world", `tenant_id=TENANT_ID`
- **Not found propagated**: raises `ConnectorNotFoundError`

### `add_email()`
- **Happy path**: `execute_modify_message` called with `add_label_ids=["STARRED"]`; returns result
- **Permission error propagated**: raises `ConnectorPermissionError`

### `move_email()`
- **Happy path**: `execute_modify_message` called with `add_label_ids=["LABEL_X"]`, `remove_label_ids=["INBOX"]`
- **Custom remove labels**: `remove_label_ids=["LABEL_A"]` → forwarded
- **Permission error propagated**: raises `ConnectorPermissionError`

### `update_email()`
- **Happy path**: `execute_modify_message` called with both `add_label_ids` and `remove_label_ids`
- **Empty args default to empty lists**: no labels → called with `[]`, `[]`
- **Permission error propagated**: raises `ConnectorPermissionError`

### `get_email()`
- **Happy path**: `NormalizedDocument` — identical assertions to `read_email()`
- **Not found propagated**: raises `ConnectorNotFoundError`

### `send_email()`
- **Happy path**: mock `build_mime_raw` returns `"base64str"`, `execute_send_message` returns `{"id": "sent1", "threadId": "t1"}` → assert dict returned with correct `id`
- **With cc/bcc**: assert `build_mime_raw` called with `cc` and `bcc` keyword args
- **403 scope missing**: `execute_send_message` raises `ConnectorPermissionError("gmail.send scope missing...")` → assert `ConnectorPermissionError` propagated

### `post_email()`
- **Delegates to send_email**: patch `connector.send_email` as `AsyncMock`; call `post_email(to, subject, body)` → assert `send_email` called once with same args

### `modify_message()`
- **Happy path**: `execute_modify_message` returns modified message → assert dict returned
- **add_label_ids forwarded**: assert passed to `execute_modify_message`
- **remove_label_ids forwarded**: assert passed to `execute_modify_message`
- **Both None default to empty lists**: assert `execute_modify_message` called with `add_label_ids=[]`, `remove_label_ids=[]`

### `delete_email()`
- **Delegates to delete_message**: mock `delete_message` → called with `(msg_id, permanent=False)`

### `remove_email()`
- **Delegates to soft delete**: mock `delete_message` → called with `(msg_id, permanent=False)`

### `delete_message()`
- **Soft delete**: `execute_trash_message` called; `execute_delete_message` not called
- **Hard delete with flag**: `authed_perm_delete` + `permanent=True` → `execute_delete_message` called
- **Hard delete blocked**: no flag + `permanent=True` → raises `ConnectorPermissionError` matching "allow_permanent_delete"
- **Not found propagated**: `execute_trash_message` raises `ConnectorNotFoundError`

### `delete_thread()`
- **Soft delete**: `execute_trash_thread` called
- **Hard delete with flag**: `execute_delete_thread` called
- **Hard delete blocked**: no flag → raises `ConnectorPermissionError`

### `bulk_delete()`
- **Soft all succeed**: two messages → `execute_trash_message` ×2; `deleted=2`, `failed=0`
- **Partial failure continues**: one trash raises → loop continues; `failed=1`, error contains msg_id
- **Multi-page**: two pages → `execute_list_messages` ×2; both IDs processed
- **Hard blocked without flag**: raises `ConnectorPermissionError`; list not called
- **Hard uses delete endpoint**: `authed_perm_delete` → `execute_delete_message` called

### `disconnect()`
- **Clears token**: `clear_token` called once

---

## 10. Test Dependencies

Already present in `requirements.txt`:
```
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.1
pytest-cov>=4.1.0
pytest-timeout==2.4.0
```
