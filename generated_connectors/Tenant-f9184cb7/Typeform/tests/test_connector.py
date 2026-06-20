"""Unit tests for TypeformConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import TypeformConnector
from exceptions import (
    TypeformAuthError,
    TypeformError,
    TypeformNetworkError,
    TypeformNotFoundError,
    TypeformRateLimitError,
)
from helpers.utils import (
    normalize_form,
    normalize_response,
    with_retry,
    _extract_answer_value,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ─────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_typeform_test_001"
ACCESS_TOKEN = "tfp_TYPEFORM_ACCESS_TOKEN_TEST"
CLIENT_ID = "tf_client_id_abc"
CLIENT_SECRET = "tf_client_secret_xyz"
REDIRECT_URI = "https://app.shielva.ai/connectors/typeform/callback"

SAMPLE_ME_RESPONSE: dict = {
    "alias": "testuser",
    "email": "test@example.com",
    "language": "en",
}

SAMPLE_FORM: dict = {
    "id": "abc123",
    "title": "Customer Satisfaction Survey",
    "type": "typeform",
    "created_at": "2026-01-01T10:00:00Z",
    "last_updated_at": "2026-06-01T12:00:00Z",
    "fields": [
        {"id": "field_01", "ref": "nps_score", "title": "How likely are you to recommend us?", "type": "rating"},
        {"id": "field_02", "ref": "comment", "title": "Any comments?", "type": "long_text"},
    ],
    "links": {"display": "https://testuser.typeform.com/to/abc123"},
}

SAMPLE_FORM_2: dict = {
    "id": "def456",
    "title": "Employee Survey",
    "type": "typeform",
    "created_at": "2026-02-01T10:00:00Z",
    "last_updated_at": "2026-06-10T08:00:00Z",
    "fields": [],
    "links": {"display": "https://testuser.typeform.com/to/def456"},
}

SAMPLE_RESPONSE: dict = {
    "token": "resp_tok_001",
    "submitted_at": "2026-06-10T09:00:00Z",
    "landed_at": "2026-06-10T08:55:00Z",
    "answers": [
        {
            "type": "number",
            "number": 9,
            "field": {"id": "field_01", "ref": "nps_score", "type": "rating"},
        },
        {
            "type": "text",
            "text": "Great product, love it!",
            "field": {"id": "field_02", "ref": "comment", "type": "long_text"},
        },
    ],
}

SAMPLE_RESPONSE_2: dict = {
    "token": "resp_tok_002",
    "submitted_at": "2026-06-11T10:00:00Z",
    "landed_at": "2026-06-11T09:55:00Z",
    "answers": [
        {
            "type": "number",
            "number": 7,
            "field": {"id": "field_01", "ref": "nps_score", "type": "rating"},
        },
    ],
}

SAMPLE_WORKSPACE: dict = {
    "id": "ws_abc",
    "name": "My Workspace",
    "account_id": "acct_001",
    "shared": False,
    "links": {"forms": "https://api.typeform.com/workspaces/ws_abc/forms"},
}

SAMPLE_WORKSPACE_2: dict = {
    "id": "ws_def",
    "name": "Team Workspace",
    "account_id": "acct_001",
    "shared": True,
    "links": {"forms": "https://api.typeform.com/workspaces/ws_def/forms"},
}

SAMPLE_INSIGHTS: dict = {
    "platforms": {"web": 120, "mobile": 80},
    "prediction_labels": ["Promoter", "Passive", "Detractor"],
    "nps": 42,
    "completion_rate": 0.78,
    "average_time": 120,
}

SAMPLE_FORMS_PAGE: dict = {
    "items": [SAMPLE_FORM],
    "page_count": 1,
    "total_items": 1,
}

SAMPLE_RESPONSES_PAGE: dict = {
    "items": [SAMPLE_RESPONSE],
    "page_count": 1,
    "total_items": 1,
}

SAMPLE_WORKSPACES_PAGE: dict = {
    "items": [SAMPLE_WORKSPACE],
    "page_count": 1,
    "total_items": 1,
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> TypeformConnector:
    return TypeformConnector(
        access_token=ACCESS_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_oauth_only() -> TypeformConnector:
    """Connector with OAuth install creds but no access token yet."""
    return TypeformConnector(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: TypeformConnector) -> TypeformConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_base_fields() -> None:
    exc = TypeformError("base error", status_code=500, code="server_error")
    assert str(exc) == "base error"
    assert exc.status_code == 500
    assert exc.code == "server_error"


def test_exception_auth_is_typeform_error() -> None:
    exc = TypeformAuthError("bad token", 401)
    assert isinstance(exc, TypeformError)
    assert exc.status_code == 401


def test_exception_rate_limit_is_typeform_error() -> None:
    exc = TypeformRateLimitError("too fast")
    assert isinstance(exc, TypeformError)
    assert exc.retry_after == 0.0
    assert exc.status_code == 429


def test_exception_rate_limit_stores_retry_after() -> None:
    exc = TypeformRateLimitError("slow down", retry_after=60.0)
    assert exc.retry_after == 60.0


def test_exception_not_found_is_typeform_error() -> None:
    exc = TypeformNotFoundError("form", "abc123")
    assert isinstance(exc, TypeformError)
    assert exc.status_code == 404
    assert "abc123" in str(exc)
    assert "form" in str(exc)


def test_exception_not_found_message_format() -> None:
    exc = TypeformNotFoundError("workspace", "ws_xyz")
    assert "ws_xyz" in str(exc)
    assert exc.code == "resource_missing"


def test_exception_network_is_typeform_error() -> None:
    exc = TypeformNetworkError("timeout", 503)
    assert isinstance(exc, TypeformError)
    assert exc.status_code == 503


def test_exception_auth_not_subclass_of_network() -> None:
    exc = TypeformAuthError("bad", 401)
    assert not isinstance(exc, TypeformNetworkError)


def test_exception_network_not_subclass_of_auth() -> None:
    exc = TypeformNetworkError("net fail")
    assert not isinstance(exc, TypeformAuthError)


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
    assert r.alias == ""
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
    assert doc.title == "Customer Satisfaction Survey"


def test_normalize_form_fallback_title_when_empty() -> None:
    form_no_title = {**SAMPLE_FORM, "title": ""}
    doc = normalize_form(form_no_title)
    assert "abc123" in doc.title


def test_normalize_form_metadata_type() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert doc.metadata["type"] == "form"


def test_normalize_form_metadata_form_id() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert doc.metadata["form_id"] == "abc123"


def test_normalize_form_metadata_field_count() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert doc.metadata["field_count"] == 2


def test_normalize_form_content_includes_title() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert "Customer Satisfaction Survey" in doc.content


def test_normalize_form_content_includes_fields() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert "How likely are you to recommend us?" in doc.content


def test_normalize_form_source_url_from_links() -> None:
    doc = normalize_form(SAMPLE_FORM)
    assert "testuser.typeform.com" in doc.source_url


def test_normalize_form_source_url_fallback_when_no_links() -> None:
    form_no_links = {**SAMPLE_FORM, "links": {}}
    doc = normalize_form(form_no_links)
    assert "abc123" in doc.source_url


def test_normalize_form_empty_fields_list() -> None:
    doc = normalize_form(SAMPLE_FORM_2)
    assert doc.metadata["field_count"] == 0


# ── normalize_response() ──────────────────────────────────────────────────────


def test_normalize_response_source_id_is_16_chars() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert len(doc.source_id) == 16


def test_normalize_response_source_id_is_hex() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    int(doc.source_id, 16)


def test_normalize_response_source_id_is_deterministic() -> None:
    doc1 = normalize_response(SAMPLE_RESPONSE, "abc123")
    doc2 = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert doc1.source_id == doc2.source_id


def test_normalize_response_different_tokens_produce_different_ids() -> None:
    doc1 = normalize_response(SAMPLE_RESPONSE, "abc123")
    doc2 = normalize_response(SAMPLE_RESPONSE_2, "abc123")
    assert doc1.source_id != doc2.source_id


def test_normalize_response_title_includes_form_id() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert "abc123" in doc.title


def test_normalize_response_title_includes_token_prefix() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert "resp_tok" in doc.title


def test_normalize_response_metadata_type() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert doc.metadata["type"] == "form_response"


def test_normalize_response_metadata_form_id() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert doc.metadata["form_id"] == "abc123"


def test_normalize_response_metadata_token() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert doc.metadata["response_token"] == "resp_tok_001"


def test_normalize_response_metadata_submitted_at() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert doc.metadata["submitted_at"] == "2026-06-10T09:00:00Z"


def test_normalize_response_metadata_answer_count() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert doc.metadata["answer_count"] == 2


def test_normalize_response_content_includes_text_answer() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert "Great product, love it!" in doc.content


def test_normalize_response_content_includes_number_answer() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert "9" in doc.content


def test_normalize_response_no_answers_fallback_content() -> None:
    empty_resp = {**SAMPLE_RESPONSE, "answers": []}
    doc = normalize_response(empty_resp, "abc123")
    assert "abc123" in doc.content


def test_normalize_response_source_url_includes_form_id() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert "abc123" in doc.source_url


def test_normalize_response_connector_and_tenant_empty_by_default() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "abc123")
    assert doc.connector_id == ""
    assert doc.tenant_id == ""


# ── _extract_answer_value() ───────────────────────────────────────────────────


def test_extract_text_answer() -> None:
    answer = {"type": "text", "text": "Hello world", "field": {}}
    assert _extract_answer_value(answer, "text") == "Hello world"


def test_extract_email_answer() -> None:
    answer = {"type": "email", "email": "user@test.com", "field": {}}
    assert _extract_answer_value(answer, "email") == "user@test.com"


def test_extract_url_answer() -> None:
    answer = {"type": "url", "url": "https://example.com", "field": {}}
    assert _extract_answer_value(answer, "url") == "https://example.com"


def test_extract_number_answer() -> None:
    answer = {"type": "number", "number": 42, "field": {}}
    assert _extract_answer_value(answer, "number") == "42"


def test_extract_boolean_answer_true() -> None:
    answer = {"type": "boolean", "boolean": True, "field": {}}
    assert _extract_answer_value(answer, "boolean") == "True"


def test_extract_boolean_answer_false() -> None:
    answer = {"type": "boolean", "boolean": False, "field": {}}
    assert _extract_answer_value(answer, "boolean") == "False"


def test_extract_date_answer() -> None:
    answer = {"type": "date", "date": "2026-06-10", "field": {}}
    assert _extract_answer_value(answer, "date") == "2026-06-10"


def test_extract_choice_answer() -> None:
    answer = {"type": "choice", "choice": {"label": "Option A", "other": ""}, "field": {}}
    assert _extract_answer_value(answer, "choice") == "Option A"


def test_extract_choice_other_answer() -> None:
    answer = {"type": "choice", "choice": {"label": "", "other": "Custom option"}, "field": {}}
    assert _extract_answer_value(answer, "choice") == "Custom option"


def test_extract_choices_answer() -> None:
    answer = {
        "type": "choices",
        "choices": {"labels": ["A", "B"], "other": ""},
        "field": {},
    }
    result = _extract_answer_value(answer, "choices")
    assert "A" in result
    assert "B" in result


def test_extract_choices_with_other() -> None:
    answer = {
        "type": "choices",
        "choices": {"labels": ["X"], "other": "something else"},
        "field": {},
    }
    result = _extract_answer_value(answer, "choices")
    assert "X" in result
    assert "something else" in result


def test_extract_file_url_answer() -> None:
    answer = {"type": "file_url", "file_url": "https://files.typeform.com/abc.pdf", "field": {}}
    assert _extract_answer_value(answer, "file_url") == "https://files.typeform.com/abc.pdf"


def test_extract_payment_answer() -> None:
    answer = {
        "type": "payment",
        "payment": {"amount": "9.99", "currency": "USD"},
        "field": {},
    }
    result = _extract_answer_value(answer, "payment")
    assert "9.99" in result
    assert "USD" in result


def test_extract_unknown_type_falls_back() -> None:
    answer = {"type": "unknown_future_type", "text": "fallback_value", "field": {}}
    result = _extract_answer_value(answer, "unknown_future_type")
    assert result == "fallback_value"


def test_extract_number_none_returns_empty() -> None:
    answer = {"type": "number", "number": None, "field": {}}
    assert _extract_answer_value(answer, "number") == ""


def test_extract_boolean_none_returns_empty() -> None:
    answer = {"type": "boolean", "field": {}}
    assert _extract_answer_value(answer, "boolean") == ""


# ── with_retry() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    results = [TypeformNetworkError("fail"), TypeformNetworkError("fail"), {"ok": True}]
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
    mock_fn = AsyncMock(side_effect=TypeformAuthError("invalid creds", 401))
    with pytest.raises(TypeformAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=TypeformNetworkError("persistent failure"))
    with pytest.raises(TypeformNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=TypeformRateLimitError("429", retry_after=0))
    with pytest.raises(TypeformRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    mock_fn = AsyncMock(return_value={"data": True})
    await with_retry(mock_fn, "arg1", kwarg1="val1")
    mock_fn.assert_called_once_with("arg1", kwarg1="val1")


# ── HTTP client ───────────────────────────────────────────────────────────────


def test_http_client_bearer_header() -> None:
    from client.http_client import TypeformHTTPClient
    client = TypeformHTTPClient()
    headers = client._make_headers("my_token_123")
    assert headers["Authorization"] == "Bearer my_token_123"
    assert headers["Content-Type"] == "application/json"


def test_http_client_base_url() -> None:
    from client.http_client import TYPEFORM_BASE_URL
    assert TYPEFORM_BASE_URL == "https://api.typeform.com"


def test_http_client_default_timeout() -> None:
    from client.http_client import TypeformHTTPClient, DEFAULT_TIMEOUT_S
    client = TypeformHTTPClient()
    assert client._timeout.total == DEFAULT_TIMEOUT_S


def test_http_client_custom_timeout() -> None:
    from client.http_client import TypeformHTTPClient
    client = TypeformHTTPClient(timeout=60.0)
    assert client._timeout.total == 60.0


def test_http_client_raise_for_status_401_raises_auth_error() -> None:
    from client.http_client import TypeformHTTPClient
    client = TypeformHTTPClient()
    with pytest.raises(TypeformAuthError) as exc_info:
        client._raise_for_status(401, {"description": "Unauthorized"})
    assert exc_info.value.status_code == 401


def test_http_client_raise_for_status_403_raises_auth_error() -> None:
    from client.http_client import TypeformHTTPClient
    client = TypeformHTTPClient()
    with pytest.raises(TypeformAuthError) as exc_info:
        client._raise_for_status(403, {"description": "Forbidden"})
    assert exc_info.value.status_code == 403


def test_http_client_raise_for_status_404_raises_not_found() -> None:
    from client.http_client import TypeformHTTPClient
    client = TypeformHTTPClient()
    with pytest.raises(TypeformNotFoundError):
        client._raise_for_status(404, {"description": "Not found"})


def test_http_client_raise_for_status_429_raises_rate_limit() -> None:
    from client.http_client import TypeformHTTPClient
    client = TypeformHTTPClient()
    with pytest.raises(TypeformRateLimitError):
        client._raise_for_status(429, {"description": "Too many requests"})


def test_http_client_raise_for_status_500_raises_network_error() -> None:
    from client.http_client import TypeformHTTPClient
    client = TypeformHTTPClient()
    with pytest.raises(TypeformNetworkError) as exc_info:
        client._raise_for_status(500, {"description": "Server error"})
    assert exc_info.value.status_code == 500


def test_http_client_raise_for_status_other_raises_typeform_error() -> None:
    from client.http_client import TypeformHTTPClient
    client = TypeformHTTPClient()
    with pytest.raises(TypeformError):
        client._raise_for_status(400, {"description": "Bad request"})


def test_http_client_raise_for_status_error_msg_from_description() -> None:
    from client.http_client import TypeformHTTPClient
    client = TypeformHTTPClient()
    with pytest.raises(TypeformAuthError) as exc_info:
        client._raise_for_status(401, {"description": "Token expired"})
    assert "Token expired" in str(exc_info.value)


def test_http_client_raise_for_status_error_msg_fallback_to_message() -> None:
    from client.http_client import TypeformHTTPClient
    client = TypeformHTTPClient()
    with pytest.raises(TypeformNetworkError) as exc_info:
        client._raise_for_status(503, {"message": "Service unavailable"})
    assert "Service unavailable" in str(exc_info.value)


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success_with_oauth_creds(connector_oauth_only: TypeformConnector) -> None:
    result = await connector_oauth_only.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    c = TypeformConnector(client_secret=CLIENT_SECRET)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    c = TypeformConnector(client_id=CLIENT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_secret" in result.message


@pytest.mark.asyncio
async def test_install_missing_both_credentials() -> None:
    c = TypeformConnector()
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message
    assert "client_secret" in result.message


@pytest.mark.asyncio
async def test_install_message_mentions_oauth(connector_oauth_only: TypeformConnector) -> None:
    result = await connector_oauth_only.install()
    assert "OAuth" in result.message or "oauth" in result.message.lower()


# ── authorize() ────────────────────────────────────────────────────────────────


def test_authorize_returns_string(connector_oauth_only: TypeformConnector) -> None:
    url = connector_oauth_only.authorize()
    assert isinstance(url, str)


def test_authorize_contains_typeform_url(connector_oauth_only: TypeformConnector) -> None:
    url = connector_oauth_only.authorize()
    assert "api.typeform.com/oauth/authorize" in url


def test_authorize_contains_client_id(connector_oauth_only: TypeformConnector) -> None:
    url = connector_oauth_only.authorize()
    assert CLIENT_ID in url


def test_authorize_contains_scopes(connector_oauth_only: TypeformConnector) -> None:
    url = connector_oauth_only.authorize()
    assert "forms" in url
    assert "responses" in url
    assert "workspaces" in url


def test_authorize_contains_redirect_uri_when_set(connector_oauth_only: TypeformConnector) -> None:
    url = connector_oauth_only.authorize()
    assert "redirect_uri" in url
    assert REDIRECT_URI in url


def test_authorize_no_redirect_uri_when_not_set() -> None:
    c = TypeformConnector(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    url = c.authorize()
    assert "redirect_uri" not in url


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: TypeformConnector) -> None:
    with patch("connector.TypeformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "testuser" in result.message
    assert result.alias == "testuser"
    assert result.email == "test@example.com"


@pytest.mark.asyncio
async def test_health_check_uses_email_when_no_alias(connector: TypeformConnector) -> None:
    me_no_alias = {"alias": "", "email": "admin@company.com"}
    with patch("connector.TypeformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=me_no_alias)
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert "admin@company.com" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: TypeformConnector) -> None:
    with patch("connector.TypeformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=TypeformAuthError("Forbidden", 403))
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: TypeformConnector) -> None:
    with patch("connector.TypeformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=TypeformNetworkError("Timeout"))
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_access_token() -> None:
    c = TypeformConnector(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: TypeformConnector) -> None:
    with patch("connector.TypeformHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=RuntimeError("boom"))
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty_forms(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_forms = AsyncMock(
        return_value={"items": [], "page_count": 1, "total_items": 0}
    )
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_single_form_single_response(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_forms = AsyncMock(return_value=SAMPLE_FORMS_PAGE)
    c._http_client.get_responses = AsyncMock(return_value=SAMPLE_RESPONSES_PAGE)
    result = await c.sync(kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    # 1 form doc + 1 response doc
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_returns_sync_result(connector_with_mock_client: TypeformConnector) -> None:
    from models import SyncResult as SR
    c = connector_with_mock_client
    c._http_client.list_forms = AsyncMock(return_value=SAMPLE_FORMS_PAGE)
    c._http_client.get_responses = AsyncMock(return_value={"items": []})
    result = await c.sync()
    assert isinstance(result, SR)


@pytest.mark.asyncio
async def test_sync_forms_api_error_returns_failed(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_forms = AsyncMock(
        side_effect=TypeformError("server error", 500)
    )
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_responses_api_error_counts_failure(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_forms = AsyncMock(return_value=SAMPLE_FORMS_PAGE)
    c._http_client.get_responses = AsyncMock(
        side_effect=TypeformError("server error", 500)
    )
    result = await c.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1


@pytest.mark.asyncio
async def test_sync_forms_pagination(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    page1 = {"items": [SAMPLE_FORM], "page_count": 2, "total_items": 2}
    page2 = {"items": [SAMPLE_FORM_2], "page_count": 2, "total_items": 2}
    c._http_client.list_forms = AsyncMock(side_effect=[page1, page2])
    c._http_client.get_responses = AsyncMock(return_value={"items": []})
    result = await c.sync()
    assert c._http_client.list_forms.call_count == 2
    # 2 form docs
    assert result.documents_synced >= 2


@pytest.mark.asyncio
async def test_sync_responses_cursor_pagination(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_forms = AsyncMock(return_value=SAMPLE_FORMS_PAGE)
    page1 = [{"token": f"tok_{i:03d}", "submitted_at": "2026-06-01T00:00:00Z", "landed_at": "", "answers": []} for i in range(25)]
    page2_resp = [SAMPLE_RESPONSE]
    c._http_client.get_responses = AsyncMock(side_effect=[
        {"items": page1},
        {"items": page2_resp},
    ])
    result = await c.sync()
    assert c._http_client.get_responses.call_count == 2
    # 1 form doc + 26 response docs
    assert result.documents_found == 26 + 1  # 25 page1 + 1 page2 + 1 form
    # Actually found from forms = 1 (form), then responses = 25 + 1 = 26
    assert result.documents_synced >= 26


# ── list_forms() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_forms_returns_list(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_forms = AsyncMock(return_value=SAMPLE_FORMS_PAGE)
    result = await c.list_forms()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == "abc123"


@pytest.mark.asyncio
async def test_list_forms_auto_paginates(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    page1 = {"items": [SAMPLE_FORM], "page_count": 2, "total_items": 2}
    page2 = {"items": [SAMPLE_FORM_2], "page_count": 2, "total_items": 2}
    c._http_client.list_forms = AsyncMock(side_effect=[page1, page2])
    result = await c.list_forms()
    assert len(result) == 2
    assert c._http_client.list_forms.call_count == 2


@pytest.mark.asyncio
async def test_list_forms_passes_workspace_id(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_forms = AsyncMock(return_value=SAMPLE_FORMS_PAGE)
    await c.list_forms(workspace_id="ws_abc")
    call_kwargs = c._http_client.list_forms.call_args.kwargs
    assert call_kwargs.get("workspace_id") == "ws_abc"


@pytest.mark.asyncio
async def test_list_forms_empty_returns_empty_list(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_forms = AsyncMock(
        return_value={"items": [], "page_count": 1, "total_items": 0}
    )
    result = await c.list_forms()
    assert result == []


# ── get_form() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_form_returns_form(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_form = AsyncMock(return_value=SAMPLE_FORM)
    result = await c.get_form("abc123")
    assert result["id"] == "abc123"
    assert result["title"] == "Customer Satisfaction Survey"


@pytest.mark.asyncio
async def test_get_form_passes_id(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_form = AsyncMock(return_value=SAMPLE_FORM)
    await c.get_form("abc123")
    call_args = c._http_client.get_form.call_args
    assert "abc123" in call_args.args


@pytest.mark.asyncio
async def test_get_form_not_found_raises(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_form = AsyncMock(
        side_effect=TypeformNotFoundError("form", "xyz999")
    )
    with pytest.raises(TypeformNotFoundError):
        await c.get_form("xyz999")


# ── get_responses() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_responses_returns_list(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_responses = AsyncMock(return_value=SAMPLE_RESPONSES_PAGE)
    result = await c.get_responses("abc123")
    assert isinstance(result, list)
    assert result[0]["token"] == "resp_tok_001"


@pytest.mark.asyncio
async def test_get_responses_auto_paginates(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    page1 = [{"token": f"tok_{i:03d}", "submitted_at": "", "landed_at": "", "answers": []} for i in range(1000)]
    page2 = [SAMPLE_RESPONSE]
    c._http_client.get_responses = AsyncMock(side_effect=[
        {"items": page1},
        {"items": page2},
    ])
    result = await c.get_responses("abc123")
    assert len(result) == 1001
    assert c._http_client.get_responses.call_count == 2


@pytest.mark.asyncio
async def test_get_responses_passes_before_cursor(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_responses = AsyncMock(return_value={"items": [SAMPLE_RESPONSE]})
    # Only 1 item so no cursor needed, but we can verify the method passes through
    await c.get_responses("abc123", page_size=1000)
    assert c._http_client.get_responses.called


@pytest.mark.asyncio
async def test_get_responses_empty_returns_empty_list(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_responses = AsyncMock(return_value={"items": []})
    result = await c.get_responses("abc123")
    assert result == []


# ── list_workspaces() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_workspaces_returns_list(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_workspaces = AsyncMock(return_value=SAMPLE_WORKSPACES_PAGE)
    result = await c.list_workspaces()
    assert isinstance(result, list)
    assert result[0]["id"] == "ws_abc"
    assert result[0]["name"] == "My Workspace"


@pytest.mark.asyncio
async def test_list_workspaces_auto_paginates(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    page1 = {"items": [SAMPLE_WORKSPACE], "page_count": 2, "total_items": 2}
    page2 = {"items": [SAMPLE_WORKSPACE_2], "page_count": 2, "total_items": 2}
    c._http_client.list_workspaces = AsyncMock(side_effect=[page1, page2])
    result = await c.list_workspaces()
    assert len(result) == 2
    assert c._http_client.list_workspaces.call_count == 2


@pytest.mark.asyncio
async def test_list_workspaces_empty_returns_empty_list(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_workspaces = AsyncMock(
        return_value={"items": [], "page_count": 1, "total_items": 0}
    )
    result = await c.list_workspaces()
    assert result == []


# ── get_workspace() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_workspace_returns_workspace(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_workspace = AsyncMock(return_value=SAMPLE_WORKSPACE)
    result = await c.get_workspace("ws_abc")
    assert result["id"] == "ws_abc"
    assert result["name"] == "My Workspace"


@pytest.mark.asyncio
async def test_get_workspace_passes_id(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_workspace = AsyncMock(return_value=SAMPLE_WORKSPACE)
    await c.get_workspace("ws_abc")
    call_args = c._http_client.get_workspace.call_args
    assert "ws_abc" in call_args.args


@pytest.mark.asyncio
async def test_get_workspace_not_found_raises(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_workspace = AsyncMock(
        side_effect=TypeformNotFoundError("workspace", "ws_missing")
    )
    with pytest.raises(TypeformNotFoundError):
        await c.get_workspace("ws_missing")


# ── get_insights() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_insights_returns_dict(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_insights = AsyncMock(return_value=SAMPLE_INSIGHTS)
    result = await c.get_insights("abc123")
    assert result["nps"] == 42
    assert result["completion_rate"] == 0.78


@pytest.mark.asyncio
async def test_get_insights_passes_form_id(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_insights = AsyncMock(return_value=SAMPLE_INSIGHTS)
    await c.get_insights("abc123")
    call_args = c._http_client.get_insights.call_args
    assert "abc123" in call_args.args


@pytest.mark.asyncio
async def test_get_insights_not_found_raises(
    connector_with_mock_client: TypeformConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_insights = AsyncMock(
        side_effect=TypeformNotFoundError("form", "missing_form")
    )
    with pytest.raises(TypeformNotFoundError):
        await c.get_insights("missing_form")


# ── Connector config loading ──────────────────────────────────────────────────


def test_connector_loads_access_token_from_config() -> None:
    c = TypeformConnector(config={"access_token": "tok_from_config"})
    assert c._access_token == "tok_from_config"


def test_connector_loads_client_id_from_config() -> None:
    c = TypeformConnector(config={"client_id": "id_from_config"})
    assert c._client_id == "id_from_config"


def test_connector_loads_client_secret_from_config() -> None:
    c = TypeformConnector(config={"client_secret": "secret_from_config"})
    assert c._client_secret == "secret_from_config"


def test_connector_loads_redirect_uri_from_config() -> None:
    c = TypeformConnector(config={"redirect_uri": "https://example.com/cb"})
    assert c._redirect_uri == "https://example.com/cb"


def test_connector_keyword_arg_fallback() -> None:
    c = TypeformConnector(access_token="tok_kwarg")
    assert c._access_token == "tok_kwarg"


def test_connector_config_takes_precedence_over_kwargs() -> None:
    c = TypeformConnector(
        config={"access_token": "tok_from_config"},
        access_token="tok_kwarg",
    )
    assert c._access_token == "tok_from_config"


def test_connector_missing_install_credentials_list() -> None:
    c = TypeformConnector()
    missing = c._missing_install_credentials()
    assert "client_id" in missing
    assert "client_secret" in missing


def test_connector_no_missing_install_credentials_when_set() -> None:
    c = TypeformConnector(client_id="id", client_secret="secret")
    missing = c._missing_install_credentials()
    assert missing == []


def test_connector_missing_credentials_when_no_access_token() -> None:
    c = TypeformConnector()
    missing = c._missing_credentials()
    assert "access_token" in missing


def test_connector_no_missing_credentials_when_access_token_set() -> None:
    c = TypeformConnector(access_token="tok")
    missing = c._missing_credentials()
    assert missing == []


def test_connector_type_constants() -> None:
    assert TypeformConnector.CONNECTOR_TYPE == "typeform"
    assert TypeformConnector.AUTH_TYPE == "oauth2"


# ── Connector lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_context_manager(connector: TypeformConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(connector_with_mock_client: TypeformConnector) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


def test_ensure_client_creates_on_first_call(connector: TypeformConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: TypeformConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2
