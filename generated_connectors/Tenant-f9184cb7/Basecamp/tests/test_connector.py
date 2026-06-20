"""Unit tests for BasecampConnector — all HTTP calls are mocked.

Test coverage:
  - exceptions hierarchy                   (5 tests)
  - models / dataclasses                   (5 tests)
  - normalize_project                      (7 tests)
  - normalize_todo                         (6 tests)
  - normalize_message                      (5 tests)
  - normalize_document                     (5 tests)
  - with_retry                             (6 tests)
  - HTTP client: headers + User-Agent      (3 tests)
  - HTTP client: get_authorization         (3 tests)
  - HTTP client: get_projects + pagination (3 tests)
  - HTTP client: get_project               (2 tests)
  - HTTP client: get_todo_lists            (2 tests)
  - HTTP client: get_todos                 (2 tests)
  - HTTP client: get_messages              (2 tests)
  - HTTP client: get_documents             (2 tests)
  - HTTP client: error codes               (5 tests)
  - HTTP client: account_id URL            (2 tests)
  - install()                              (6 tests)
  - health_check()                         (5 tests)
  - sync()                                 (8 tests)
  - list_*() connector methods             (5 tests)
  - authorize URL                          (3 tests)
  ─────────────────────────────────────────────
  Total                                   ≥ 91 tests
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AUTH_TYPE, CONNECTOR_TYPE, BasecampConnector
from exceptions import (
    BasecampAuthError,
    BasecampError,
    BasecampNetworkError,
    BasecampNotFoundError,
    BasecampRateLimitError,
)
from helpers.utils import (
    normalize_document,
    normalize_message,
    normalize_project,
    normalize_todo,
    with_retry,
)
from models import (
    AuthStatus,
    BasecampAccount,
    ConnectorHealth,
    ResourceType,
    SyncStatus,
)

# ── Constants ─────────────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_basecamp_test_001"
ACCESS_TOKEN = "test_oauth2_access_token_abc123"
ACCOUNT_ID = "1234567"

# ── Sample Basecamp data ──────────────────────────────────────────────────────

SAMPLE_IDENTITY = {
    "id": 9001,
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email_address": "ada@37signals.com",
}

SAMPLE_BC_ACCOUNT = {
    "id": int(ACCOUNT_ID),
    "name": "Shielva Ltd",
    "product": "bc3",
    "href": f"https://3.basecampapi.com/{ACCOUNT_ID}",
    "app_href": f"https://3.basecamp.com/{ACCOUNT_ID}",
}

SAMPLE_AUTH_RESPONSE = {
    "expires_at": "2026-09-20T00:00:00.000Z",
    "identity": SAMPLE_IDENTITY,
    "accounts": [SAMPLE_BC_ACCOUNT],
}

SAMPLE_PROJECT = {
    "id": 10001,
    "name": "Website Redesign",
    "description": "<p>Redesign the company website for Q3.</p>",
    "status": "active",
    "purpose": "company",
    "app_url": f"https://3.basecamp.com/{ACCOUNT_ID}/projects/10001",
    "created_at": "2026-01-10T09:00:00.000Z",
    "updated_at": "2026-06-01T14:30:00.000Z",
}

SAMPLE_PROJECT_2 = {
    "id": 10002,
    "name": "Mobile App v2",
    "description": "",
    "status": "active",
    "purpose": "company",
    "app_url": f"https://3.basecamp.com/{ACCOUNT_ID}/projects/10002",
    "created_at": "2026-02-01T08:00:00.000Z",
    "updated_at": "2026-06-15T10:00:00.000Z",
}

SAMPLE_TODOLIST = {
    "id": 20001,
    "name": "Design tasks",
    "description": "",
    "type": "Todolist",
    "app_url": f"https://3.basecamp.com/{ACCOUNT_ID}/buckets/10001/todolists/20001",
}

SAMPLE_TODO = {
    "id": 30001,
    "title": "Create wireframes",
    "content": "<p>Use Figma to create initial wireframes.</p>",
    "completed": False,
    "due_on": "2026-07-20",
    "todolist_id": 20001,
    "assignees": [{"id": 9001, "name": "Ada Lovelace"}],
    "app_url": f"https://3.basecamp.com/{ACCOUNT_ID}/buckets/10001/todos/30001",
    "created_at": "2026-06-01T10:00:00.000Z",
    "updated_at": "2026-06-10T12:00:00.000Z",
}

SAMPLE_TODO_COMPLETED = {
    "id": 30002,
    "title": "Gather brand assets",
    "content": "",
    "completed": True,
    "due_on": "",
    "todolist_id": 20001,
    "assignees": [],
    "app_url": f"https://3.basecamp.com/{ACCOUNT_ID}/buckets/10001/todos/30002",
    "created_at": "2026-06-02T09:00:00.000Z",
    "updated_at": "2026-06-05T11:00:00.000Z",
}

SAMPLE_MESSAGE = {
    "id": 40001,
    "subject": "Kickoff meeting notes",
    "content": "<p>We discussed scope, timeline, and budget.</p>",
    "creator": {"id": 9001, "name": "Ada Lovelace"},
    "app_url": f"https://3.basecamp.com/{ACCOUNT_ID}/buckets/10001/messages/40001",
    "created_at": "2026-06-05T14:00:00.000Z",
    "updated_at": "2026-06-05T14:30:00.000Z",
}

SAMPLE_DOCUMENT = {
    "id": 50001,
    "title": "Brand Guidelines",
    "content": "<p>Official brand guidelines document.</p>",
    "creator": {"id": 9001, "name": "Ada Lovelace"},
    "app_url": f"https://3.basecamp.com/{ACCOUNT_ID}/buckets/10001/vaults/60001/documents/50001",
    "created_at": "2026-05-20T10:00:00.000Z",
    "updated_at": "2026-06-01T09:00:00.000Z",
}

SAMPLE_PROJECT_WITH_VAULT = {
    **SAMPLE_PROJECT,
    "dock": [
        {"id": 60001, "name": "vault", "title": "Docs"},
        {"id": 70001, "name": "todoset", "title": "To-dos"},
    ],
}

# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_connector(
    access_token: str = ACCESS_TOKEN,
    account_id: str = ACCOUNT_ID,
) -> BasecampConnector:
    return BasecampConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "access_token": access_token,
            "account_id": account_id,
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
            "redirect_uri": "https://app.shielva.ai/oauth/basecamp/callback",
        },
    )


@pytest.fixture()
def connector() -> BasecampConnector:
    c = make_connector()
    c.client = MagicMock()
    return c


# ═══════════════════════════════════════════════════════════════════════════════
# EXCEPTIONS
# ═══════════════════════════════════════════════════════════════════════════════


def test_basecamp_error_base_fields() -> None:
    exc = BasecampError("something broke", status_code=500, code="server_err")
    assert exc.status_code == 500
    assert exc.code == "server_err"
    assert "something broke" in str(exc)


def test_basecamp_auth_error_is_subclass() -> None:
    exc = BasecampAuthError("Unauthorized", status_code=401)
    assert isinstance(exc, BasecampError)
    assert exc.status_code == 401
    assert exc.code == "auth_error"


def test_basecamp_rate_limit_error_retry_after() -> None:
    exc = BasecampRateLimitError("too many requests", retry_after=45.0)
    assert exc.retry_after == 45.0
    assert exc.status_code == 429
    assert exc.code == "rate_limit"


def test_basecamp_not_found_error_message() -> None:
    exc = BasecampNotFoundError("project", "10001")
    assert "10001" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_basecamp_network_error_inherits_base() -> None:
    exc = BasecampNetworkError("connection refused")
    assert isinstance(exc, BasecampError)
    assert "connection refused" in str(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_health_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"


def test_resource_type_values() -> None:
    assert ResourceType.PROJECT == "project"
    assert ResourceType.TODO == "todo"
    assert ResourceType.MESSAGE == "message"
    assert ResourceType.DOCUMENT == "document"


def test_basecamp_account_from_dict() -> None:
    account = BasecampAccount.from_dict(SAMPLE_BC_ACCOUNT)
    assert account.id == int(ACCOUNT_ID)
    assert account.name == "Shielva Ltd"
    assert account.product == "bc3"
    assert "basecampapi" in account.href


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_project()
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_project_basic() -> None:
    doc = normalize_project(SAMPLE_PROJECT)
    assert doc.title == "Website Redesign"
    assert doc.metadata["project_id"] == 10001
    assert doc.metadata["status"] == "active"
    assert "Website Redesign" in doc.content


def test_normalize_project_source_id_stable() -> None:
    doc = normalize_project(SAMPLE_PROJECT)
    expected = hashlib.sha256(f"project:{SAMPLE_PROJECT['id']}".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_project_html_stripped_from_description() -> None:
    doc = normalize_project(SAMPLE_PROJECT)
    assert "<p>" not in doc.content
    assert "Redesign the company website" in doc.content


def test_normalize_project_empty_description() -> None:
    doc = normalize_project(SAMPLE_PROJECT_2)
    assert "Mobile App v2" in doc.content


def test_normalize_project_source_url() -> None:
    doc = normalize_project(SAMPLE_PROJECT)
    assert str(SAMPLE_PROJECT["id"]) in doc.source_url


def test_normalize_project_metadata_timestamps() -> None:
    doc = normalize_project(SAMPLE_PROJECT)
    assert doc.metadata["created_at"] == "2026-01-10T09:00:00.000Z"
    assert doc.metadata["updated_at"] == "2026-06-01T14:30:00.000Z"


def test_normalize_project_missing_id_falls_back() -> None:
    raw = {**SAMPLE_PROJECT, "id": 0, "name": "Unnamed"}
    doc = normalize_project(raw)
    assert doc.metadata["project_id"] == 0
    assert doc.title == "Unnamed"


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_todo()
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_todo_basic() -> None:
    doc = normalize_todo(SAMPLE_TODO, 10001)
    assert doc.title == "Create wireframes"
    assert doc.metadata["todo_id"] == 30001
    assert doc.metadata["project_id"] == 10001
    assert doc.metadata["completed"] is False


def test_normalize_todo_source_id_stable() -> None:
    doc = normalize_todo(SAMPLE_TODO, 10001)
    expected = hashlib.sha256(f"todo:{SAMPLE_TODO['id']}".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_todo_html_stripped_from_content() -> None:
    doc = normalize_todo(SAMPLE_TODO, 10001)
    assert "<p>" not in doc.content
    assert "Figma" in doc.content


def test_normalize_todo_completed_status_in_content() -> None:
    doc = normalize_todo(SAMPLE_TODO_COMPLETED, 10001)
    assert "completed" in doc.content


def test_normalize_todo_assignees_in_content_and_metadata() -> None:
    doc = normalize_todo(SAMPLE_TODO, 10001)
    assert "Ada Lovelace" in doc.content
    assert "Ada Lovelace" in doc.metadata["assignees"]


def test_normalize_todo_due_date_in_content() -> None:
    doc = normalize_todo(SAMPLE_TODO, 10001)
    assert "2026-07-20" in doc.content


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_message()
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_message_basic() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, 10001)
    assert doc.title == "Kickoff meeting notes"
    assert doc.metadata["message_id"] == 40001
    assert doc.metadata["project_id"] == 10001


def test_normalize_message_source_id_stable() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, 10001)
    expected = hashlib.sha256(f"message:{SAMPLE_MESSAGE['id']}".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_message_html_body_stripped() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, 10001)
    assert "<p>" not in doc.content
    assert "scope, timeline" in doc.content


def test_normalize_message_creator_in_content() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, 10001)
    assert "Ada Lovelace" in doc.content


def test_normalize_message_no_creator() -> None:
    raw = {**SAMPLE_MESSAGE, "creator": None}
    doc = normalize_message(raw, 10001)
    assert doc.metadata["creator_name"] == ""


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_document()
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_document_basic() -> None:
    doc = normalize_document(SAMPLE_DOCUMENT, 10001)
    assert doc.title == "Brand Guidelines"
    assert doc.metadata["document_id"] == 50001
    assert doc.metadata["project_id"] == 10001


def test_normalize_document_source_id_stable() -> None:
    doc = normalize_document(SAMPLE_DOCUMENT, 10001)
    expected = hashlib.sha256(f"document:{SAMPLE_DOCUMENT['id']}".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_document_html_stripped() -> None:
    doc = normalize_document(SAMPLE_DOCUMENT, 10001)
    assert "<p>" not in doc.content
    assert "Official brand guidelines" in doc.content


def test_normalize_document_creator_in_metadata() -> None:
    doc = normalize_document(SAMPLE_DOCUMENT, 10001)
    assert doc.metadata["creator_name"] == "Ada Lovelace"


def test_normalize_document_source_url() -> None:
    doc = normalize_document(SAMPLE_DOCUMENT, 10001)
    assert str(SAMPLE_DOCUMENT["id"]) in doc.source_url


# ═══════════════════════════════════════════════════════════════════════════════
# with_retry()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    fn = AsyncMock(side_effect=[BasecampNetworkError("transient"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_auth_immediately() -> None:
    fn = AsyncMock(side_effect=BasecampAuthError("Unauthorized"))
    with pytest.raises(BasecampAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=BasecampNetworkError("always fails"))
    with pytest.raises(BasecampNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retried() -> None:
    fn = AsyncMock(
        side_effect=[
            BasecampRateLimitError("rate limited", retry_after=0),
            {"ok": True},
        ]
    )
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_rate_limit_exhausts() -> None:
    fn = AsyncMock(
        side_effect=BasecampRateLimitError("always rate limited", retry_after=0)
    )
    with pytest.raises(BasecampRateLimitError):
        await with_retry(fn, max_attempts=2, base_delay=0)
    assert fn.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — Bearer token + User-Agent headers
# ═══════════════════════════════════════════════════════════════════════════════


def test_http_client_bearer_token_header() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={"access_token": "my_token_xyz"})
    headers = client._headers()
    assert headers["Authorization"] == "Bearer my_token_xyz"


def test_http_client_user_agent_header() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={"access_token": "tok"})
    headers = client._headers()
    assert "Shielva" in headers["User-Agent"]
    assert "contact@shielva.ai" in headers["User-Agent"]


def test_http_client_accept_json_header() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={"access_token": "tok"})
    headers = client._headers()
    assert "application/json" in headers["Accept"]


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — account_id URL construction
# ═══════════════════════════════════════════════════════════════════════════════


def test_http_client_base_url_uses_account_id() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={"account_id": "9999888"})
    assert "9999888" in client._base_url()


def test_http_client_base_url_template() -> None:
    from client.http_client import BasecampHTTPClient, API_BASE_TEMPLATE
    client = BasecampHTTPClient(config={"account_id": "42"})
    assert client._base_url() == API_BASE_TEMPLATE.format(account_id="42")


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — get_authorization
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_get_authorization_success() -> None:
    from client.http_client import BasecampHTTPClient, AUTH_URL
    client = BasecampHTTPClient(config={"access_token": ACCESS_TOKEN})
    client._get = AsyncMock(return_value=(SAMPLE_AUTH_RESPONSE, ""))  # type: ignore[method-assign]
    result = await client.get_authorization()
    assert result["identity"]["first_name"] == "Ada"
    assert len(result["accounts"]) == 1
    client._get.assert_called_once_with(AUTH_URL)


@pytest.mark.asyncio
async def test_http_client_get_authorization_auth_error() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={"access_token": "bad_token"})
    client._get = AsyncMock(side_effect=BasecampAuthError("Unauthorized"))  # type: ignore[method-assign]
    with pytest.raises(BasecampAuthError):
        await client.get_authorization()


@pytest.mark.asyncio
async def test_http_client_get_authorization_returns_dict() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={"access_token": ACCESS_TOKEN})
    client._get = AsyncMock(return_value=(SAMPLE_AUTH_RESPONSE, ""))  # type: ignore[method-assign]
    result = await client.get_authorization()
    assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — get_projects (Link header pagination)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_get_projects_single_page() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    client._get = AsyncMock(  # type: ignore[method-assign]
        return_value=([SAMPLE_PROJECT, SAMPLE_PROJECT_2], "")
    )
    result = await client.get_projects()
    assert len(result) == 2
    assert result[0]["id"] == 10001


@pytest.mark.asyncio
async def test_http_client_get_projects_link_header_pagination() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    next_link = f'<https://3.basecampapi.com/{ACCOUNT_ID}/projects.json?page=2>; rel="next"'
    client._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            ([SAMPLE_PROJECT], next_link),
            ([SAMPLE_PROJECT_2], ""),
        ]
    )
    result = await client.get_projects()
    assert len(result) == 2
    assert client._get.call_count == 2


@pytest.mark.asyncio
async def test_http_client_get_projects_empty() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    client._get = AsyncMock(return_value=([], ""))  # type: ignore[method-assign]
    result = await client.get_projects()
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — get_project
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_get_project_success() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    client._get = AsyncMock(return_value=(SAMPLE_PROJECT_WITH_VAULT, ""))  # type: ignore[method-assign]
    result = await client.get_project(10001)
    assert result["id"] == 10001
    assert "dock" in result


@pytest.mark.asyncio
async def test_http_client_get_project_not_found() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    client._get = AsyncMock(side_effect=BasecampNotFoundError("project", "99999"))  # type: ignore[method-assign]
    with pytest.raises(BasecampNotFoundError):
        await client.get_project(99999)


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — get_todo_lists
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_get_todo_lists_success() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    client._get = AsyncMock(return_value=([SAMPLE_TODOLIST], ""))  # type: ignore[method-assign]
    result = await client.get_todo_lists(10001)
    assert len(result) == 1
    assert result[0]["id"] == 20001


@pytest.mark.asyncio
async def test_http_client_get_todo_lists_empty() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    client._get = AsyncMock(return_value=([], ""))  # type: ignore[method-assign]
    result = await client.get_todo_lists(10001)
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — get_todos
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_get_todos_success() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    client._get = AsyncMock(  # type: ignore[method-assign]
        return_value=([SAMPLE_TODO, SAMPLE_TODO_COMPLETED], "")
    )
    result = await client.get_todos(10001, 20001)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_http_client_get_todos_empty() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    client._get = AsyncMock(return_value=([], ""))  # type: ignore[method-assign]
    result = await client.get_todos(10001, 20001)
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — get_messages
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_get_messages_success() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    client._get = AsyncMock(return_value=([SAMPLE_MESSAGE], ""))  # type: ignore[method-assign]
    result = await client.get_messages(10001)
    assert len(result) == 1
    assert result[0]["id"] == 40001


@pytest.mark.asyncio
async def test_http_client_get_messages_empty() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    client._get = AsyncMock(return_value=([], ""))  # type: ignore[method-assign]
    result = await client.get_messages(10001)
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — get_documents
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_get_documents_with_vault() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    # get_project returns project with vault in dock
    client._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            (SAMPLE_PROJECT_WITH_VAULT, ""),   # get_project call
            ([SAMPLE_DOCUMENT], ""),            # get_all_pages for docs
        ]
    )
    result = await client.get_documents(10001)
    assert len(result) == 1
    assert result[0]["id"] == 50001


@pytest.mark.asyncio
async def test_http_client_get_documents_no_vault_returns_empty() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(
        config={"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}
    )
    project_no_vault = {**SAMPLE_PROJECT, "dock": [{"id": 70001, "name": "todoset"}]}
    client._get = AsyncMock(return_value=(project_no_vault, ""))  # type: ignore[method-assign]
    result = await client.get_documents(10001)
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — error status codes (via _raise_for_status)
# ═══════════════════════════════════════════════════════════════════════════════


def test_raise_for_status_401_raises_auth_error() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={})
    with pytest.raises(BasecampAuthError) as exc_info:
        client._raise_for_status(401, {"error": "Unauthorized"})
    assert exc_info.value.status_code == 401


def test_raise_for_status_403_raises_auth_error() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={})
    with pytest.raises(BasecampAuthError) as exc_info:
        client._raise_for_status(403, {})
    assert exc_info.value.status_code == 403


def test_raise_for_status_404_raises_not_found() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={})
    with pytest.raises(BasecampNotFoundError):
        client._raise_for_status(404, {})


def test_raise_for_status_429_raises_rate_limit() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={})
    with pytest.raises(BasecampRateLimitError) as exc_info:
        client._raise_for_status(429, {"retry_after": 30})
    assert exc_info.value.status_code == 429


def test_raise_for_status_500_raises_network_error() -> None:
    from client.http_client import BasecampHTTPClient
    client = BasecampHTTPClient(config={})
    with pytest.raises(BasecampNetworkError):
        client._raise_for_status(500, {"error": "Internal Server Error"})


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT — Link header parsing
# ═══════════════════════════════════════════════════════════════════════════════


def test_parse_next_link_extracts_url() -> None:
    from client.http_client import _parse_next_link
    header = '<https://3.basecampapi.com/123/projects.json?page=2>; rel="next"'
    result = _parse_next_link(header)
    assert result == "https://3.basecampapi.com/123/projects.json?page=2"


def test_parse_next_link_returns_none_when_absent() -> None:
    from client.http_client import _parse_next_link
    result = _parse_next_link("")
    assert result is None


def test_parse_next_link_returns_none_for_prev_only() -> None:
    from client.http_client import _parse_next_link
    header = '<https://3.basecampapi.com/123/projects.json?page=1>; rel="prev"'
    result = _parse_next_link(header)
    assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector: BasecampConnector) -> None:
    connector.client.get_authorization = AsyncMock(return_value=SAMPLE_AUTH_RESPONSE)
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Ada" in result.message


@pytest.mark.asyncio
async def test_install_missing_access_token() -> None:
    c = make_connector(access_token="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_invalid_token(connector: BasecampConnector) -> None:
    connector.client.get_authorization = AsyncMock(
        side_effect=BasecampAuthError("Unauthorized")
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error(connector: BasecampConnector) -> None:
    connector.client.get_authorization = AsyncMock(
        side_effect=BasecampNetworkError("Connection refused")
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_stores_account_id(connector: BasecampConnector) -> None:
    connector.client.get_authorization = AsyncMock(return_value=SAMPLE_AUTH_RESPONSE)
    await connector.install()
    assert connector.config.get("account_id") == str(SAMPLE_BC_ACCOUNT["id"])


@pytest.mark.asyncio
async def test_install_no_accounts_still_succeeds(connector: BasecampConnector) -> None:
    auth_no_accounts = {**SAMPLE_AUTH_RESPONSE, "accounts": []}
    connector.client.get_authorization = AsyncMock(return_value=auth_no_accounts)
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY


# ═══════════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(connector: BasecampConnector) -> None:
    connector.client.get_authorization = AsyncMock(return_value=SAMPLE_AUTH_RESPONSE)
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_missing_token() -> None:
    c = make_connector(access_token="")
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_invalid_token(connector: BasecampConnector) -> None:
    connector.client.get_authorization = AsyncMock(
        side_effect=BasecampAuthError("Token revoked")
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: BasecampConnector) -> None:
    connector.client.get_authorization = AsyncMock(
        side_effect=BasecampNetworkError("timeout")
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_exception(connector: BasecampConnector) -> None:
    connector.client.get_authorization = AsyncMock(side_effect=Exception("boom"))
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty_projects(connector: BasecampConnector) -> None:
    connector.client.get_projects = AsyncMock(return_value=[])
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0


@pytest.mark.asyncio
async def test_sync_one_project_no_resources(connector: BasecampConnector) -> None:
    connector.client.get_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
    connector.client.get_todo_lists = AsyncMock(return_value=[])
    connector.client.get_messages = AsyncMock(return_value=[])
    connector.client.get_documents = AsyncMock(return_value=[])
    result = await connector.sync()
    assert result.documents_found == 1  # the project itself
    assert result.documents_synced == 1
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_project_with_todos(connector: BasecampConnector) -> None:
    connector.client.get_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
    connector.client.get_todo_lists = AsyncMock(return_value=[SAMPLE_TODOLIST])
    connector.client.get_todos = AsyncMock(return_value=[SAMPLE_TODO, SAMPLE_TODO_COMPLETED])
    connector.client.get_messages = AsyncMock(return_value=[])
    connector.client.get_documents = AsyncMock(return_value=[])
    result = await connector.sync()
    assert result.documents_found == 3  # 1 project + 2 todos
    assert result.documents_synced == 3
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_project_with_messages(connector: BasecampConnector) -> None:
    connector.client.get_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
    connector.client.get_todo_lists = AsyncMock(return_value=[])
    connector.client.get_messages = AsyncMock(return_value=[SAMPLE_MESSAGE])
    connector.client.get_documents = AsyncMock(return_value=[])
    result = await connector.sync()
    assert result.documents_found == 2  # 1 project + 1 message


@pytest.mark.asyncio
async def test_sync_project_with_documents(connector: BasecampConnector) -> None:
    connector.client.get_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
    connector.client.get_todo_lists = AsyncMock(return_value=[])
    connector.client.get_messages = AsyncMock(return_value=[])
    connector.client.get_documents = AsyncMock(return_value=[SAMPLE_DOCUMENT])
    result = await connector.sync()
    assert result.documents_found == 2  # 1 project + 1 document


@pytest.mark.asyncio
async def test_sync_project_list_failure_returns_failed(connector: BasecampConnector) -> None:
    connector.client.get_projects = AsyncMock(
        side_effect=BasecampNetworkError("Server error", 500)
    )
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "projects" in result.message.lower()


@pytest.mark.asyncio
async def test_sync_ingest_called_with_kb_id(connector: BasecampConnector) -> None:
    connector.client.get_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
    connector.client.get_todo_lists = AsyncMock(return_value=[SAMPLE_TODOLIST])
    connector.client.get_todos = AsyncMock(return_value=[SAMPLE_TODO])
    connector.client.get_messages = AsyncMock(return_value=[SAMPLE_MESSAGE])
    connector.client.get_documents = AsyncMock(return_value=[])
    connector._ingest_document = AsyncMock()  # type: ignore[method-assign]
    await connector.sync(kb_id="kb_basecamp_001")
    # 1 project + 1 todo + 1 message = 3 ingest calls
    assert connector._ingest_document.call_count == 3


@pytest.mark.asyncio
async def test_sync_normalizer_failure_counts_failed(connector: BasecampConnector) -> None:
    connector.client.get_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
    connector.client.get_todo_lists = AsyncMock(return_value=[])
    connector.client.get_messages = AsyncMock(return_value=[])
    connector.client.get_documents = AsyncMock(return_value=[])
    with patch("connector.normalize_project", side_effect=Exception("normalizer failed")):
        result = await connector.sync()
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════════════
# list_*() connector methods
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_projects_delegates_to_client(connector: BasecampConnector) -> None:
    connector.client.get_projects = AsyncMock(return_value=[SAMPLE_PROJECT, SAMPLE_PROJECT_2])
    result = await connector.list_projects()
    assert len(result) == 2
    assert result[0]["id"] == 10001


@pytest.mark.asyncio
async def test_list_todo_lists_delegates_to_client(connector: BasecampConnector) -> None:
    connector.client.get_todo_lists = AsyncMock(return_value=[SAMPLE_TODOLIST])
    result = await connector.list_todo_lists(10001)
    assert result[0]["id"] == 20001
    connector.client.get_todo_lists.assert_called_once_with(10001)


@pytest.mark.asyncio
async def test_list_todos_delegates_to_client(connector: BasecampConnector) -> None:
    connector.client.get_todos = AsyncMock(return_value=[SAMPLE_TODO])
    result = await connector.list_todos(10001, 20001)
    assert result[0]["id"] == 30001
    connector.client.get_todos.assert_called_once_with(10001, 20001)


@pytest.mark.asyncio
async def test_list_messages_delegates_to_client(connector: BasecampConnector) -> None:
    connector.client.get_messages = AsyncMock(return_value=[SAMPLE_MESSAGE])
    result = await connector.list_messages(10001)
    assert result[0]["subject"] == "Kickoff meeting notes"


@pytest.mark.asyncio
async def test_list_documents_delegates_to_client(connector: BasecampConnector) -> None:
    connector.client.get_documents = AsyncMock(return_value=[SAMPLE_DOCUMENT])
    result = await connector.list_documents(10001)
    assert result[0]["title"] == "Brand Guidelines"


# ═══════════════════════════════════════════════════════════════════════════════
# authorize() URL generation
# ═══════════════════════════════════════════════════════════════════════════════


def test_authorize_contains_authorize_base_url() -> None:
    c = make_connector()
    url = c.authorize()
    assert "launchpad.37signals.com" in url
    assert "authorization/new" in url


def test_authorize_contains_client_id() -> None:
    c = make_connector()
    url = c.authorize()
    assert "test_client_id" in url


def test_authorize_contains_redirect_uri() -> None:
    c = make_connector()
    url = c.authorize()
    assert "shielva.ai" in url or "redirect_uri" in url


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level constants
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_type_constant() -> None:
    assert CONNECTOR_TYPE == "basecamp"


def test_auth_type_constant() -> None:
    assert AUTH_TYPE == "oauth2"


def test_connector_class_constants() -> None:
    assert BasecampConnector.CONNECTOR_TYPE == "basecamp"
    assert BasecampConnector.AUTH_TYPE == "oauth2"


# ═══════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_client_aclose(connector: BasecampConnector) -> None:
    connector.client.aclose = AsyncMock()
    await connector.aclose()
    connector.client.aclose.assert_called_once()


@pytest.mark.asyncio
async def test_context_manager_closes_on_exit() -> None:
    c = make_connector()
    c.client = MagicMock()
    c.client.aclose = AsyncMock()
    async with c:
        pass
    c.client.aclose.assert_called_once()
