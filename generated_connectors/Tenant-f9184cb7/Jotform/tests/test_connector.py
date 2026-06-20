"""Unit tests for JotformConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import JotformConnector
from exceptions import (
    JotformAuthError,
    JotformError,
    JotformNetworkError,
    JotformNotFoundError,
    JotformRateLimitError,
)
from helpers.utils import (
    normalize_form,
    normalize_question,
    normalize_submission,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ─────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_jotform_test_001"
API_KEY = "jotform_test_api_key_abc123"

SAMPLE_USER: dict = {
    "username": "testuser",
    "email": "test@example.com",
    "name": "Test User",
    "accountType": "FREE",
    "status": "ACTIVE",
}

SAMPLE_FORM: dict = {
    "id": "123456789",
    "title": "Customer Feedback Form",
    "status": "ENABLED",
    "created_at": "2026-01-01 10:00:00",
    "updated_at": "2026-06-01 12:00:00",
    "url": "https://form.jotform.com/123456789",
    "count": "42",
}

SAMPLE_FORM_2: dict = {
    "id": "987654321",
    "title": "Employee Survey",
    "status": "ENABLED",
    "created_at": "2026-02-01 10:00:00",
    "updated_at": "2026-06-10 08:00:00",
    "url": "https://form.jotform.com/987654321",
    "count": "15",
}

SAMPLE_SUBMISSION: dict = {
    "id": "5001",
    "form_id": "123456789",
    "created_at": "2026-06-10 09:00:00",
    "updated_at": "2026-06-10 09:01:00",
    "status": "ACTIVE",
    "answers": {
        "1": {
            "name": "question1",
            "text": "How satisfied are you?",
            "type": "control_rating",
            "answer": "5",
        },
        "2": {
            "name": "question2",
            "text": "Comments",
            "type": "control_textarea",
            "answer": "Great service!",
        },
    },
}

SAMPLE_SUBMISSION_2: dict = {
    "id": "5002",
    "form_id": "123456789",
    "created_at": "2026-06-11 10:00:00",
    "updated_at": "2026-06-11 10:01:00",
    "status": "ACTIVE",
    "answers": {
        "1": {
            "name": "question1",
            "text": "How satisfied are you?",
            "type": "control_rating",
            "answer": "3",
        },
    },
}

SAMPLE_QUESTION: dict = {
    "qid": "1",
    "order": "1",
    "type": "control_rating",
    "text": "How satisfied are you?",
    "required": "Yes",
    "options": "1|2|3|4|5",
}

SAMPLE_QUESTION_2: dict = {
    "qid": "2",
    "order": "2",
    "type": "control_textarea",
    "text": "Comments",
    "required": "No",
}

SAMPLE_FORMS_PAGE: dict = {
    "items": [SAMPLE_FORM],
}

SAMPLE_SUBMISSIONS_PAGE: dict = {
    "items": [SAMPLE_SUBMISSION],
}

SAMPLE_QUESTIONS_PAGE: dict = {
    "items": [SAMPLE_QUESTION, SAMPLE_QUESTION_2],
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> JotformConnector:
    return JotformConnector(
        api_key=API_KEY,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: JotformConnector) -> JotformConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_base_fields() -> None:
    exc = JotformError("base error", status_code=500, code="server_error")
    assert str(exc) == "base error"
    assert exc.status_code == 500
    assert exc.code == "server_error"


def test_exception_auth_is_jotform_error() -> None:
    exc = JotformAuthError("bad key", 401)
    assert isinstance(exc, JotformError)
    assert exc.status_code == 401


def test_exception_rate_limit_is_jotform_error() -> None:
    exc = JotformRateLimitError("too fast")
    assert isinstance(exc, JotformError)
    assert exc.retry_after == 0.0
    assert exc.status_code == 429


def test_exception_rate_limit_stores_retry_after() -> None:
    exc = JotformRateLimitError("slow down", retry_after=60.0)
    assert exc.retry_after == 60.0


def test_exception_not_found_is_jotform_error() -> None:
    exc = JotformNotFoundError("form", "abc123")
    assert isinstance(exc, JotformError)
    assert exc.status_code == 404
    assert "abc123" in str(exc)
    assert "form" in str(exc)


def test_exception_not_found_message_format() -> None:
    exc = JotformNotFoundError("submission", "9999")
    assert "9999" in str(exc)
    assert exc.code == "resource_missing"


def test_exception_network_is_jotform_error() -> None:
    exc = JotformNetworkError("timeout", 503)
    assert isinstance(exc, JotformError)
    assert exc.status_code == 503


def test_exception_auth_not_subclass_of_network() -> None:
    exc = JotformAuthError("bad", 401)
    assert not isinstance(exc, JotformNetworkError)


def test_exception_network_not_subclass_of_auth() -> None:
    exc = JotformNetworkError("net fail")
    assert not isinstance(exc, JotformAuthError)


# ── Models ────────────────────────────────────────────────────────────────────


def test_model_connector_health_values() -> None:
    from models import ConnectorHealth
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_model_auth_status_values() -> None:
    from models import AuthStatus
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.FAILED == "failed"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_model_sync_status_values() -> None:
    from models import SyncStatus
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"


def test_model_install_result_defaults() -> None:
    from models import InstallResult, ConnectorHealth, AuthStatus
    r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
    assert r.connector_id == ""
    assert r.message == ""


def test_model_health_check_result_defaults() -> None:
    from models import HealthCheckResult, ConnectorHealth, AuthStatus
    r = HealthCheckResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
    assert r.username == ""
    assert r.email == ""
    assert r.message == ""


def test_model_sync_result_defaults() -> None:
    from models import SyncResult, SyncStatus
    r = SyncResult(status=SyncStatus.COMPLETED)
    assert r.documents_found == 0
    assert r.documents_synced == 0
    assert r.documents_failed == 0
    assert r.message == ""


def test_model_connector_document_metadata_default() -> None:
    from models import ConnectorDocument
    doc = ConnectorDocument(
        source_id="abc",
        title="T",
        content="C",
        connector_id="conn",
        tenant_id="t",
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ── normalize_form() ──────────────────────────────────────────────────────────


def test_normalize_form_source_id_is_16_chars() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert len(doc.source_id) == 16


def test_normalize_form_source_id_is_hex() -> None:
    doc = normalize_form(SAMPLE_FORM)
    int(doc.source_id, 16)  # raises ValueError if not valid hex


def test_normalize_form_source_id_is_deterministic() -> None:
    doc1 = normalize_form(SAMPLE_FORM)
    doc2 = normalize_form(SAMPLE_FORM)
    assert doc1.source_id == doc2.source_id


def test_normalize_form_different_ids_produce_different_source_ids() -> None:
    doc1 = normalize_form(SAMPLE_FORM)
    doc2 = normalize_form(SAMPLE_FORM_2)
    assert doc1.source_id != doc2.source_id


def test_normalize_form_title() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert doc.title == "Customer Feedback Form"


def test_normalize_form_fallback_title_when_empty() -> None:
    form_no_title = {**SAMPLE_FORM, "title": ""}
    doc = normalize_form(form_no_title)
    assert "123456789" in doc.title


def test_normalize_form_metadata_type() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert doc.metadata["type"] == "form"


def test_normalize_form_metadata_form_id() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert doc.metadata["form_id"] == "123456789"


def test_normalize_form_metadata_status() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert doc.metadata["status"] == "ENABLED"


def test_normalize_form_content_includes_title() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert "Customer Feedback Form" in doc.content


def test_normalize_form_source_url() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert "jotform.com" in doc.source_url


def test_normalize_form_source_url_fallback_when_no_url() -> None:
    form_no_url = {**SAMPLE_FORM, "url": ""}
    doc = normalize_form(form_no_url)
    assert "123456789" in doc.source_url


# ── normalize_submission() ────────────────────────────────────────────────────


def test_normalize_submission_source_id_is_16_chars() -> None:
    doc = normalize_submission(SAMPLE_SUBMISSION)
    assert len(doc.source_id) == 16


def test_normalize_submission_source_id_is_hex() -> None:
    doc = normalize_submission(SAMPLE_SUBMISSION)
    int(doc.source_id, 16)


def test_normalize_submission_source_id_is_deterministic() -> None:
    doc1 = normalize_submission(SAMPLE_SUBMISSION)
    doc2 = normalize_submission(SAMPLE_SUBMISSION)
    assert doc1.source_id == doc2.source_id


def test_normalize_submission_different_ids_produce_different_source_ids() -> None:
    doc1 = normalize_submission(SAMPLE_SUBMISSION)
    doc2 = normalize_submission(SAMPLE_SUBMISSION_2)
    assert doc1.source_id != doc2.source_id


def test_normalize_submission_metadata_type() -> None:
    doc = normalize_submission(SAMPLE_SUBMISSION)
    assert doc.metadata["type"] == "submission"


def test_normalize_submission_metadata_submission_id() -> None:
    doc = normalize_submission(SAMPLE_SUBMISSION)
    assert doc.metadata["submission_id"] == "5001"


def test_normalize_submission_metadata_form_id() -> None:
    doc = normalize_submission(SAMPLE_SUBMISSION)
    assert doc.metadata["form_id"] == "123456789"


def test_normalize_submission_content_includes_answers() -> None:
    doc = normalize_submission(SAMPLE_SUBMISSION)
    assert "5" in doc.content or "Great service!" in doc.content


def test_normalize_submission_title_includes_id() -> None:
    doc = normalize_submission(SAMPLE_SUBMISSION)
    assert "5001" in doc.title


def test_normalize_submission_empty_answers() -> None:
    sub_empty = {**SAMPLE_SUBMISSION, "answers": {}}
    doc = normalize_submission(sub_empty)
    assert doc.source_id is not None
    assert "5001" in doc.content or "5001" in doc.title


def test_normalize_submission_source_url_includes_form_id() -> None:
    doc = normalize_submission(SAMPLE_SUBMISSION)
    assert "123456789" in doc.source_url


# ── normalize_question() ──────────────────────────────────────────────────────


def test_normalize_question_source_id_is_16_chars() -> None:
    doc = normalize_question(SAMPLE_QUESTION, "123456789")
    assert len(doc.source_id) == 16


def test_normalize_question_source_id_is_hex() -> None:
    doc = normalize_question(SAMPLE_QUESTION, "123456789")
    int(doc.source_id, 16)


def test_normalize_question_source_id_is_deterministic() -> None:
    doc1 = normalize_question(SAMPLE_QUESTION, "123456789")
    doc2 = normalize_question(SAMPLE_QUESTION, "123456789")
    assert doc1.source_id == doc2.source_id


def test_normalize_question_different_qids_produce_different_source_ids() -> None:
    doc1 = normalize_question(SAMPLE_QUESTION, "123456789")
    doc2 = normalize_question(SAMPLE_QUESTION_2, "123456789")
    assert doc1.source_id != doc2.source_id


def test_normalize_question_different_form_ids_produce_different_source_ids() -> None:
    doc1 = normalize_question(SAMPLE_QUESTION, "123456789")
    doc2 = normalize_question(SAMPLE_QUESTION, "987654321")
    assert doc1.source_id != doc2.source_id


def test_normalize_question_metadata_type() -> None:
    doc = normalize_question(SAMPLE_QUESTION, "123456789")
    assert doc.metadata["type"] == "question"


def test_normalize_question_metadata_qid() -> None:
    doc = normalize_question(SAMPLE_QUESTION, "123456789")
    assert doc.metadata["qid"] == "1"


def test_normalize_question_metadata_form_id() -> None:
    doc = normalize_question(SAMPLE_QUESTION, "123456789")
    assert doc.metadata["form_id"] == "123456789"


def test_normalize_question_metadata_q_type() -> None:
    doc = normalize_question(SAMPLE_QUESTION, "123456789")
    assert doc.metadata["q_type"] == "control_rating"


def test_normalize_question_title_is_text() -> None:
    doc = normalize_question(SAMPLE_QUESTION, "123456789")
    assert doc.title == "How satisfied are you?"


def test_normalize_question_content_includes_type() -> None:
    doc = normalize_question(SAMPLE_QUESTION, "123456789")
    assert "control_rating" in doc.content


def test_normalize_question_content_includes_options() -> None:
    doc = normalize_question(SAMPLE_QUESTION, "123456789")
    assert "1|2|3|4|5" in doc.content


def test_normalize_question_source_url_includes_form_id() -> None:
    doc = normalize_question(SAMPLE_QUESTION, "123456789")
    assert "123456789" in doc.source_url


# ── with_retry() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    results = [JotformNetworkError("fail"), JotformNetworkError("fail"), {"ok": True}]
    call_no = {"n": 0}

    async def fn_impl(*args, **kwargs):
        r = results[call_no["n"]]
        call_no["n"] += 1
        if isinstance(r, Exception):
            raise r
        return r

    result = await with_retry(fn_impl, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert call_no["n"] == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=JotformAuthError("invalid key", 401))
    with pytest.raises(JotformAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=JotformNetworkError("persistent failure"))
    with pytest.raises(JotformNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=JotformRateLimitError("429", retry_after=0))
    with pytest.raises(JotformRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    mock_fn = AsyncMock(return_value={"data": True})
    await with_retry(mock_fn, "arg1", kwarg1="val1")
    mock_fn.assert_called_once_with("arg1", kwarg1="val1")


# ── HTTP client ───────────────────────────────────────────────────────────────


def test_http_client_base_url() -> None:
    from client.http_client import JOTFORM_BASE_URL
    assert JOTFORM_BASE_URL == "https://api.jotform.com"


def test_http_client_default_timeout() -> None:
    from client.http_client import JotformHTTPClient, DEFAULT_TIMEOUT_S
    client = JotformHTTPClient()
    assert client._timeout.total == DEFAULT_TIMEOUT_S


def test_http_client_custom_timeout() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient(timeout=60.0)
    assert client._timeout.total == 60.0


def test_http_client_make_params_includes_api_key() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient()
    params = client._make_params("my_api_key_123")
    assert params["apiKey"] == "my_api_key_123"


def test_http_client_make_params_merges_extra() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient()
    params = client._make_params("key", extra={"offset": 10, "limit": 50})
    assert params["apiKey"] == "key"
    assert params["offset"] == 10
    assert params["limit"] == 50


def test_http_client_raise_for_status_200_returns_content() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient()
    body = {"responseCode": 200, "content": {"username": "testuser"}}
    result = client._raise_for_status(200, body)
    assert result == {"username": "testuser"}


def test_http_client_raise_for_status_200_list_content_wraps() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient()
    body = {"responseCode": 200, "content": [{"id": "1"}, {"id": "2"}]}
    result = client._raise_for_status(200, body)
    assert "items" in result
    assert len(result["items"]) == 2


def test_http_client_raise_for_status_401_raises_auth_error() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient()
    body = {"responseCode": 401, "message": "Invalid API Key"}
    with pytest.raises(JotformAuthError) as exc_info:
        client._raise_for_status(401, body)
    assert exc_info.value.status_code == 401


def test_http_client_raise_for_status_403_raises_auth_error() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient()
    body = {"responseCode": 403, "message": "Forbidden"}
    with pytest.raises(JotformAuthError) as exc_info:
        client._raise_for_status(403, body)
    assert exc_info.value.status_code == 403


def test_http_client_raise_for_status_404_raises_not_found() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient()
    body = {"responseCode": 404, "message": "Form not found"}
    with pytest.raises(JotformNotFoundError):
        client._raise_for_status(404, body)


def test_http_client_raise_for_status_429_raises_rate_limit() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient()
    body = {"responseCode": 429, "message": "Too many requests"}
    with pytest.raises(JotformRateLimitError):
        client._raise_for_status(429, body)


def test_http_client_raise_for_status_500_raises_network_error() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient()
    body = {"responseCode": 500, "message": "Server error"}
    with pytest.raises(JotformNetworkError) as exc_info:
        client._raise_for_status(500, body)
    assert exc_info.value.status_code == 500


def test_http_client_raise_for_status_other_raises_jotform_error() -> None:
    from client.http_client import JotformHTTPClient
    client = JotformHTTPClient()
    body = {"responseCode": 400, "message": "Bad request"}
    with pytest.raises(JotformError):
        client._raise_for_status(400, body)


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success(connector: JotformConnector) -> None:
    with patch("connector.JotformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(return_value=SAMPLE_USER)
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "testuser" in result.message


@pytest.mark.asyncio
async def test_install_success_shows_email_when_no_username(connector: JotformConnector) -> None:
    user_no_username = {"username": "", "email": "admin@company.com"}
    with patch("connector.JotformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(return_value=user_no_username)
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert "admin@company.com" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = JotformConnector(tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector: JotformConnector) -> None:
    with patch("connector.JotformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(
            side_effect=JotformAuthError("Invalid API Key", 401)
        )
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid API Key" in result.message


@pytest.mark.asyncio
async def test_install_network_error(connector: JotformConnector) -> None:
    with patch("connector.JotformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(
            side_effect=JotformNetworkError("Connection refused")
        )
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_error(connector: JotformConnector) -> None:
    with patch("connector.JotformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(side_effect=RuntimeError("unexpected"))
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_connector_id(connector: JotformConnector) -> None:
    with patch("connector.JotformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(return_value=SAMPLE_USER)
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.connector_id == CONNECTOR_ID


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: JotformConnector) -> None:
    with patch("connector.JotformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(return_value=SAMPLE_USER)
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "testuser" in result.message
    assert result.username == "testuser"
    assert result.email == "test@example.com"


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: JotformConnector) -> None:
    with patch("connector.JotformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(
            side_effect=JotformAuthError("Forbidden", 403)
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: JotformConnector) -> None:
    with patch("connector.JotformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(
            side_effect=JotformNetworkError("Timeout")
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = JotformConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: JotformConnector) -> None:
    with patch("connector.JotformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(side_effect=RuntimeError("boom"))
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty_forms(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_forms = AsyncMock(return_value={"items": []})
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_single_form_single_submission(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_forms = AsyncMock(return_value={"items": [SAMPLE_FORM]})
    c._http_client.get_form_submissions = AsyncMock(
        return_value={"items": [SAMPLE_SUBMISSION]}
    )
    result = await c.sync(kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    # 1 form doc + 1 submission doc
    assert result.documents_synced >= 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_returns_sync_result(connector_with_mock_client: JotformConnector) -> None:
    from models import SyncResult as SR
    c = connector_with_mock_client
    c._http_client.get_forms = AsyncMock(return_value={"items": []})
    result = await c.sync()
    assert isinstance(result, SR)


@pytest.mark.asyncio
async def test_sync_forms_api_error_returns_failed(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_forms = AsyncMock(
        side_effect=JotformError("server error", 500)
    )
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_submissions_api_error_counts_failure(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_forms = AsyncMock(return_value={"items": [SAMPLE_FORM]})
    c._http_client.get_form_submissions = AsyncMock(
        side_effect=JotformError("server error", 500)
    )
    result = await c.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1


@pytest.mark.asyncio
async def test_sync_multiple_forms(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_forms = AsyncMock(
        return_value={"items": [SAMPLE_FORM, SAMPLE_FORM_2]}
    )
    c._http_client.get_form_submissions = AsyncMock(
        return_value={"items": [SAMPLE_SUBMISSION]}
    )
    result = await c.sync()
    assert result.documents_synced >= 2  # At least 2 forms synced


@pytest.mark.asyncio
async def test_sync_forms_pagination(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    # First page full (100 items), second page partial
    page1_forms = [{"id": str(i), "title": f"Form {i}", "status": "ENABLED", "url": ""} for i in range(100)]
    page2_forms = [SAMPLE_FORM_2]
    c._http_client.get_forms = AsyncMock(side_effect=[
        {"items": page1_forms},
        {"items": page2_forms},
    ])
    c._http_client.get_form_submissions = AsyncMock(return_value={"items": []})
    result = await c.sync()
    assert c._http_client.get_forms.call_count == 2
    assert result.documents_synced >= 100


# ── list_forms() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_forms_returns_page(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_forms = AsyncMock(return_value={"items": [SAMPLE_FORM]})
    result = await c.list_forms()
    assert "items" in result
    assert result["items"][0]["id"] == "123456789"


@pytest.mark.asyncio
async def test_list_forms_default_pagination(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_forms = AsyncMock(return_value=SAMPLE_FORMS_PAGE)
    await c.list_forms()
    call_kwargs = c._http_client.get_forms.call_args
    assert call_kwargs.kwargs.get("offset") == 0
    assert call_kwargs.kwargs.get("limit") == 100


@pytest.mark.asyncio
async def test_list_forms_custom_pagination(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_forms = AsyncMock(return_value=SAMPLE_FORMS_PAGE)
    await c.list_forms(offset=50, limit=25)
    call_kwargs = c._http_client.get_forms.call_args
    assert call_kwargs.kwargs.get("offset") == 50
    assert call_kwargs.kwargs.get("limit") == 25


@pytest.mark.asyncio
async def test_list_forms_empty_result(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_forms = AsyncMock(return_value={"items": []})
    result = await c.list_forms()
    assert result["items"] == []


# ── list_submissions() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_submissions_returns_page(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_form_submissions = AsyncMock(return_value=SAMPLE_SUBMISSIONS_PAGE)
    result = await c.list_submissions("123456789")
    assert "items" in result
    assert result["items"][0]["id"] == "5001"


@pytest.mark.asyncio
async def test_list_submissions_default_pagination(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_form_submissions = AsyncMock(return_value=SAMPLE_SUBMISSIONS_PAGE)
    await c.list_submissions("123456789")
    call_kwargs = c._http_client.get_form_submissions.call_args
    assert call_kwargs.kwargs.get("offset") == 0
    assert call_kwargs.kwargs.get("limit") == 100


@pytest.mark.asyncio
async def test_list_submissions_passes_form_id(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_form_submissions = AsyncMock(return_value=SAMPLE_SUBMISSIONS_PAGE)
    await c.list_submissions("123456789")
    call_args = c._http_client.get_form_submissions.call_args
    assert "123456789" in call_args.args


@pytest.mark.asyncio
async def test_list_submissions_empty(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_form_submissions = AsyncMock(return_value={"items": []})
    result = await c.list_submissions("123456789")
    assert result["items"] == []


# ── list_all_submissions() ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_all_submissions_returns_page(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_user_submissions = AsyncMock(return_value=SAMPLE_SUBMISSIONS_PAGE)
    result = await c.list_all_submissions()
    assert "items" in result
    assert result["items"][0]["id"] == "5001"


@pytest.mark.asyncio
async def test_list_all_submissions_default_pagination(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_user_submissions = AsyncMock(return_value=SAMPLE_SUBMISSIONS_PAGE)
    await c.list_all_submissions()
    call_kwargs = c._http_client.get_user_submissions.call_args
    assert call_kwargs.kwargs.get("offset") == 0
    assert call_kwargs.kwargs.get("limit") == 100


@pytest.mark.asyncio
async def test_list_all_submissions_custom_offset(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_user_submissions = AsyncMock(return_value=SAMPLE_SUBMISSIONS_PAGE)
    await c.list_all_submissions(offset=200, limit=50)
    call_kwargs = c._http_client.get_user_submissions.call_args
    assert call_kwargs.kwargs.get("offset") == 200
    assert call_kwargs.kwargs.get("limit") == 50


# ── get_form() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_form_returns_form(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_form = AsyncMock(return_value=SAMPLE_FORM)
    result = await c.get_form("123456789")
    assert result["id"] == "123456789"
    assert result["title"] == "Customer Feedback Form"


@pytest.mark.asyncio
async def test_get_form_passes_id(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_form = AsyncMock(return_value=SAMPLE_FORM)
    await c.get_form("123456789")
    call_args = c._http_client.get_form.call_args
    assert "123456789" in call_args.args


@pytest.mark.asyncio
async def test_get_form_not_found_raises(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_form = AsyncMock(
        side_effect=JotformNotFoundError("form", "999999")
    )
    with pytest.raises(JotformNotFoundError):
        await c.get_form("999999")


# ── list_form_questions() ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_form_questions_returns_questions(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_form_questions = AsyncMock(return_value=SAMPLE_QUESTIONS_PAGE)
    result = await c.list_form_questions("123456789")
    assert "items" in result


@pytest.mark.asyncio
async def test_list_form_questions_passes_form_id(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_form_questions = AsyncMock(return_value=SAMPLE_QUESTIONS_PAGE)
    await c.list_form_questions("123456789")
    call_args = c._http_client.get_form_questions.call_args
    assert "123456789" in call_args.args


@pytest.mark.asyncio
async def test_list_form_questions_empty(
    connector_with_mock_client: JotformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_form_questions = AsyncMock(return_value={"items": []})
    result = await c.list_form_questions("123456789")
    assert result["items"] == []


# ── Connector config loading ──────────────────────────────────────────────────


def test_connector_loads_from_config_dict() -> None:
    c = JotformConnector(config={"api_key": "key_from_config"})
    assert c._api_key == "key_from_config"


def test_connector_keyword_arg_fallback() -> None:
    c = JotformConnector(api_key="key_kwarg")
    assert c._api_key == "key_kwarg"


def test_connector_config_takes_precedence_over_kwargs() -> None:
    c = JotformConnector(
        config={"api_key": "key_from_config"},
        api_key="key_kwarg",
    )
    assert c._api_key == "key_from_config"


def test_connector_missing_credentials_list() -> None:
    c = JotformConnector()
    missing = c._missing_credentials()
    assert "api_key" in missing


def test_connector_no_missing_credentials_when_set() -> None:
    c = JotformConnector(api_key="key")
    missing = c._missing_credentials()
    assert missing == []


def test_connector_type_constants() -> None:
    assert JotformConnector.CONNECTOR_TYPE == "jotform"
    assert JotformConnector.AUTH_TYPE == "api_key"


# ── Connector lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_context_manager(connector: JotformConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(connector_with_mock_client: JotformConnector) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


def test_ensure_client_creates_on_first_call(connector: JotformConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: JotformConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2
