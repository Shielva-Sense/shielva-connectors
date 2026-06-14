# Gmail Connector — Test Guidelines

## 1. Package Structure

```
google_gmail_88ec5d_connector/
├── tests/
│   ├── __init__.py
│   ├── conftest.py          # shared fixtures — connector, mock_http_client, valid_token
│   └── test_connector.py    # all unit tests (no network I/O)
├── connector.py
├── client/
│   └── http_client.py
├── helpers/
│   ├── normalizer.py
│   └── utils.py
├── exceptions.py
└── requirements.txt
```

---

## 2. Connector Class Details

**Class**: `GmailConnector` (in `connector.py`)
**Inherits**: `BaseConnector`

**Constructor**:
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
| `install` | `(config: Dict[str, Any] = None) -> ConnectorStatus` |
| `authorize` | `(auth_data: Dict[str, Any]) -> TokenInfo` |
| `health_check` | `() -> ConnectorStatus` |
| `sync` | `(since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult` |
| `list_email` | `(label_ids=None, query=None) -> List[Dict]` |
| `on_token_refresh` | `() -> TokenInfo` |
| `read_email` | `(msg_id: str, format="metadata", metadata_headers=None) -> Dict` |
| `add_email` | `(raw_b64: str) -> Dict` |
| `list_message` | `(label_ids=None, query=None, page_token=None, max_results=100) -> Dict` |
| `update_email` | `(msg_id: str, add_label_ids=None, remove_label_ids=None) -> Dict` |
| `label_email` | `(msg_id: str, label_ids=None) -> Dict` |
| `trash_email` | `(msg_id: str) -> Dict` |
| `delete_email` | `(msg_id: str) -> None` |
| `remove_email` | `(msg_id: str) -> None` |
| `batch_delete_emails` | `(msg_ids: List[str]) -> None` |

---

## 3. Client/SDK Layer

All Gmail REST API calls are made by `GmailHTTPClient` in `client/http_client.py`.

**Mock target**: `connector.GmailHTTPClient` (the class imported into `connector.py`)

```python
mocker.patch("connector.GmailHTTPClient", return_value=mock_instance)
```

`mock_instance` is a `MagicMock` whose `execute_*` attributes are `AsyncMock`.

**Full list of execute_* methods** (all `async`):

| Method | Returns |
|---|---|
| `execute_get_profile()` | `Dict` — `{ emailAddress, messagesTotal, threadsTotal }` |
| `execute_list_messages(label_ids, page_token, max_results, query)` | `Dict` — `{ messages, nextPageToken }` |
| `execute_get_message(msg_id, format, metadata_headers)` | `Dict` — full message resource |
| `execute_modify_message(msg_id, add_label_ids, remove_label_ids)` | `Dict` — updated message resource |
| `execute_import_message(raw_b64)` | `Dict` — `{ id, threadId, labelIds }` |
| `execute_trash_message(msg_id)` | `Dict` — message resource with `TRASH` in labelIds |
| `execute_delete_message(msg_id)` | `None` |
| `execute_batch_delete_messages(msg_ids)` | `None` |

---

## 4. Required Packages

```
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.1
pytest-cov>=4.1.0
pytest-timeout==2.4.0
```

All already present in `requirements.txt`.

---

## 5. Fixture Blueprint — conftest.py

```python
"""Shared fixtures for Gmail connector unit tests — fully mocked, zero real I/O."""
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# Relative path resolution — NEVER use absolute paths
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GmailConnector
from shared.base_connector import AuthStatus, ConnectorHealth, TokenInfo

CONNECTOR_CONFIG = {
    "client_id":          "test_client_id",
    "client_secret":      "test_client_secret",
    "scopes":             "https://www.googleapis.com/auth/gmail.modify https://mail.google.com/",
    "auth_url":           "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url":          "https://oauth2.googleapis.com/token",
    "base_url":           "https://gmail.googleapis.com",
    "rate_limit_per_min": 100,
    "pagination_type":    "page_token",
    "api_version":        "v1",
    "redirect_uri":       "https://app.example.com/oauth/callback",
}

RAW_MESSAGE = {
    "id": "msg1",
    "threadId": "thread1",
    "snippet": "Hello world preview text",
    "labelIds": ["INBOX", "UNREAD"],
    "payload": {
        "headers": [
            {"name": "Subject", "value": "Test Subject"},
            {"name": "From",    "value": "sender@example.com"},
            {"name": "Date",    "value": "Fri, 13 Jun 2026 10:00:00 +0000"},
        ]
    },
}

RAW_TRASHED_MESSAGE = {**RAW_MESSAGE, "labelIds": ["TRASH"]}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector methods that touch Redis/HTTP from running."""
    mocker.patch.object(GmailConnector, "set_token",     new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "clear_token",   new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_batch",  new_callable=AsyncMock, return_value=True)
    mocker.patch.object(GmailConnector, "report_status", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structured logger to avoid keyword-arg noise in test output."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector_config():
    return dict(CONNECTOR_CONFIG)


@pytest.fixture
def connector(connector_config):
    return GmailConnector(
        tenant_id="test_tenant",
        connector_id="test_connector",
        config=connector_config,
    )


@pytest.fixture
def valid_token():
    return TokenInfo(
        access_token="test_access_token",
        refresh_token="test_refresh_token",
        expires_at=datetime(2099, 12, 31),
        token_type="Bearer",
        scopes=[
            "https://www.googleapis.com/auth/gmail.modify",
            "https://mail.google.com/",
        ],
    )


@pytest.fixture
def connector_with_token(connector, valid_token):
    """Connector with a valid in-memory token (ensure_token() returns immediately)."""
    connector._token_info = valid_token
    return connector


@pytest.fixture
def mock_http_client(mocker):
    """Patch GmailHTTPClient at import path in connector module."""
    mock_instance = MagicMock()
    mock_instance.execute_get_profile = AsyncMock(return_value={
        "emailAddress": "user@gmail.com",
        "messagesTotal": 5,
        "threadsTotal": 3,
    })
    mock_instance.execute_list_messages = AsyncMock(return_value={
        "messages": [{"id": "msg1", "threadId": "thread1"}],
        "nextPageToken": None,
    })
    mock_instance.execute_get_message = AsyncMock(return_value=dict(RAW_MESSAGE))
    mock_instance.execute_modify_message = AsyncMock(return_value=dict(RAW_MESSAGE))
    mock_instance.execute_import_message = AsyncMock(return_value={
        "id": "new_msg_id", "threadId": "t1", "labelIds": [],
    })
    mock_instance.execute_trash_message = AsyncMock(return_value=dict(RAW_TRASHED_MESSAGE))
    mock_instance.execute_delete_message = AsyncMock(return_value=None)
    mock_instance.execute_batch_delete_messages = AsyncMock(return_value=None)
    mocker.patch("connector.GmailHTTPClient", return_value=mock_instance)
    return mock_instance
```

---

## 6. Mock Patterns

### Patch the HTTP client (preferred — covers all methods at once)
```python
mocker.patch("connector.GmailHTTPClient", return_value=mock_instance)
```

### Simulate a GmailNotFoundError
```python
from exceptions import GmailNotFoundError
mock_http_client.execute_delete_message = AsyncMock(side_effect=GmailNotFoundError("HTTP 404"))
```

### Simulate a GmailAuthError (403 scope error)
```python
from exceptions import GmailAuthError
mock_http_client.execute_batch_delete_messages = AsyncMock(side_effect=GmailAuthError("HTTP 403"))
```

### Patch aiohttp for authorize() tests
```python
# session.post must be MagicMock (NOT AsyncMock) — it returns a context manager, not a coroutine
mock_post_cm = MagicMock()
mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
mock_post_cm.__aexit__ = AsyncMock(return_value=None)
mock_session_instance.post = MagicMock(return_value=mock_post_cm)
```

### Inject a token to bypass ensure_token
```python
connector._token_info = valid_token
```

---

## 7. Auth-Type Specific Rules

Auth type: `oauth2_code`

- **Mock `ensure_token()`** by injecting a `TokenInfo` into `connector._token_info` directly (see `connector_with_token` fixture).
- **Do NOT** test the real Google OAuth2 token endpoint.
- **Do NOT** test `Credentials.refresh()` against a live server.
- **Mock `aiohttp.ClientSession`** for `authorize()` tests using the aiohttp CM pattern in §6.
- **Do NOT** test `on_token_refresh()` against live credentials — mock `connector.Credentials` and `connector.asyncio.get_event_loop`.

---

## 8. Do NOT Test

- `BaseConnector` methods: `save_config`, `get_token`, `set_token`, `clear_token`, `ensure_token`, `ingest_batch`, `report_status`
- `googleapiclient.discovery.build` internals
- `google.auth.transport.requests.Request` internals
- Real Gmail REST API calls
- tenacity retry decorators (test connector behavior, not retry internals)
- Python stdlib modules

---

## 9. Per-Method Test Specifications

### 9.1 install()
- **Happy path**: `{client_id, client_secret}` → `HEALTHY + PENDING`, message contains "Authorize"
- **Missing client_id**: `{client_secret}` → `UNHEALTHY + MISSING_CREDENTIALS`, message mentions "client_id"
- **Missing client_secret**: `{client_id}` → `UNHEALTHY + MISSING_CREDENTIALS`, message mentions "client_secret"
- **Empty config**: `{}` → `UNHEALTHY + MISSING_CREDENTIALS`
- **save_config called**: `save_config` awaited once on success

### 9.2 authorize()
- **Happy path**: mock POST returns `{access_token, refresh_token, expires_in, scope}` → `TokenInfo` with correct fields
- **Scope fallback**: POST returns `scope=""` → `result.scopes == list(GmailConnector.REQUIRED_SCOPES)`
- **Token persisted**: `set_token` awaited once
- **POST URL**: token endpoint contains `oauth2.googleapis.com/token`
- **redirect_uri**: payload includes `redirect_uri` from config
- **HTTP error**: `raise_for_status()` raises `ClientResponseError` → propagates

### 9.3 health_check()
- **Happy path**: `execute_get_profile` returns `{emailAddress: "user@gmail.com"}` → `HEALTHY + CONNECTED`, message contains email
- **connector_id set**: `result.connector_id == "test_connector"`
- **Auth error**: `execute_get_profile` raises `GmailAuthError` → `UNHEALTHY + TOKEN_EXPIRED`
- **Unexpected error**: `execute_get_profile` raises `RuntimeError` → `UNHEALTHY + FAILED`, message contains error text

### 9.4 sync()
- **Full sync**: mocked `list_email` returns 1 message → `COMPLETED`, `documents_found=1`, `documents_synced=1`
- **Incremental — after query built**: `sync(since=datetime(...), full=False)` → `list_email` called with `query` containing `"after:"`
- **Full ignores since**: `sync(since=..., full=True)` → `list_email` called with `query=None`
- **Empty results**: → `COMPLETED`, `documents_synced=0`
- **Auth error**: → `FAILED`, message propagated
- **Rate limit error**: → `FAILED`
- **Unexpected error**: → `FAILED`
- **ingest_batch called**: when docs exist, `ingest_batch` awaited once
- **Multi-tenant doc ID**: doc.id contains `tenant_id`

### 9.5 list_email()
- **Returns messages**: result is a list, `result[0]["id"] == "msg1"`
- **Calls get_message**: `execute_get_message` awaited with `msg_id="msg1"`, `format="metadata"`
- **Multi-page**: two calls to `execute_list_messages` when first returns `nextPageToken`; result has 2 messages
- **Page token passed**: second call receives `page_token="tok2"`
- **Empty inbox**: → `[]`, `execute_get_message` not called
- **Skip failed fetch**: one of two fails → result has 1 message
- **Query forwarded**: `list_email(query="after:123")` → `execute_list_messages` receives `query="after:123"`
- **Default labels**: `execute_list_messages` receives `label_ids=["INBOX", "UNREAD"]`
- **Custom labels**: `list_email(label_ids=["SENT"])` → `execute_list_messages` receives `label_ids=["SENT"]`

### 9.6 on_token_refresh()
- **Happy path**: mock `Credentials` + `run_in_executor` → returns `TokenInfo` with new `access_token`
- **Token persisted**: `set_token` awaited once
- **No refresh token**: `connector._token_info.refresh_token = None` → raises `GmailAuthError`
- **No token at all**: `connector._token_info = None` → raises `GmailAuthError`
- **Correct config**: `Credentials()` called with `client_id="test_client_id"`, `client_secret="test_client_secret"`

### 9.7 read_email()
- **Happy path**: `execute_get_message(msg_id="msg1")` → returns raw dict containing `"id": "msg1"`
- **Custom format**: `read_email(msg_id="x", format="full")` → `execute_get_message` called with `format="full"`
- **Not found**: `execute_get_message` raises `GmailNotFoundError` → propagates

### 9.8 add_email()
- **Happy path**: `execute_import_message(raw_b64="base64data")` → result contains `"id": "new_msg_id"`
- **Delegates once**: `execute_import_message` awaited exactly once with the provided `raw_b64`
- **Error propagates**: `execute_import_message` raises `GmailAPIError` → propagates

### 9.9 list_message()
- **Happy path**: returns raw dict `{messages: [...], nextPageToken: None}`
- **Default label_ids**: `execute_list_messages` called with `label_ids=["INBOX"]`
- **page_token forwarded**: `list_message(page_token="tok")` → `execute_list_messages` receives `page_token="tok"`
- **max_results forwarded**: `list_message(max_results=50)` → `execute_list_messages` receives `max_results=50`
- **query forwarded**: `list_message(query="is:unread")` → `execute_list_messages` receives `query="is:unread"`

### 9.10 update_email()
- **Happy path**: `execute_modify_message(msg_id="msg1", add_label_ids=["STARRED"], remove_label_ids=["UNREAD"])` → updated message dict
- **Labels forwarded**: verify both `add_label_ids` and `remove_label_ids` passed through correctly
- **Error propagates**: `execute_modify_message` raises `GmailAPIError` → propagates

### 9.11 label_email()
- **Happy path**: `execute_modify_message(msg_id="msg1", add_label_ids=["STARRED"], remove_label_ids=[])` → updated message dict
- **remove_label_ids always empty**: `label_email(msg_id="x", label_ids=["A"])` → `execute_modify_message` called with `remove_label_ids=[]`
- **None label_ids → empty list**: `label_email(msg_id="x")` → `execute_modify_message` called with `add_label_ids=[]`
- **Error propagates**: raises `GmailAPIError` → propagates

### 9.12 trash_email()
- **Happy path**: `execute_trash_message(msg_id="msg1")` returns `{..., labelIds: ["TRASH"]}` → `"TRASH" in result["labelIds"]`
- **Delegates once**: `execute_trash_message` awaited with `msg_id="msg1"`
- **Not found**: `execute_trash_message` raises `GmailNotFoundError` → propagates
- **Auth error**: raises `GmailAuthError` → propagates

### 9.13 delete_email()
- **Happy path**: `execute_delete_message(msg_id="msg1")` returns `None` → method returns `None`
- **Delegates once**: `execute_delete_message` awaited with `msg_id="msg1"`
- **Not found**: `execute_delete_message` raises `GmailNotFoundError` → propagates
- **Auth error (403)**: raises `GmailAuthError` → propagates

### 9.14 remove_email()
- **Delegates to delete_email**: `remove_email("msg1")` calls `delete_email("msg1")` — assert via `mocker.spy` or patching `delete_email`
- **Error propagates**: if `delete_email` raises, `remove_email` re-raises it

### 9.15 batch_delete_emails()
- **Happy path**: list of 3 IDs → `execute_batch_delete_messages` awaited with `msg_ids=["id1","id2","id3"]`, returns `None`
- **Empty list guard**: `batch_delete_emails([])` → raises `ValueError("msg_ids must be a non-empty list")`; http_client NOT called
- **Exceeds 1000**: `batch_delete_emails(["id"] * 1001)` → raises `ValueError("at most 1000")`; http_client NOT called
- **Auth error (403)**: `execute_batch_delete_messages` raises `GmailAuthError` → propagates
- **IDs forwarded exactly**: verify the provided `msg_ids` list is passed to `execute_batch_delete_messages`

---

## 10. Test Dependencies

All already in `requirements.txt`:
```
# ── Test dependencies ──
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.1
pytest-cov>=4.1.0
pytest-timeout==2.4.0
```

### ⚠️ Existing test that needs updating after scope upgrade

`test_required_scopes_includes_gmail_readonly` asserts `"gmail.readonly" in REQUIRED_SCOPES` — this will **fail** after the scope upgrade. Replace with:
```python
def test_required_scopes_includes_gmail_modify(self):
    assert any("gmail.modify" in s for s in GmailConnector.REQUIRED_SCOPES)

def test_required_scopes_includes_full_access(self):
    assert "https://mail.google.com/" in GmailConnector.REQUIRED_SCOPES
```
