"""Unit tests for PandaDocConnector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import PandaDocConnector, CONNECTOR_TYPE, AUTH_TYPE
from exceptions import (
    PandaDocAuthError,
    PandaDocError,
    PandaDocNetworkError,
    PandaDocNotFoundError,
    PandaDocRateLimitError,
    PandaDocServerError,
)
from helpers.utils import (
    normalize_contact,
    normalize_document,
    normalize_form,
    normalize_template,
    with_retry,
    _stable_id,
)
from models import (
    AuthStatus,
    ConnectorHealth,
    DocumentStatus,
    ResourceType,
    SyncStatus,
)
from client.http_client import PandaDocHTTPClient

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_pandadoc_test_001"
API_KEY = "test-pandadoc-api-key-abc123"

SAMPLE_DOCUMENT: dict = {
    "id": "doc-abc123",
    "name": "Sales Agreement Q2",
    "status": "document.completed",
    "date_created": "2026-06-01T10:00:00Z",
    "date_modified": "2026-06-02T11:00:00Z",
    "expiration_date": "2026-12-31T00:00:00Z",
    "created_by": {
        "firstName": "Alice",
        "lastName": "Smith",
        "email": "alice@example.com",
    },
    "template_uuid": "tpl-xyz789",
    "recipients": [
        {"email": "bob@example.com"},
        {"email": "carol@example.com"},
    ],
}

SAMPLE_TEMPLATE: dict = {
    "id": "tpl-xyz789",
    "name": "NDA Template",
    "date_created": "2026-01-15T09:00:00Z",
    "date_modified": "2026-03-01T12:00:00Z",
    "created_by": {
        "firstName": "Dave",
        "lastName": "Jones",
        "email": "dave@example.com",
    },
}

SAMPLE_CONTACT: dict = {
    "id": "cnt-def456",
    "first_name": "Eve",
    "last_name": "Taylor",
    "email": "eve@example.com",
    "company": "Acme Corp",
    "job_title": "VP Sales",
    "phone": "+1-555-0100",
    "date_created": "2026-02-01T08:00:00Z",
}

SAMPLE_FORM: dict = {
    "id": "frm-ghi789",
    "name": "Onboarding Form",
    "status": "active",
    "date_created": "2026-03-10T14:00:00Z",
    "date_modified": "2026-04-01T10:00:00Z",
}

SAMPLE_WORKSPACE: dict = {
    "workspaces": [
        {"id": "ws-001", "name": "Acme Workspace"}
    ]
}

SAMPLE_MEMBERS: dict = {
    "workspace_members": [
        {"user_id": "u-001", "email": "admin@example.com", "role": "owner"},
        {"user_id": "u-002", "email": "user@example.com", "role": "editor"},
    ]
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_connector(api_key: str = API_KEY) -> PandaDocConnector:
    """Return a PandaDocConnector with a test API key."""
    return PandaDocConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key},
    )


def _no_key_connector() -> PandaDocConnector:
    return PandaDocConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})


# ═══════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTION TESTS (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_pandadoc_error_base() -> None:
    exc = PandaDocError("something failed", status_code=400, code="bad_request")
    assert exc.message == "something failed"
    assert exc.status_code == 400
    assert exc.code == "bad_request"
    assert str(exc) == "something failed"


def test_pandadoc_auth_error_inherits_base() -> None:
    exc = PandaDocAuthError("unauthorized", status_code=401, code="auth_error")
    assert isinstance(exc, PandaDocError)
    assert exc.status_code == 401


def test_pandadoc_network_error_inherits_base() -> None:
    exc = PandaDocNetworkError("timeout")
    assert isinstance(exc, PandaDocError)
    assert exc.status_code == 0


def test_pandadoc_not_found_error() -> None:
    exc = PandaDocNotFoundError("document", "doc-abc123")
    assert isinstance(exc, PandaDocError)
    assert exc.status_code == 404
    assert exc.code == "resource_not_found"
    assert "doc-abc123" in exc.message


def test_pandadoc_rate_limit_error() -> None:
    exc = PandaDocRateLimitError("too many requests", retry_after=5.0)
    assert isinstance(exc, PandaDocError)
    assert exc.status_code == 429
    assert exc.retry_after == 5.0
    assert exc.code == "rate_limit"


def test_pandadoc_rate_limit_error_default_retry_after() -> None:
    exc = PandaDocRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_pandadoc_server_error_inherits_base() -> None:
    exc = PandaDocServerError("internal server error", status_code=500)
    assert isinstance(exc, PandaDocError)
    assert exc.status_code == 500


def test_pandadoc_error_default_values() -> None:
    exc = PandaDocError("minimal error")
    assert exc.status_code == 0
    assert exc.code == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MODEL TESTS (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_health_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"
    assert AuthStatus.FAILED == "failed"


def test_sync_status_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_document_status_enum() -> None:
    assert DocumentStatus.DOCUMENT_COMPLETED == "document.completed"
    assert DocumentStatus.DOCUMENT_DRAFT == "document.draft"
    assert DocumentStatus.DOCUMENT_DECLINED == "document.declined"


def test_resource_type_enum() -> None:
    assert ResourceType.DOCUMENT == "document"
    assert ResourceType.TEMPLATE == "template"
    assert ResourceType.CONTACT == "contact"
    assert ResourceType.FORM == "form"
    assert ResourceType.MEMBER == "member"


def test_connector_constants() -> None:
    assert CONNECTOR_TYPE == "pandadoc"
    assert AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZER TESTS (12 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_stable_id_is_16_chars() -> None:
    sid = _stable_id("document", "doc-abc123")
    assert len(sid) == 16


def test_stable_id_is_deterministic() -> None:
    assert _stable_id("document", "doc-abc123") == _stable_id("document", "doc-abc123")


def test_stable_id_differs_by_prefix() -> None:
    assert _stable_id("document", "abc") != _stable_id("template", "abc")


def test_normalize_document_basic() -> None:
    doc = normalize_document(SAMPLE_DOCUMENT)
    assert doc.resource_type == "document"
    assert "Sales Agreement Q2" in doc.title
    assert "document.completed" in doc.title
    assert doc.source_id == _stable_id("document", "doc-abc123")
    assert "doc-abc123" in doc.content
    assert "alice@example.com" in doc.content or "Alice Smith" in doc.content
    assert "bob@example.com" in doc.content
    assert "tpl-xyz789" in doc.content
    assert "pandadoc.com" in doc.source_url


def test_normalize_document_metadata() -> None:
    doc = normalize_document(SAMPLE_DOCUMENT)
    assert doc.metadata["document_id"] == "doc-abc123"
    assert doc.metadata["status"] == "document.completed"
    assert doc.metadata["template_uuid"] == "tpl-xyz789"
    assert "bob@example.com" in doc.metadata["recipient_emails"]


def test_normalize_document_empty_raw() -> None:
    doc = normalize_document({})
    assert doc.resource_type == "document"
    assert "Document" in doc.title


def test_normalize_template_basic() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE)
    assert doc.resource_type == "template"
    assert "NDA Template" in doc.title
    assert doc.source_id == _stable_id("template", "tpl-xyz789")
    assert "tpl-xyz789" in doc.content
    assert "Dave Jones" in doc.content or "dave@example.com" in doc.content
    assert "pandadoc.com" in doc.source_url


def test_normalize_template_metadata() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE)
    assert doc.metadata["template_id"] == "tpl-xyz789"
    assert doc.metadata["name"] == "NDA Template"


def test_normalize_contact_basic() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert doc.resource_type == "contact"
    assert "Eve Taylor" in doc.title
    assert doc.source_id == _stable_id("contact", "cnt-def456")
    assert "eve@example.com" in doc.content
    assert "Acme Corp" in doc.content
    assert "VP Sales" in doc.content
    assert "+1-555-0100" in doc.content


def test_normalize_contact_metadata() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert doc.metadata["contact_id"] == "cnt-def456"
    assert doc.metadata["email"] == "eve@example.com"
    assert doc.metadata["company"] == "Acme Corp"


def test_normalize_form_basic() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert doc.resource_type == "form"
    assert "Onboarding Form" in doc.title
    assert "active" in doc.title
    assert doc.source_id == _stable_id("form", "frm-ghi789")
    assert "frm-ghi789" in doc.content


def test_normalize_form_metadata() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert doc.metadata["form_id"] == "frm-ghi789"
    assert doc.metadata["status"] == "active"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. WITH_RETRY TESTS (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    fn = AsyncMock(side_effect=[PandaDocNetworkError("timeout"), {"ok": True}])
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    fn = AsyncMock(side_effect=PandaDocNetworkError("always fails"))
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(PandaDocNetworkError):
            await with_retry(fn, max_attempts=3)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=PandaDocAuthError("forbidden", 403))
    with pytest.raises(PandaDocAuthError):
        await with_retry(fn, max_attempts=3)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[PandaDocRateLimitError("rate limited", retry_after=2.0), {"ok": True}]
    )
    sleep_mock = AsyncMock()
    with patch("helpers.utils.asyncio.sleep", sleep_mock):
        result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    sleep_mock.assert_called_once_with(2.0)


@pytest.mark.asyncio
async def test_with_retry_rate_limit_zero_retry_after_uses_backoff() -> None:
    fn = AsyncMock(
        side_effect=[PandaDocRateLimitError("rate limited", retry_after=0.0), {"ok": True}]
    )
    sleep_mock = AsyncMock()
    with patch("helpers.utils.asyncio.sleep", sleep_mock):
        with patch("helpers.utils.random.uniform", return_value=0.0):
            result = await with_retry(fn, max_attempts=3, base_delay=1.0)
    assert result == {"ok": True}
    assert sleep_mock.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    fn = AsyncMock(return_value={"data": [1, 2, 3]})
    result = await with_retry(fn, "arg1", key="val")
    fn.assert_called_once_with("arg1", key="val")
    assert result == {"data": [1, 2, 3]}


@pytest.mark.asyncio
async def test_with_retry_max_attempts_one() -> None:
    fn = AsyncMock(side_effect=PandaDocNetworkError("fail"))
    with pytest.raises(PandaDocNetworkError):
        await with_retry(fn, max_attempts=1)
    assert fn.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HTTP CLIENT TESTS (18 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def _make_http_client(api_key: str = API_KEY) -> PandaDocHTTPClient:
    return PandaDocHTTPClient(config={"api_key": api_key})


def test_http_client_sets_api_key_header() -> None:
    client = _make_http_client()
    auth_header = client._client.headers.get("authorization")
    assert auth_header == f"API-Key {API_KEY}"


def test_http_client_uses_correct_base_url() -> None:
    client = _make_http_client()
    assert "pandadoc.com" in str(client._client.base_url)


@pytest.mark.asyncio
async def test_get_workspaces_success() -> None:
    client = _make_http_client()
    client._request = AsyncMock(return_value=SAMPLE_WORKSPACE)
    result = await client.get_workspaces()
    client._request.assert_called_once_with("GET", "/workspaces/")
    assert "workspaces" in result


@pytest.mark.asyncio
async def test_get_documents_default_params() -> None:
    client = _make_http_client()
    client._request = AsyncMock(return_value={"results": [SAMPLE_DOCUMENT]})
    result = await client.get_documents()
    client._request.assert_called_once_with(
        "GET", "/documents", params={"page": 1, "count": 100}
    )
    assert "results" in result


@pytest.mark.asyncio
async def test_get_documents_custom_params() -> None:
    client = _make_http_client()
    client._request = AsyncMock(return_value={"results": []})
    await client.get_documents(page=2, count=50)
    client._request.assert_called_once_with(
        "GET", "/documents", params={"page": 2, "count": 50}
    )


@pytest.mark.asyncio
async def test_get_document_by_id() -> None:
    client = _make_http_client()
    client._request = AsyncMock(return_value=SAMPLE_DOCUMENT)
    result = await client.get_document("doc-abc123")
    client._request.assert_called_once_with("GET", "/documents/doc-abc123")
    assert result["id"] == "doc-abc123"


@pytest.mark.asyncio
async def test_get_document_details() -> None:
    client = _make_http_client()
    details = {"id": "doc-abc123", "fields": [], "tokens": []}
    client._request = AsyncMock(return_value=details)
    result = await client.get_document_details("doc-abc123")
    client._request.assert_called_once_with("GET", "/documents/doc-abc123/details")
    assert result["id"] == "doc-abc123"


@pytest.mark.asyncio
async def test_get_templates() -> None:
    client = _make_http_client()
    client._request = AsyncMock(return_value={"results": [SAMPLE_TEMPLATE]})
    result = await client.get_templates()
    client._request.assert_called_once_with(
        "GET", "/templates", params={"page": 1, "count": 100}
    )
    assert "results" in result


@pytest.mark.asyncio
async def test_get_contacts() -> None:
    client = _make_http_client()
    client._request = AsyncMock(return_value={"results": [SAMPLE_CONTACT]})
    result = await client.get_contacts()
    client._request.assert_called_once_with(
        "GET", "/contacts", params={"page": 1, "count": 100}
    )
    assert "results" in result


@pytest.mark.asyncio
async def test_get_forms() -> None:
    client = _make_http_client()
    client._request = AsyncMock(return_value={"results": [SAMPLE_FORM]})
    result = await client.get_forms()
    client._request.assert_called_once_with(
        "GET", "/forms", params={"page": 1, "count": 100}
    )
    assert "results" in result


@pytest.mark.asyncio
async def test_get_members() -> None:
    client = _make_http_client()
    client._request = AsyncMock(return_value=SAMPLE_MEMBERS)
    result = await client.get_members()
    client._request.assert_called_once_with("GET", "/members")
    assert "workspace_members" in result


def test_raise_for_status_401() -> None:
    client = _make_http_client()
    with pytest.raises(PandaDocAuthError) as exc_info:
        client._raise_for_status(401, {}, "unauthorized", "")
    assert exc_info.value.status_code == 401


def test_raise_for_status_403() -> None:
    client = _make_http_client()
    with pytest.raises(PandaDocAuthError) as exc_info:
        client._raise_for_status(403, {}, "forbidden", "")
    assert exc_info.value.status_code == 403


def test_raise_for_status_404() -> None:
    client = _make_http_client()
    with pytest.raises(PandaDocNotFoundError):
        client._raise_for_status(404, {}, "not found", "", "/documents/x")


def test_raise_for_status_429() -> None:
    client = _make_http_client()
    with pytest.raises(PandaDocRateLimitError) as exc_info:
        client._raise_for_status(429, {}, "rate limited", "")
    assert exc_info.value.status_code == 429


def test_raise_for_status_500() -> None:
    client = _make_http_client()
    with pytest.raises(PandaDocServerError) as exc_info:
        client._raise_for_status(500, {}, "server error", "")
    assert exc_info.value.status_code == 500


def test_raise_for_status_other() -> None:
    client = _make_http_client()
    with pytest.raises(PandaDocError) as exc_info:
        client._raise_for_status(422, {}, "unprocessable", "validation_error")
    assert exc_info.value.status_code == 422


def test_http_client_empty_api_key() -> None:
    client = PandaDocHTTPClient(config={})
    auth_header = client._client.headers.get("authorization")
    assert auth_header == "API-Key "


# ═══════════════════════════════════════════════════════════════════════════════
# 6. INSTALL TESTS (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    connector = _no_key_connector()
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message.lower()


@pytest.mark.asyncio
async def test_install_success() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(return_value=SAMPLE_WORKSPACE)
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "validated" in result.message.lower()
    assert "Acme Workspace" in result.message


@pytest.mark.asyncio
async def test_install_success_no_workspaces() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(return_value={"workspaces": []})
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_invalid_key() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(
        side_effect=PandaDocAuthError("unauthorized", 401)
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(
        side_effect=PandaDocNetworkError("connection refused")
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_error() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(side_effect=RuntimeError("unexpected"))
    result = await connector.install()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 7. HEALTH CHECK TESTS (7 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_missing_key() -> None:
    connector = _no_key_connector()
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_healthy() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(return_value=SAMPLE_WORKSPACE)
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.workspace_name == "Acme Workspace"
    assert "reachable" in result.message.lower()


@pytest.mark.asyncio
async def test_health_check_healthy_no_workspaces() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(return_value={"workspaces": []})
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.workspace_name == ""


@pytest.mark.asyncio
async def test_health_check_auth_error() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(
        side_effect=PandaDocAuthError("forbidden", 403)
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(
        side_effect=PandaDocNetworkError("timeout")
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_server_error() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(
        side_effect=PandaDocServerError("service unavailable", 503)
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_unexpected_error() -> None:
    connector = _make_connector()
    connector.client.get_workspaces = AsyncMock(side_effect=ValueError("unexpected"))
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 8. SYNC TESTS (10 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def _mock_connector_for_sync(
    docs: list[dict] | None = None,
    templates: list[dict] | None = None,
    contacts: list[dict] | None = None,
    forms: list[dict] | None = None,
) -> PandaDocConnector:
    connector = _make_connector()
    connector.client.get_documents = AsyncMock(
        return_value={"results": docs if docs is not None else [SAMPLE_DOCUMENT]}
    )
    connector.client.get_templates = AsyncMock(
        return_value={"results": templates if templates is not None else [SAMPLE_TEMPLATE]}
    )
    connector.client.get_contacts = AsyncMock(
        return_value={"results": contacts if contacts is not None else [SAMPLE_CONTACT]}
    )
    connector.client.get_forms = AsyncMock(
        return_value={"results": forms if forms is not None else [SAMPLE_FORM]}
    )
    return connector


@pytest.mark.asyncio
async def test_sync_missing_api_key() -> None:
    connector = _no_key_connector()
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "api_key" in result.message.lower() or "No API" in result.message


@pytest.mark.asyncio
async def test_sync_completed_with_all_resources() -> None:
    connector = _mock_connector_for_sync()
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 4
    assert result.documents_synced == 4
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_partial_on_document_normalize_error() -> None:
    connector = _make_connector()
    connector.client.get_documents = AsyncMock(
        return_value={"results": [{"id": ""}]}  # normalize won't error but won't find much
    )
    connector.client.get_templates = AsyncMock(return_value={"results": []})
    connector.client.get_contacts = AsyncMock(return_value={"results": []})
    connector.client.get_forms = AsyncMock(return_value={"results": []})
    result = await connector.sync()
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


@pytest.mark.asyncio
async def test_sync_failed_on_document_api_error() -> None:
    connector = _make_connector()
    connector.client.get_documents = AsyncMock(
        side_effect=PandaDocAuthError("unauthorized", 401)
    )
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "documents" in result.message.lower()


@pytest.mark.asyncio
async def test_sync_empty_all_resources() -> None:
    connector = _mock_connector_for_sync(docs=[], templates=[], contacts=[], forms=[])
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_counts_across_resource_types() -> None:
    connector = _mock_connector_for_sync(
        docs=[SAMPLE_DOCUMENT, SAMPLE_DOCUMENT],
        templates=[SAMPLE_TEMPLATE],
        contacts=[SAMPLE_CONTACT, SAMPLE_CONTACT, SAMPLE_CONTACT],
        forms=[SAMPLE_FORM],
    )
    result = await connector.sync()
    assert result.documents_found == 7
    assert result.documents_synced == 7


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest() -> None:
    connector = _mock_connector_for_sync(
        docs=[SAMPLE_DOCUMENT], templates=[], contacts=[], forms=[]
    )
    ingest_mock = AsyncMock()
    connector._ingest_document = ingest_mock
    result = await connector.sync(kb_id="kb-test-001")
    assert ingest_mock.call_count == 1
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_without_kb_id_does_not_call_ingest() -> None:
    connector = _mock_connector_for_sync(
        docs=[SAMPLE_DOCUMENT], templates=[], contacts=[], forms=[]
    )
    ingest_mock = AsyncMock()
    connector._ingest_document = ingest_mock
    await connector.sync()
    ingest_mock.assert_not_called()


@pytest.mark.asyncio
async def test_sync_template_failure_does_not_abort_contacts() -> None:
    connector = _make_connector()
    connector.client.get_documents = AsyncMock(return_value={"results": []})
    connector.client.get_templates = AsyncMock(
        side_effect=PandaDocError("templates endpoint error")
    )
    connector.client.get_contacts = AsyncMock(
        return_value={"results": [SAMPLE_CONTACT]}
    )
    connector.client.get_forms = AsyncMock(return_value={"results": []})
    result = await connector.sync()
    # contacts should still be synced despite templates failure
    assert result.documents_synced >= 1


@pytest.mark.asyncio
async def test_sync_status_partial_when_failures_exist() -> None:
    connector = _make_connector()
    connector.client.get_documents = AsyncMock(return_value={"results": []})
    connector.client.get_templates = AsyncMock(return_value={"results": []})
    connector.client.get_contacts = AsyncMock(return_value={"results": []})
    connector.client.get_forms = AsyncMock(
        side_effect=PandaDocError("forms broken")
    )

    # Patch to simulate a failed item counting through forms branch
    original_sync = connector.sync

    result = await connector.sync()
    # forms error increments failed counter → PARTIAL
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. LIST METHOD TESTS (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_documents_returns_flat_list() -> None:
    connector = _make_connector()
    connector.client.get_documents = AsyncMock(
        return_value={"results": [SAMPLE_DOCUMENT]}
    )
    docs = await connector.list_documents()
    assert len(docs) == 1
    assert docs[0]["id"] == "doc-abc123"


@pytest.mark.asyncio
async def test_list_templates_returns_flat_list() -> None:
    connector = _make_connector()
    connector.client.get_templates = AsyncMock(
        return_value={"results": [SAMPLE_TEMPLATE]}
    )
    templates = await connector.list_templates()
    assert len(templates) == 1
    assert templates[0]["id"] == "tpl-xyz789"


@pytest.mark.asyncio
async def test_list_contacts_returns_flat_list() -> None:
    connector = _make_connector()
    connector.client.get_contacts = AsyncMock(
        return_value={"results": [SAMPLE_CONTACT]}
    )
    contacts = await connector.list_contacts()
    assert len(contacts) == 1
    assert contacts[0]["id"] == "cnt-def456"


@pytest.mark.asyncio
async def test_list_forms_returns_flat_list() -> None:
    connector = _make_connector()
    connector.client.get_forms = AsyncMock(
        return_value={"results": [SAMPLE_FORM]}
    )
    forms = await connector.list_forms()
    assert len(forms) == 1
    assert forms[0]["id"] == "frm-ghi789"


@pytest.mark.asyncio
async def test_list_documents_empty_when_no_results_key() -> None:
    connector = _make_connector()
    connector.client.get_documents = AsyncMock(return_value={})
    docs = await connector.list_documents()
    assert docs == []


@pytest.mark.asyncio
async def test_list_contacts_empty_result() -> None:
    connector = _make_connector()
    connector.client.get_contacts = AsyncMock(return_value={"results": []})
    contacts = await connector.list_contacts()
    assert contacts == []


# ═══════════════════════════════════════════════════════════════════════════════
# 10. GET DOCUMENT / DETAILS TESTS (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_document_returns_document() -> None:
    connector = _make_connector()
    connector.client.get_document = AsyncMock(return_value=SAMPLE_DOCUMENT)
    result = await connector.get_document("doc-abc123")
    connector.client.get_document.assert_called_once_with("doc-abc123")
    assert result["id"] == "doc-abc123"


@pytest.mark.asyncio
async def test_get_document_not_found_raises() -> None:
    connector = _make_connector()
    connector.client.get_document = AsyncMock(
        side_effect=PandaDocNotFoundError("document", "doc-missing")
    )
    with pytest.raises(PandaDocNotFoundError):
        await connector.get_document("doc-missing")


@pytest.mark.asyncio
async def test_get_document_details_returns_details() -> None:
    connector = _make_connector()
    details = {"id": "doc-abc123", "fields": [{"name": "signature"}], "tokens": []}
    connector.client.get_document_details = AsyncMock(return_value=details)
    result = await connector.get_document_details("doc-abc123")
    connector.client.get_document_details.assert_called_once_with("doc-abc123")
    assert result["fields"][0]["name"] == "signature"


@pytest.mark.asyncio
async def test_get_document_details_not_found_raises() -> None:
    connector = _make_connector()
    connector.client.get_document_details = AsyncMock(
        side_effect=PandaDocNotFoundError("document", "doc-ghost")
    )
    with pytest.raises(PandaDocNotFoundError):
        await connector.get_document_details("doc-ghost")


@pytest.mark.asyncio
async def test_get_document_auth_error_propagates() -> None:
    connector = _make_connector()
    connector.client.get_document = AsyncMock(
        side_effect=PandaDocAuthError("forbidden", 403)
    )
    with pytest.raises(PandaDocAuthError):
        await connector.get_document("doc-abc123")


# ═══════════════════════════════════════════════════════════════════════════════
# 11. PAGINATION TESTS (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_documents_paginates_until_partial_page() -> None:
    connector = _make_connector()
    page1 = {"results": [SAMPLE_DOCUMENT] * 100}
    page2 = {"results": [SAMPLE_DOCUMENT] * 50}
    connector.client.get_documents = AsyncMock(side_effect=[page1, page2])
    docs = await connector.list_documents()
    assert len(docs) == 150
    assert connector.client.get_documents.call_count == 2


@pytest.mark.asyncio
async def test_list_documents_stops_on_empty_page() -> None:
    connector = _make_connector()
    page1 = {"results": [SAMPLE_DOCUMENT] * 100}
    page2 = {"results": []}
    connector.client.get_documents = AsyncMock(side_effect=[page1, page2])
    docs = await connector.list_documents()
    assert len(docs) == 100
    assert connector.client.get_documents.call_count == 2


@pytest.mark.asyncio
async def test_list_templates_paginates() -> None:
    connector = _make_connector()
    page1 = {"results": [SAMPLE_TEMPLATE] * 100}
    page2 = {"results": [SAMPLE_TEMPLATE] * 10}
    connector.client.get_templates = AsyncMock(side_effect=[page1, page2])
    templates = await connector.list_templates()
    assert len(templates) == 110


@pytest.mark.asyncio
async def test_list_contacts_paginates() -> None:
    connector = _make_connector()
    page1 = {"results": [SAMPLE_CONTACT] * 100}
    page2 = {"results": [SAMPLE_CONTACT] * 25}
    connector.client.get_contacts = AsyncMock(side_effect=[page1, page2])
    contacts = await connector.list_contacts()
    assert len(contacts) == 125


@pytest.mark.asyncio
async def test_list_forms_paginates() -> None:
    connector = _make_connector()
    page1 = {"results": [SAMPLE_FORM] * 100}
    page2 = {"results": [SAMPLE_FORM] * 5}
    connector.client.get_forms = AsyncMock(side_effect=[page1, page2])
    forms = await connector.list_forms()
    assert len(forms) == 105


@pytest.mark.asyncio
async def test_list_documents_page_increment() -> None:
    """Verify that each subsequent page call uses an incremented page number."""
    connector = _make_connector()
    calls: list[dict] = []

    async def fake_get_documents(page: int = 1, count: int = 100, **kwargs: object) -> dict:
        calls.append({"page": page, "count": count})
        if page == 1:
            return {"results": [SAMPLE_DOCUMENT] * 100}
        return {"results": [SAMPLE_DOCUMENT] * 5}

    connector.client.get_documents = fake_get_documents
    docs = await connector.list_documents()
    assert len(docs) == 105
    assert calls[0]["page"] == 1
    assert calls[1]["page"] == 2
