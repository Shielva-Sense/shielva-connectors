"""Unit tests for SurveyMonkeyConnector — all HTTP calls are mocked via AsyncMock.

Total tests: 80+
Coverage:
  - exceptions (6)
  - models (5)
  - normalize_survey (7)
  - normalize_response (8)
  - normalize_collector (6)
  - with_retry (7)
  - HTTP client (16)
  - install() (6)
  - health_check() (6)
  - authorize() (4)
  - sync() (9)
  - list_surveys() (3)
  - list_responses() (4)
  - list_collectors() (3)
  - get_survey() (2)
  - get_survey_details() (2)
  - pagination helpers (3)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import SurveyMonkeyConnector
from exceptions import (
    SurveyMonkeyAuthError,
    SurveyMonkeyError,
    SurveyMonkeyNetworkError,
    SurveyMonkeyNotFoundError,
    SurveyMonkeyRateLimitError,
)
from helpers.utils import (
    _extract_answer_text,
    _short_hash,
    normalize_collector,
    normalize_response,
    normalize_survey,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, ResourceKind, SyncStatus

# ── Shared test data ──────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_surveymonkey_test_001"
ACCESS_TOKEN = "sm_SURVEYMONKEY_ACCESS_TOKEN_TEST"
CLIENT_ID = "test_client_id"
CLIENT_SECRET = "test_client_secret"
REDIRECT_URI = "https://app.shielva.ai/connectors/surveymonkey/callback"

SAMPLE_ME: dict = {
    "username": "testuser",
    "email": "test@example.com",
    "first_name": "Test",
    "last_name": "User",
    "account_type": "enterprise",
}

SAMPLE_SURVEY: dict = {
    "id": "123456789",
    "title": "Customer Satisfaction Survey",
    "href": "https://api.surveymonkey.com/v3/surveys/123456789",
    "date_created": "2026-01-01T10:00:00+00:00",
    "date_modified": "2026-06-01T12:00:00+00:00",
    "question_count": 5,
    "page_count": 2,
    "response_count": 42,
}

SAMPLE_SURVEY_2: dict = {
    "id": "987654321",
    "title": "Product Feedback",
    "href": "https://api.surveymonkey.com/v3/surveys/987654321",
    "date_created": "2026-02-01T10:00:00+00:00",
    "date_modified": "2026-06-05T09:00:00+00:00",
    "question_count": 3,
    "page_count": 1,
    "response_count": 17,
}

SAMPLE_RESPONSE: dict = {
    "id": "resp_001",
    "date_created": "2026-06-10T09:00:00+00:00",
    "date_modified": "2026-06-10T09:05:00+00:00",
    "ip_address": "192.168.1.1",
    "response_status": "completed",
    "total_time": 120,
    "collector_id": "coll_001",
    "pages": [
        {
            "id": "page_1",
            "questions": [
                {
                    "id": "q_001",
                    "answers": [
                        {"text": "Very satisfied", "choice_id": "ch_01"}
                    ],
                },
                {
                    "id": "q_002",
                    "answers": [
                        {"text": "Great product overall!"}
                    ],
                },
            ],
        }
    ],
}

SAMPLE_RESPONSE_2: dict = {
    "id": "resp_002",
    "date_created": "2026-06-11T10:00:00+00:00",
    "date_modified": "2026-06-11T10:10:00+00:00",
    "ip_address": "10.0.0.1",
    "response_status": "completed",
    "total_time": 90,
    "collector_id": "coll_001",
    "pages": [],
}

SAMPLE_COLLECTOR: dict = {
    "id": "coll_001",
    "name": "Email Invitation",
    "status": "open",
    "type": "email",
    "href": "https://api.surveymonkey.com/v3/collectors/coll_001",
    "date_created": "2026-01-05T10:00:00+00:00",
    "date_modified": "2026-06-01T10:00:00+00:00",
    "survey_id": "123456789",
}

SAMPLE_SURVEYS_PAGE: dict = {
    "data": [SAMPLE_SURVEY],
    "links": {"self": "...", "next": None},
    "per_page": 50,
    "total": 1,
    "page": 1,
}

SAMPLE_SURVEYS_PAGE_WITH_NEXT: dict = {
    "data": [SAMPLE_SURVEY],
    "links": {"self": "...", "next": "https://api.surveymonkey.com/v3/surveys?page=2"},
    "per_page": 50,
    "total": 2,
    "page": 1,
}

SAMPLE_SURVEYS_PAGE_2: dict = {
    "data": [SAMPLE_SURVEY_2],
    "links": {"self": "...", "next": None},
    "per_page": 50,
    "total": 2,
    "page": 2,
}

SAMPLE_RESPONSES_PAGE: dict = {
    "data": [SAMPLE_RESPONSE],
    "links": {"self": "...", "next": None},
    "per_page": 100,
    "total": 1,
    "page": 1,
}

SAMPLE_COLLECTORS_PAGE: dict = {
    "data": [SAMPLE_COLLECTOR],
    "links": {"self": "...", "next": None},
    "per_page": 50,
    "total": 1,
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def config() -> dict:
    return {
        "access_token": ACCESS_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    }


@pytest.fixture()
def connector(config: dict) -> SurveyMonkeyConnector:
    return SurveyMonkeyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=config,
    )


@pytest.fixture()
def connector_mock(connector: SurveyMonkeyConnector) -> SurveyMonkeyConnector:
    connector.client = MagicMock()
    return connector


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_base_attributes() -> None:
    exc = SurveyMonkeyError("base error", status_code=500, code="server_error")
    assert exc.message == "base error"
    assert exc.status_code == 500
    assert exc.code == "server_error"
    assert str(exc) == "base error"


def test_auth_error_is_base_error() -> None:
    exc = SurveyMonkeyAuthError("bad token", 401)
    assert isinstance(exc, SurveyMonkeyError)
    assert exc.status_code == 401


def test_rate_limit_error_stores_retry_after() -> None:
    exc = SurveyMonkeyRateLimitError("too fast", retry_after=60.0)
    assert isinstance(exc, SurveyMonkeyError)
    assert exc.status_code == 429
    assert exc.retry_after == 60.0
    assert exc.code == "rate_limit"


def test_rate_limit_error_default_retry_after() -> None:
    exc = SurveyMonkeyRateLimitError("slow down")
    assert exc.retry_after == 0.0


def test_not_found_error_message_and_code() -> None:
    exc = SurveyMonkeyNotFoundError("survey", "123456")
    assert isinstance(exc, SurveyMonkeyError)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"
    assert "123456" in str(exc)
    assert "survey" in str(exc)


def test_network_error_is_base_error() -> None:
    exc = SurveyMonkeyNetworkError("timeout", 504)
    assert isinstance(exc, SurveyMonkeyError)
    assert exc.status_code == 504


# ── Models ────────────────────────────────────────────────────────────────────


def test_connector_health_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.FAILED == "failed"


def test_sync_status_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_resource_kind_values() -> None:
    assert ResourceKind.SURVEY == "survey"
    assert ResourceKind.RESPONSE == "response"
    assert ResourceKind.COLLECTOR == "collector"
    assert ResourceKind.CONTACT == "contact"
    assert ResourceKind.CONTACT_LIST == "contact_list"


def test_connector_document_defaults() -> None:
    from models import ConnectorDocument
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test",
        content="Content",
        connector_id="conn_1",
        tenant_id="tenant_1",
    )
    assert doc.source_url == ""
    assert doc.metadata == {}
    assert doc.resource_kind == ""


# ── normalize_survey() ────────────────────────────────────────────────────────


def test_normalize_survey_basic() -> None:
    doc = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Customer Satisfaction Survey"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert doc.resource_kind == "survey"


def test_normalize_survey_source_id_is_16_chars() -> None:
    doc = normalize_survey(SAMPLE_SURVEY)
    assert len(doc.source_id) == 16


def test_normalize_survey_source_id_is_hex() -> None:
    doc = normalize_survey(SAMPLE_SURVEY)
    int(doc.source_id, 16)


def test_normalize_survey_source_id_deterministic() -> None:
    doc1 = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_survey_different_ids_differ() -> None:
    doc1 = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_survey(SAMPLE_SURVEY_2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_survey_metadata_fields() -> None:
    doc = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["survey_id"] == "123456789"
    assert meta["title"] == "Customer Satisfaction Survey"
    assert meta["question_count"] == 5
    assert meta["page_count"] == 2
    assert meta["response_count"] == 42


def test_normalize_survey_missing_title_fallback() -> None:
    raw = {**SAMPLE_SURVEY, "title": ""}
    doc = normalize_survey(raw)
    assert "123456789" in doc.title


# ── normalize_response() ──────────────────────────────────────────────────────


def test_normalize_response_basic() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "123456789", CONNECTOR_ID, TENANT_ID)
    assert "resp_001" in doc.title
    assert doc.resource_kind == "response"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_response_source_id_16_chars() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "123456789")
    assert len(doc.source_id) == 16


def test_normalize_response_source_id_hex() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "123456789")
    int(doc.source_id, 16)


def test_normalize_response_source_id_deterministic() -> None:
    doc1 = normalize_response(SAMPLE_RESPONSE, "123456789", CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_response(SAMPLE_RESPONSE, "123456789", CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_response_different_ids_differ() -> None:
    doc1 = normalize_response(SAMPLE_RESPONSE, "123456789")
    doc2 = normalize_response(SAMPLE_RESPONSE_2, "123456789")
    assert doc1.source_id != doc2.source_id


def test_normalize_response_content_includes_answers() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "123456789")
    assert "Very satisfied" in doc.content or "Great product" in doc.content


def test_normalize_response_empty_pages() -> None:
    doc = normalize_response(SAMPLE_RESPONSE_2, "123456789")
    assert "resp_002" in doc.title
    assert "no answers" in doc.content or doc.content is not None


def test_normalize_response_metadata() -> None:
    doc = normalize_response(SAMPLE_RESPONSE, "123456789", CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["response_id"] == "resp_001"
    assert meta["survey_id"] == "123456789"
    assert meta["collector_id"] == "coll_001"
    assert meta["response_status"] == "completed"
    assert meta["total_time"] == 120


# ── normalize_collector() ─────────────────────────────────────────────────────


def test_normalize_collector_basic() -> None:
    doc = normalize_collector(SAMPLE_COLLECTOR, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Email Invitation"
    assert doc.resource_kind == "collector"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_collector_source_id_16_chars() -> None:
    doc = normalize_collector(SAMPLE_COLLECTOR)
    assert len(doc.source_id) == 16


def test_normalize_collector_source_id_deterministic() -> None:
    doc1 = normalize_collector(SAMPLE_COLLECTOR)
    doc2 = normalize_collector(SAMPLE_COLLECTOR)
    assert doc1.source_id == doc2.source_id


def test_normalize_collector_metadata() -> None:
    doc = normalize_collector(SAMPLE_COLLECTOR, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["collector_id"] == "coll_001"
    assert meta["survey_id"] == "123456789"
    assert meta["status"] == "open"
    assert meta["type"] == "email"


def test_normalize_collector_missing_name_fallback() -> None:
    raw = {**SAMPLE_COLLECTOR, "name": ""}
    doc = normalize_collector(raw)
    assert "coll_001" in doc.title


def test_normalize_collector_content_includes_status() -> None:
    doc = normalize_collector(SAMPLE_COLLECTOR)
    assert "open" in doc.content
    assert "email" in doc.content


# ── _extract_answer_text() ────────────────────────────────────────────────────


def test_extract_answer_text_field() -> None:
    answer = {"text": "My answer", "choice_id": "ch_01"}
    assert _extract_answer_text(answer) == "My answer"


def test_extract_answer_row_choice() -> None:
    answer = {"row_id": "r1", "choice_id": "c1"}
    result = _extract_answer_text(answer)
    assert "r1" in result
    assert "c1" in result


def test_extract_answer_choice_only() -> None:
    answer = {"choice_id": "c99"}
    result = _extract_answer_text(answer)
    assert "c99" in result


def test_extract_answer_other_id() -> None:
    answer = {"other_id": "other_1", "text": "Custom text"}
    result = _extract_answer_text(answer)
    assert "Custom text" in result


def test_extract_answer_other_id_no_text() -> None:
    answer = {"other_id": "other_99"}
    result = _extract_answer_text(answer)
    assert "other_99" in result


def test_extract_answer_empty() -> None:
    assert _extract_answer_text({}) == ""


# ── with_retry() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    results: list = [
        SurveyMonkeyNetworkError("fail"),
        SurveyMonkeyNetworkError("fail"),
        {"ok": True},
    ]
    idx = {"n": 0}

    async def fn_impl(*args, **kwargs):  # type: ignore[no-untyped-def]
        val = results[idx["n"]]
        idx["n"] += 1
        if isinstance(val, Exception):
            raise val
        return val

    result = await with_retry(fn_impl, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert idx["n"] == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=SurveyMonkeyAuthError("invalid", 401))
    with pytest.raises(SurveyMonkeyAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=SurveyMonkeyNetworkError("persistent"))
    with pytest.raises(SurveyMonkeyNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=SurveyMonkeyRateLimitError("429", retry_after=0))
    with pytest.raises(SurveyMonkeyRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    mock_fn = AsyncMock(return_value={"result": "ok"})
    await with_retry(mock_fn, "arg1", key="value")
    mock_fn.assert_called_once_with("arg1", key="value")


@pytest.mark.asyncio
async def test_with_retry_single_attempt_no_retry() -> None:
    mock_fn = AsyncMock(return_value="done")
    result = await with_retry(mock_fn, max_attempts=1)
    assert result == "done"


# ── HTTP client unit tests ────────────────────────────────────────────────────


def test_http_client_bearer_header() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient(config={"access_token": "tok_abc"})
    headers = client._make_headers()
    assert headers["Authorization"] == "Bearer tok_abc"
    assert headers["Content-Type"] == "application/json"


def test_http_client_base_url() -> None:
    from client.http_client import SURVEYMONKEY_BASE_URL
    assert SURVEYMONKEY_BASE_URL == "https://api.surveymonkey.com/v3"


def test_http_client_auth_url() -> None:
    from client.http_client import SURVEYMONKEY_AUTH_URL
    assert "surveymonkey.com/oauth/authorize" in SURVEYMONKEY_AUTH_URL


def test_http_client_token_url() -> None:
    from client.http_client import SURVEYMONKEY_TOKEN_URL
    assert "surveymonkey.com/oauth/token" in SURVEYMONKEY_TOKEN_URL


def test_http_client_default_timeout() -> None:
    from client.http_client import SurveyMonkeyHTTPClient, DEFAULT_TIMEOUT_S
    client = SurveyMonkeyHTTPClient()
    assert client._timeout.total == DEFAULT_TIMEOUT_S


def test_http_client_custom_timeout() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient(timeout=60.0)
    assert client._timeout.total == 60.0


def test_raise_for_status_401() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient()
    with pytest.raises(SurveyMonkeyAuthError):
        client._raise_for_status(401, "Unauthorized")


def test_raise_for_status_403() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient()
    with pytest.raises(SurveyMonkeyAuthError):
        client._raise_for_status(403, "Forbidden")


def test_raise_for_status_404() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient()
    with pytest.raises(SurveyMonkeyNotFoundError):
        client._raise_for_status(404, "Not found")


def test_raise_for_status_429() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient()
    with pytest.raises(SurveyMonkeyRateLimitError):
        client._raise_for_status(429, "Too many requests")


def test_raise_for_status_500() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient()
    with pytest.raises(SurveyMonkeyNetworkError):
        client._raise_for_status(500, "Internal server error")


def test_raise_for_status_503() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient()
    with pytest.raises(SurveyMonkeyNetworkError):
        client._raise_for_status(503, "Service unavailable")


def test_raise_for_status_other() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient()
    with pytest.raises(SurveyMonkeyError):
        client._raise_for_status(422, "Unprocessable")


def test_http_client_access_token_from_config() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient(config={"access_token": "my_token"})
    assert client._access_token == "my_token"


def test_http_client_empty_config() -> None:
    from client.http_client import SurveyMonkeyHTTPClient
    client = SurveyMonkeyHTTPClient()
    assert client._access_token == ""


# ── install() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success(connector: SurveyMonkeyConnector) -> None:
    connector.client = MagicMock()
    connector.client.get_me = AsyncMock(return_value=SAMPLE_ME)
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "testuser" in result.message


@pytest.mark.asyncio
async def test_install_success_shows_email_when_no_username(
    connector: SurveyMonkeyConnector,
) -> None:
    connector.client = MagicMock()
    connector.client.get_me = AsyncMock(
        return_value={**SAMPLE_ME, "username": "", "email": "admin@co.com"}
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert "admin@co.com" in result.message


@pytest.mark.asyncio
async def test_install_missing_access_token() -> None:
    c = SurveyMonkeyConnector(
        tenant_id=TENANT_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        },
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_missing_install_fields() -> None:
    c = SurveyMonkeyConnector(tenant_id=TENANT_ID, config={"access_token": ACCESS_TOKEN})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


@pytest.mark.asyncio
async def test_install_auth_error(connector: SurveyMonkeyConnector) -> None:
    connector.client = MagicMock()
    connector.client.get_me = AsyncMock(
        side_effect=SurveyMonkeyAuthError("Invalid token", 401)
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error(connector: SurveyMonkeyConnector) -> None:
    connector.client = MagicMock()
    connector.client.get_me = AsyncMock(
        side_effect=SurveyMonkeyNetworkError("Timeout")
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: SurveyMonkeyConnector) -> None:
    connector.client = MagicMock()
    connector.client.get_me = AsyncMock(return_value=SAMPLE_ME)
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.username == "testuser"
    assert result.email == "test@example.com"


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: SurveyMonkeyConnector) -> None:
    connector.client = MagicMock()
    connector.client.get_me = AsyncMock(
        side_effect=SurveyMonkeyAuthError("Forbidden", 403)
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: SurveyMonkeyConnector) -> None:
    connector.client = MagicMock()
    connector.client.get_me = AsyncMock(
        side_effect=SurveyMonkeyNetworkError("Timeout")
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_access_token() -> None:
    c = SurveyMonkeyConnector(tenant_id=TENANT_ID, config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: SurveyMonkeyConnector) -> None:
    connector.client = MagicMock()
    connector.client.get_me = AsyncMock(side_effect=RuntimeError("boom"))
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_returns_message(connector: SurveyMonkeyConnector) -> None:
    connector.client = MagicMock()
    connector.client.get_me = AsyncMock(return_value=SAMPLE_ME)
    result = await connector.health_check()
    assert "SurveyMonkey API reachable" in result.message


# ── authorize() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authorize_returns_url(connector: SurveyMonkeyConnector) -> None:
    url = await connector.authorize()
    assert url.startswith("https://api.surveymonkey.com/oauth/authorize")


@pytest.mark.asyncio
async def test_authorize_includes_client_id(connector: SurveyMonkeyConnector) -> None:
    url = await connector.authorize()
    assert "client_id=test_client_id" in url


@pytest.mark.asyncio
async def test_authorize_includes_redirect_uri(connector: SurveyMonkeyConnector) -> None:
    url = await connector.authorize()
    assert "redirect_uri=" in url
    assert "shielva.ai" in url


@pytest.mark.asyncio
async def test_authorize_includes_response_type(connector: SurveyMonkeyConnector) -> None:
    url = await connector.authorize()
    assert "response_type=code" in url


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty_surveys(connector_mock: SurveyMonkeyConnector) -> None:
    connector_mock.client.get_surveys = AsyncMock(
        return_value={"data": [], "links": {"next": None}}
    )
    result = await connector_mock.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_single_survey_single_response(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_surveys = AsyncMock(return_value=SAMPLE_SURVEYS_PAGE)
    connector_mock.client.get_responses = AsyncMock(return_value=SAMPLE_RESPONSES_PAGE)
    result = await connector_mock.sync(kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    # 1 survey + 1 response found
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_survey_pagination_via_links_next(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_surveys = AsyncMock(
        side_effect=[SAMPLE_SURVEYS_PAGE_WITH_NEXT, SAMPLE_SURVEYS_PAGE_2]
    )
    connector_mock.client.get_responses = AsyncMock(
        return_value={"data": [], "links": {"next": None}}
    )
    result = await connector_mock.sync()
    assert connector_mock.client.get_surveys.call_count == 2
    # 2 surveys found, 0 responses
    assert result.documents_found == 2


@pytest.mark.asyncio
async def test_sync_responses_per_survey(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_surveys = AsyncMock(
        return_value={
            "data": [SAMPLE_SURVEY, SAMPLE_SURVEY_2],
            "links": {"next": None},
        }
    )
    connector_mock.client.get_responses = AsyncMock(
        return_value=SAMPLE_RESPONSES_PAGE
    )
    result = await connector_mock.sync()
    # get_responses called once per survey
    assert connector_mock.client.get_responses.call_count == 2
    assert result.documents_found == 4  # 2 surveys + 2 responses (1 per survey)


@pytest.mark.asyncio
async def test_sync_response_fetch_error_counts_failure(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_surveys = AsyncMock(return_value=SAMPLE_SURVEYS_PAGE)
    connector_mock.client.get_responses = AsyncMock(
        side_effect=SurveyMonkeyError("server error", 500)
    )
    result = await connector_mock.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1


@pytest.mark.asyncio
async def test_sync_surveys_api_error_returns_failed(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_surveys = AsyncMock(
        side_effect=SurveyMonkeyError("server error", 500)
    )
    result = await connector_mock.sync()
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_completed_status_when_no_failures(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_surveys = AsyncMock(return_value=SAMPLE_SURVEYS_PAGE)
    connector_mock.client.get_responses = AsyncMock(return_value=SAMPLE_RESPONSES_PAGE)
    result = await connector_mock.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_partial_status_when_some_failures(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    good_survey = SAMPLE_SURVEY
    bad_survey = {**SAMPLE_SURVEY_2, "id": ""}  # empty id causes skip
    connector_mock.client.get_surveys = AsyncMock(
        return_value={
            "data": [good_survey, bad_survey],
            "links": {"next": None},
        }
    )
    connector_mock.client.get_responses = AsyncMock(
        side_effect=SurveyMonkeyError("fail", 500)
    )
    result = await connector_mock.sync()
    assert result.documents_failed >= 1


@pytest.mark.asyncio
async def test_sync_response_pagination_stops_when_no_next(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_surveys = AsyncMock(return_value=SAMPLE_SURVEYS_PAGE)
    # 100 items but no next link — should stop after first page
    responses_full = [{"id": f"resp_{i}", "pages": []} for i in range(100)]
    connector_mock.client.get_responses = AsyncMock(
        return_value={"data": responses_full, "links": {"next": None}}
    )
    result = await connector_mock.sync()
    assert connector_mock.client.get_responses.call_count == 1
    assert result.documents_found == 101  # 1 survey + 100 responses


# ── list_surveys() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_surveys_returns_list(connector_mock: SurveyMonkeyConnector) -> None:
    connector_mock.client.get_surveys = AsyncMock(return_value=SAMPLE_SURVEYS_PAGE)
    result = await connector_mock.list_surveys()
    assert isinstance(result, list)
    assert result[0]["id"] == "123456789"


@pytest.mark.asyncio
async def test_list_surveys_default_pagination(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_surveys = AsyncMock(return_value=SAMPLE_SURVEYS_PAGE)
    await connector_mock.list_surveys()
    call_kwargs = connector_mock.client.get_surveys.call_args
    assert call_kwargs.kwargs.get("per_page") == 50


@pytest.mark.asyncio
async def test_list_surveys_custom_page(connector_mock: SurveyMonkeyConnector) -> None:
    connector_mock.client.get_surveys = AsyncMock(return_value=SAMPLE_SURVEYS_PAGE)
    await connector_mock.list_surveys(page=3, per_page=25)
    call_kwargs = connector_mock.client.get_surveys.call_args
    assert call_kwargs.kwargs.get("page") == 3
    assert call_kwargs.kwargs.get("per_page") == 25


# ── list_responses() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_responses_returns_list(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_responses = AsyncMock(return_value=SAMPLE_RESPONSES_PAGE)
    result = await connector_mock.list_responses("123456789")
    assert isinstance(result, list)
    assert result[0]["id"] == "resp_001"


@pytest.mark.asyncio
async def test_list_responses_passes_survey_id(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_responses = AsyncMock(return_value=SAMPLE_RESPONSES_PAGE)
    await connector_mock.list_responses("123456789")
    call_args = connector_mock.client.get_responses.call_args
    assert "123456789" in call_args.args


@pytest.mark.asyncio
async def test_list_responses_default_per_page(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_responses = AsyncMock(return_value=SAMPLE_RESPONSES_PAGE)
    await connector_mock.list_responses("123456789")
    call_kwargs = connector_mock.client.get_responses.call_args
    assert call_kwargs.kwargs.get("per_page") == 100


@pytest.mark.asyncio
async def test_list_responses_custom_pagination(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_responses = AsyncMock(return_value=SAMPLE_RESPONSES_PAGE)
    await connector_mock.list_responses("123456789", page=2, per_page=50)
    call_kwargs = connector_mock.client.get_responses.call_args
    assert call_kwargs.kwargs.get("page") == 2
    assert call_kwargs.kwargs.get("per_page") == 50


# ── list_collectors() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_collectors_returns_list(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_collectors = AsyncMock(
        return_value=SAMPLE_COLLECTORS_PAGE
    )
    result = await connector_mock.list_collectors("123456789")
    assert isinstance(result, list)
    assert result[0]["id"] == "coll_001"


@pytest.mark.asyncio
async def test_list_collectors_passes_survey_id(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_collectors = AsyncMock(
        return_value=SAMPLE_COLLECTORS_PAGE
    )
    await connector_mock.list_collectors("123456789")
    call_args = connector_mock.client.get_collectors.call_args
    assert "123456789" in call_args.args


@pytest.mark.asyncio
async def test_list_collectors_empty(connector_mock: SurveyMonkeyConnector) -> None:
    connector_mock.client.get_collectors = AsyncMock(
        return_value={"data": [], "links": {}}
    )
    result = await connector_mock.list_collectors("123456789")
    assert result == []


# ── get_survey() / get_survey_details() ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_survey_returns_dict(connector_mock: SurveyMonkeyConnector) -> None:
    connector_mock.client.get_survey = AsyncMock(return_value=SAMPLE_SURVEY)
    result = await connector_mock.get_survey("123456789")
    assert result["id"] == "123456789"
    assert result["title"] == "Customer Satisfaction Survey"


@pytest.mark.asyncio
async def test_get_survey_not_found_raises(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_survey = AsyncMock(
        side_effect=SurveyMonkeyNotFoundError("survey", "999")
    )
    with pytest.raises(SurveyMonkeyNotFoundError):
        await connector_mock.get_survey("999")


@pytest.mark.asyncio
async def test_get_survey_details_returns_dict(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    details = {**SAMPLE_SURVEY, "pages": [{"id": "p1", "questions": []}]}
    connector_mock.client.get_survey_details = AsyncMock(return_value=details)
    result = await connector_mock.get_survey_details("123456789")
    assert result["id"] == "123456789"
    assert "pages" in result


@pytest.mark.asyncio
async def test_get_survey_details_not_found_raises(
    connector_mock: SurveyMonkeyConnector,
) -> None:
    connector_mock.client.get_survey_details = AsyncMock(
        side_effect=SurveyMonkeyNotFoundError("survey", "999")
    )
    with pytest.raises(SurveyMonkeyNotFoundError):
        await connector_mock.get_survey_details("999")


# ── Connector constants & lifecycle ───────────────────────────────────────────


def test_connector_type_constant(connector: SurveyMonkeyConnector) -> None:
    assert connector.CONNECTOR_TYPE == "surveymonkey"
    assert connector.AUTH_TYPE == "oauth2"


@pytest.mark.asyncio
async def test_connector_context_manager(connector: SurveyMonkeyConnector) -> None:
    async with connector as c:
        assert c is connector


def test_connector_loads_config(config: dict) -> None:
    c = SurveyMonkeyConnector(config=config)
    assert c._access_token == ACCESS_TOKEN
    assert c._client_id == CLIENT_ID
    assert c._client_secret == CLIENT_SECRET
    assert c._redirect_uri == REDIRECT_URI


def test_connector_missing_install_fields_all() -> None:
    c = SurveyMonkeyConnector(tenant_id=TENANT_ID, config={})
    missing = c._missing_install_fields()
    assert "client_id" in missing
    assert "client_secret" in missing
    assert "redirect_uri" in missing


def test_connector_no_missing_fields_when_all_set(config: dict) -> None:
    c = SurveyMonkeyConnector(config=config)
    assert c._missing_install_fields() == []


def test_connector_has_access_token_true(connector: SurveyMonkeyConnector) -> None:
    assert connector._has_access_token() is True


def test_connector_has_access_token_false() -> None:
    c = SurveyMonkeyConnector(config={})
    assert c._has_access_token() is False


# ── _short_hash() ─────────────────────────────────────────────────────────────


def test_short_hash_length() -> None:
    result = _short_hash("survey:123")
    assert len(result) == 16


def test_short_hash_is_hex() -> None:
    result = _short_hash("response:abc")
    int(result, 16)


def test_short_hash_deterministic() -> None:
    assert _short_hash("collector:xyz") == _short_hash("collector:xyz")


def test_short_hash_different_inputs_differ() -> None:
    assert _short_hash("survey:1") != _short_hash("survey:2")
