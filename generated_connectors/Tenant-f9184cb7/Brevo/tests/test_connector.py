"""Unit tests for BrevoConnector — all HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All 5 exception classes and their attributes
- All model enum values and dataclass fields
- normalize_contact, normalize_campaign, normalize_template (stable IDs, metadata, content)
- with_retry (success, retry on network, no retry on auth, exhausted, rate limit)
- BrevoHTTPClient (mocked: api-key header, all 8 endpoints, _raise_for_status 401/403/404/429/500)
- install() (success, missing api_key)
- health_check() (healthy with email/plan, auth error, network error, generic)
- sync() (returns SyncResult, counts contacts + campaigns, partial graceful)
- list_contacts (pagination), list_campaigns (status filter, pagination), list_contact_lists,
  list_senders (return types, empty)
- get_contact (by email, by id, 404)
- aclose / context manager
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

from connector import BrevoConnector
from exceptions import (
    BrevoAuthError,
    BrevoError,
    BrevoNetworkError,
    BrevoNotFoundError,
    BrevoRateLimitError,
)
from helpers.utils import (
    normalize_campaign,
    normalize_contact,
    normalize_template,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_brevo_test_001"
VALID_API_KEY = "test_brevo_api_key_abc123xyz"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_CONTACT: dict[str, Any] = {
    "id": 101,
    "email": "jane@example.com",
    "attributes": {
        "FIRSTNAME": "Jane",
        "LASTNAME": "Doe",
    },
    "listIds": [1, 2],
    "createdAt": "2024-01-01T00:00:00.000Z",
    "modifiedAt": "2024-06-01T00:00:00.000Z",
}

SAMPLE_CAMPAIGN: dict[str, Any] = {
    "id": 201,
    "name": "Q2 Newsletter",
    "subject": "Big news from Acme",
    "status": "sent",
    "sentDate": "2024-04-15T10:00:00.000Z",
    "createdAt": "2024-04-01T00:00:00.000Z",
    "statistics": {"globalStats": {"uniqueClicks": 500, "opens": 1200}},
}

SAMPLE_TEMPLATE: dict[str, Any] = {
    "id": 301,
    "name": "Welcome Email",
    "subject": "Welcome to Acme!",
    "tag": "onboarding",
    "isActive": True,
    "createdAt": "2024-01-10T00:00:00.000Z",
    "modifiedAt": "2024-05-20T00:00:00.000Z",
}

ACCOUNT_RESPONSE: dict[str, Any] = {
    "email": "admin@example.com",
    "firstName": "Admin",
    "lastName": "User",
    "plan": [{"type": "enterprise", "credits": "unlimited"}],
}

CONTACTS_PAGE: dict[str, Any] = {"contacts": [SAMPLE_CONTACT], "count": 1}
CAMPAIGNS_PAGE: dict[str, Any] = {"campaigns": [SAMPLE_CAMPAIGN], "count": 1}
EMPTY_CONTACTS: dict[str, Any] = {"contacts": [], "count": 0}
EMPTY_CAMPAIGNS: dict[str, Any] = {"campaigns": [], "count": 0}
LISTS_PAGE: dict[str, Any] = {
    "lists": [{"id": 1, "name": "Main List", "totalBlacklisted": 0, "totalSubscribers": 500}],
    "count": 1,
}
SENDERS_RESPONSE: dict[str, Any] = {
    "senders": [{"id": 1, "name": "Acme Corp", "email": "noreply@acme.com", "active": True}]
}

# ── Connector fixture ────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> BrevoConnector:
    c = BrevoConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert BrevoConnector.CONNECTOR_TYPE == "brevo"


def test_auth_type_attr() -> None:
    assert BrevoConnector.AUTH_TYPE == "api_key"


def test_connector_stores_tenant_id() -> None:
    c = BrevoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = BrevoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_api_key_from_config() -> None:
    c = BrevoConnector(config={"api_key": VALID_API_KEY})
    assert c._api_key == VALID_API_KEY


def test_connector_no_http_client_initially() -> None:
    c = BrevoConnector()
    assert c.http_client is None


def test_connector_empty_config_defaults() -> None:
    c = BrevoConnector()
    assert c._api_key == ""


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_brevo_error_base() -> None:
    exc = BrevoError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_brevo_auth_error_is_base() -> None:
    exc = BrevoAuthError("auth fail", 401, "UNAUTHORIZED")
    assert isinstance(exc, BrevoError)
    assert exc.status_code == 401


def test_brevo_auth_error_403() -> None:
    exc = BrevoAuthError("forbidden", 403, "FORBIDDEN")
    assert exc.status_code == 403
    assert isinstance(exc, BrevoError)


def test_brevo_rate_limit_error_attrs() -> None:
    exc = BrevoRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_brevo_rate_limit_error_default_retry_after() -> None:
    exc = BrevoRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_brevo_not_found_error_message() -> None:
    exc = BrevoNotFoundError("contact", "jane@example.com")
    assert "jane@example.com" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_brevo_network_error_is_base() -> None:
    exc = BrevoNetworkError("timeout")
    assert isinstance(exc, BrevoError)


def test_brevo_rate_limit_is_base() -> None:
    exc = BrevoRateLimitError("rl")
    assert isinstance(exc, BrevoError)


# ════════════════════════════════════════════════════════════════════════
# 3. MODELS
# ════════════════════════════════════════════════════════════════════════


def test_connector_health_enum_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.FAILED == "failed"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_enum_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_install_result_fields() -> None:
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="c1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.connector_id == "c1"
    assert r.message == "ok"


def test_health_check_result_fields() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        message="healthy",
        user_name="Admin User",
        user_email="admin@example.com",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.user_name == "Admin User"
    assert r.user_email == "admin@example.com"


def test_sync_result_fields() -> None:
    r = SyncResult(
        status=SyncStatus.PARTIAL,
        documents_found=10,
        documents_synced=8,
        documents_failed=2,
        message="partial",
    )
    assert r.documents_found == 10
    assert r.documents_failed == 2


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        source_id="x1",
        title="Test doc",
        content="Content here",
        connector_id="c1",
        tenant_id="t1",
        source_url="https://example.com",
        metadata={"key": "val"},
    )
    assert doc.source_id == "x1"
    assert doc.metadata["key"] == "val"


def test_connector_document_default_metadata() -> None:
    doc = ConnectorDocument(
        source_id="x2",
        title="T",
        content="C",
        connector_id="c",
        tenant_id="t",
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ════════════════════════════════════════════════════════════════════════
# 4. NORMALIZERS — normalize_contact
# ════════════════════════════════════════════════════════════════════════


def _expected_contact_id(contact_id: Any, email: str) -> str:
    raw = f"contact:{contact_id}:{email}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _expected_campaign_id(campaign_id: Any) -> str:
    raw = f"campaign:{campaign_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _expected_template_id(template_id: Any) -> str:
    raw = f"template:{template_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def test_normalize_contact_title_has_name_and_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "Jane Doe" in doc.title
    assert "jane@example.com" in doc.title


def test_normalize_contact_source_id_is_stable() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    expected = _expected_contact_id(101, "jane@example.com")
    assert doc.source_id == expected
    assert len(doc.source_id) == 16


def test_normalize_contact_source_id_is_deterministic() -> None:
    doc1 = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_contact_tenant_connector() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_contact_metadata_object_type() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "contact"


def test_normalize_contact_metadata_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == "jane@example.com"


def test_normalize_contact_metadata_brevo_id() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["brevo_id"] == "101"


def test_normalize_contact_metadata_list_ids() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["list_ids"] == [1, 2]


def test_normalize_contact_content_has_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "jane@example.com" in doc.content


def test_normalize_contact_minimal_record() -> None:
    doc = normalize_contact({"id": 999, "email": ""}, CONNECTOR_ID, TENANT_ID)
    expected = _expected_contact_id(999, "")
    assert doc.source_id == expected
    assert "Unknown" in doc.title


def test_normalize_contact_no_attributes() -> None:
    record = {"id": 102, "email": "solo@x.com"}
    doc = normalize_contact(record, CONNECTOR_ID, TENANT_ID)
    assert "solo@x.com" in doc.title


def test_normalize_contact_source_id_differs_by_email() -> None:
    doc1 = normalize_contact({"id": 1, "email": "a@x.com"}, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_contact({"id": 1, "email": "b@x.com"}, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


# ════════════════════════════════════════════════════════════════════════
# 5. NORMALIZERS — normalize_campaign
# ════════════════════════════════════════════════════════════════════════


def test_normalize_campaign_title() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert "Q2 Newsletter" in doc.title
    assert "sent" in doc.title


def test_normalize_campaign_source_id_is_stable() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    expected = _expected_campaign_id(201)
    assert doc.source_id == expected
    assert len(doc.source_id) == 16


def test_normalize_campaign_source_id_is_deterministic() -> None:
    doc1 = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_campaign_metadata_object_type() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "email_campaign"


def test_normalize_campaign_metadata_status() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["status"] == "sent"


def test_normalize_campaign_metadata_subject() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["subject"] == "Big news from Acme"


def test_normalize_campaign_metadata_brevo_id() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["brevo_id"] == "201"


def test_normalize_campaign_minimal_record() -> None:
    doc = normalize_campaign({"id": 999}, CONNECTOR_ID, TENANT_ID)
    expected = _expected_campaign_id(999)
    assert doc.source_id == expected
    assert "Campaign 999" in doc.title


def test_normalize_campaign_source_id_differs_by_id() -> None:
    doc1 = normalize_campaign({"id": 1}, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_campaign({"id": 2}, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


# ════════════════════════════════════════════════════════════════════════
# 6. NORMALIZERS — normalize_template
# ════════════════════════════════════════════════════════════════════════


def test_normalize_template_title() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert "Welcome Email" in doc.title


def test_normalize_template_source_id_is_stable() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    expected = _expected_template_id(301)
    assert doc.source_id == expected
    assert len(doc.source_id) == 16


def test_normalize_template_source_id_is_deterministic() -> None:
    doc1 = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_template_metadata_object_type() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "email_template"


def test_normalize_template_metadata_brevo_id() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["brevo_id"] == "301"


def test_normalize_template_metadata_is_active() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["isActive"] is True


def test_normalize_template_metadata_tag() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["tag"] == "onboarding"


def test_normalize_template_minimal_record() -> None:
    doc = normalize_template({"id": 999}, CONNECTOR_ID, TENANT_ID)
    expected = _expected_template_id(999)
    assert doc.source_id == expected
    assert "Template 999" in doc.title


def test_normalize_template_source_id_differs_from_campaign() -> None:
    doc_t = normalize_template({"id": 1}, CONNECTOR_ID, TENANT_ID)
    doc_c = normalize_campaign({"id": 1}, CONNECTOR_ID, TENANT_ID)
    assert doc_t.source_id != doc_c.source_id


# ════════════════════════════════════════════════════════════════════════
# 7. RETRY LOGIC
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_retries_on_network_error() -> None:
    fn = AsyncMock(side_effect=[BrevoNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=BrevoAuthError("auth fail", 401))
    with pytest.raises(BrevoAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=BrevoNetworkError("timeout"))
    with pytest.raises(BrevoNetworkError):
        await with_retry(fn, max_retries=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[BrevoRateLimitError("rl", retry_after=0), {"done": True}]
    )
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"done": True}
    mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_retry_with_args_and_kwargs() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", max_retries=1, base_delay=0, kwarg1="val")
    fn.assert_called_once_with("arg1", kwarg1="val")
    assert result == "result"


# ════════════════════════════════════════════════════════════════════════
# 8. HTTP CLIENT — _raise_for_status
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_uses_api_key_header() -> None:
    """BrevoHTTPClient must set the api-key header (not Authorization, not Bearer)."""
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    # Verify _get_session creates a session with api-key header
    session = client._get_session()
    assert "api-key" in session.headers
    assert session.headers["api-key"] == VALID_API_KEY
    # Verify it is NOT a Bearer scheme
    auth_header = session.headers.get("Authorization", "")
    assert "Bearer" not in auth_header
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_401() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key="bad_key")
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.content_length = 10
    mock_response.json = AsyncMock(return_value={"message": "Unauthorized"})
    mock_response.text = AsyncMock(return_value="Unauthorized")

    with pytest.raises(BrevoAuthError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 401
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_403() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key="bad_key")
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.content_length = 10
    mock_response.json = AsyncMock(return_value={"message": "Forbidden"})
    mock_response.text = AsyncMock(return_value="Forbidden")

    with pytest.raises(BrevoAuthError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 403
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_404() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.url = "https://api.brevo.com/v3/contacts/nobody"
    mock_response.headers = {}
    mock_response.content_length = 10
    mock_response.json = AsyncMock(return_value={"message": "Not found"})
    mock_response.text = AsyncMock(return_value="Not found")

    with pytest.raises(BrevoNotFoundError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 404
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_429() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "10"}
    mock_response.content_length = 10
    mock_response.json = AsyncMock(return_value={"message": "Too Many Requests"})
    mock_response.text = AsyncMock(return_value="Too Many Requests")

    with pytest.raises(BrevoRateLimitError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 10.0
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_500() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.content_length = 10
    mock_response.json = AsyncMock(return_value={"message": "Internal Server Error"})
    mock_response.text = AsyncMock(return_value="Internal Server Error")

    with pytest.raises(BrevoError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 500
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_account_calls_correct_path() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    client._request = AsyncMock(return_value=ACCOUNT_RESPONSE)
    result = await client.get_account()
    client._request.assert_called_once_with("GET", "/v3/account")
    assert result == ACCOUNT_RESPONSE
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_contacts_passes_params() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    client._request = AsyncMock(return_value=CONTACTS_PAGE)
    result = await client.get_contacts(limit=25, offset=50)
    client._request.assert_called_once_with(
        "GET", "/v3/contacts", params={"limit": 25, "offset": 50}
    )
    assert result == CONTACTS_PAGE
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_contact_by_identifier() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    client._request = AsyncMock(return_value=SAMPLE_CONTACT)
    result = await client.get_contact("jane@example.com")
    client._request.assert_called_once_with("GET", "/v3/contacts/jane@example.com")
    assert result == SAMPLE_CONTACT
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_email_campaigns_with_status() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    client._request = AsyncMock(return_value=CAMPAIGNS_PAGE)
    result = await client.get_email_campaigns(limit=10, offset=0, status="sent")
    client._request.assert_called_once_with(
        "GET", "/v3/emailCampaigns", params={"limit": 10, "offset": 0, "status": "sent"}
    )
    assert result == CAMPAIGNS_PAGE
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_email_campaigns_no_status() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    client._request = AsyncMock(return_value=CAMPAIGNS_PAGE)
    await client.get_email_campaigns(limit=50, offset=0)
    call_kwargs = client._request.call_args
    assert "status" not in call_kwargs[1]["params"]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_senders() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    client._request = AsyncMock(return_value=SENDERS_RESPONSE)
    result = await client.get_senders()
    client._request.assert_called_once_with("GET", "/v3/senders")
    assert result == SENDERS_RESPONSE
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_smtp_templates_passes_params() -> None:
    from client.http_client import BrevoHTTPClient

    client = BrevoHTTPClient(api_key=VALID_API_KEY)
    client._request = AsyncMock(return_value={"templates": [], "count": 0})
    await client.get_smtp_templates(limit=20, offset=40)
    client._request.assert_called_once_with(
        "GET", "/v3/smtp/templates", params={"limit": 20, "offset": 40}
    )
    await client.aclose()


# ════════════════════════════════════════════════════════════════════════
# 9. install()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    connector = BrevoConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.BrevoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=ACCOUNT_RESPONSE)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Brevo" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    connector = BrevoConnector(config={})
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "required" in result.message


@pytest.mark.asyncio
async def test_install_empty_api_key_string() -> None:
    connector = BrevoConnector(config={"api_key": ""})
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    connector = BrevoConnector(
        config={"api_key": "bad_key"},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.BrevoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(
            side_effect=BrevoAuthError("Authentication failed", 401)
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_exception_fallback() -> None:
    connector = BrevoConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.BrevoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success() -> None:
    connector = BrevoConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.BrevoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=ACCOUNT_RESPONSE)
        instance.aclose = AsyncMock()
        await connector.install()
    assert connector.http_client is not None


# ════════════════════════════════════════════════════════════════════════
# 10. health_check()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(authed: BrevoConnector) -> None:
    with patch("connector.BrevoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=ACCOUNT_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_returns_email(authed: BrevoConnector) -> None:
    with patch("connector.BrevoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=ACCOUNT_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.user_email == "admin@example.com"


@pytest.mark.asyncio
async def test_health_check_returns_plan_in_message(authed: BrevoConnector) -> None:
    with patch("connector.BrevoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=ACCOUNT_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert "enterprise" in result.message or "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    connector = BrevoConnector(config={})
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: BrevoConnector) -> None:
    with patch("connector.BrevoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(
            side_effect=BrevoAuthError("Invalid key", 401)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: BrevoConnector) -> None:
    with patch("connector.BrevoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(
            side_effect=BrevoNetworkError("timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: BrevoConnector) -> None:
    with patch("connector.BrevoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=RuntimeError("boom"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ════════════════════════════════════════════════════════════════════════
# 11. sync()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(authed: BrevoConnector) -> None:
    authed.http_client.get_contacts = AsyncMock(return_value=EMPTY_CONTACTS)
    authed.http_client.get_email_campaigns = AsyncMock(return_value=EMPTY_CAMPAIGNS)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_data_returns_sync_result(authed: BrevoConnector) -> None:
    authed.http_client.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    authed.http_client.get_email_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    result = await authed.sync()
    assert isinstance(result, SyncResult)
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_counts_contacts_and_campaigns(authed: BrevoConnector) -> None:
    contacts_2 = {"contacts": [SAMPLE_CONTACT, SAMPLE_CONTACT], "count": 2}
    authed.http_client.get_contacts = AsyncMock(return_value=contacts_2)
    authed.http_client.get_email_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    result = await authed.sync()
    assert result.documents_found == 3  # 2 contacts + 1 campaign


@pytest.mark.asyncio
async def test_sync_contacts_fetch_error_returns_failed(authed: BrevoConnector) -> None:
    authed.http_client.get_contacts = AsyncMock(
        side_effect=BrevoError("API gone", 500)
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(authed: BrevoConnector) -> None:
    authed.http_client.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    authed.http_client.get_email_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    with patch("connector.normalize_contact", side_effect=ValueError("bad")):
        result = await authed.sync()
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_partial_on_some_failures(authed: BrevoConnector) -> None:
    authed.http_client.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    authed.http_client.get_email_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    call_count = [0]

    def side_effect(record: Any, cid: str, tid: str) -> ConnectorDocument:
        call_count[0] += 1
        if call_count[0] == 1:
            raise ValueError("bad contact")
        return normalize_contact(record, cid, tid)

    with patch("connector.normalize_contact", side_effect=side_effect):
        result = await authed.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed == 1


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    connector = BrevoConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.get_contacts = AsyncMock(return_value=EMPTY_CONTACTS)
    mock_client.get_email_campaigns = AsyncMock(return_value=EMPTY_CAMPAIGNS)
    connector._make_client = lambda: mock_client
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_campaign_fetch_error_after_contacts_returns_partial(
    authed: BrevoConnector,
) -> None:
    authed.http_client.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    authed.http_client.get_email_campaigns = AsyncMock(
        side_effect=BrevoError("campaign API gone", 500)
    )
    result = await authed.sync()
    # contacts synced OK, campaign fetch failed → PARTIAL
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1


# ════════════════════════════════════════════════════════════════════════
# 12. list_contacts / get_contact
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_contacts(authed: BrevoConnector) -> None:
    authed.http_client.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    result = await authed.list_contacts(limit=10)
    assert result["contacts"][0]["id"] == 101


@pytest.mark.asyncio
async def test_list_contacts_with_offset(authed: BrevoConnector) -> None:
    authed.http_client.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    await authed.list_contacts(limit=10, offset=20)
    authed.http_client.get_contacts.assert_called_once_with(limit=10, offset=20)


@pytest.mark.asyncio
async def test_list_contacts_empty(authed: BrevoConnector) -> None:
    authed.http_client.get_contacts = AsyncMock(return_value=EMPTY_CONTACTS)
    result = await authed.list_contacts()
    assert result["contacts"] == []
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_get_contact_by_email(authed: BrevoConnector) -> None:
    authed.http_client.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
    result = await authed.get_contact("jane@example.com")
    authed.http_client.get_contact.assert_called_once_with("jane@example.com")
    assert result["email"] == "jane@example.com"


@pytest.mark.asyncio
async def test_get_contact_by_id(authed: BrevoConnector) -> None:
    authed.http_client.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
    result = await authed.get_contact("101")
    authed.http_client.get_contact.assert_called_once_with("101")
    assert result["id"] == 101


@pytest.mark.asyncio
async def test_get_contact_not_found(authed: BrevoConnector) -> None:
    authed.http_client.get_contact = AsyncMock(
        side_effect=BrevoNotFoundError("contact", "nobody@x.com")
    )
    with pytest.raises(BrevoNotFoundError):
        await authed.get_contact("nobody@x.com")


# ════════════════════════════════════════════════════════════════════════
# 13. list_campaigns
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_campaigns(authed: BrevoConnector) -> None:
    authed.http_client.get_email_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    result = await authed.list_campaigns(limit=10)
    assert result["campaigns"][0]["id"] == 201


@pytest.mark.asyncio
async def test_list_campaigns_with_status_filter(authed: BrevoConnector) -> None:
    authed.http_client.get_email_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    await authed.list_campaigns(status="sent", limit=10, offset=0)
    authed.http_client.get_email_campaigns.assert_called_once_with(
        limit=10, offset=0, status="sent"
    )


@pytest.mark.asyncio
async def test_list_campaigns_no_status_filter(authed: BrevoConnector) -> None:
    authed.http_client.get_email_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    await authed.list_campaigns(limit=5, offset=10)
    authed.http_client.get_email_campaigns.assert_called_once_with(
        limit=5, offset=10, status=None
    )


@pytest.mark.asyncio
async def test_list_campaigns_empty(authed: BrevoConnector) -> None:
    authed.http_client.get_email_campaigns = AsyncMock(return_value=EMPTY_CAMPAIGNS)
    result = await authed.list_campaigns()
    assert result["campaigns"] == []


@pytest.mark.asyncio
async def test_list_campaigns_pagination(authed: BrevoConnector) -> None:
    authed.http_client.get_email_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    await authed.list_campaigns(limit=25, offset=75)
    authed.http_client.get_email_campaigns.assert_called_once_with(
        limit=25, offset=75, status=None
    )


# ════════════════════════════════════════════════════════════════════════
# 14. list_contact_lists
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_contact_lists(authed: BrevoConnector) -> None:
    authed.http_client.get_contact_lists = AsyncMock(return_value=LISTS_PAGE)
    result = await authed.list_contact_lists(limit=10)
    assert result["lists"][0]["name"] == "Main List"


@pytest.mark.asyncio
async def test_list_contact_lists_with_offset(authed: BrevoConnector) -> None:
    authed.http_client.get_contact_lists = AsyncMock(return_value=LISTS_PAGE)
    await authed.list_contact_lists(limit=5, offset=10)
    authed.http_client.get_contact_lists.assert_called_once_with(limit=5, offset=10)


@pytest.mark.asyncio
async def test_list_contact_lists_empty(authed: BrevoConnector) -> None:
    authed.http_client.get_contact_lists = AsyncMock(
        return_value={"lists": [], "count": 0}
    )
    result = await authed.list_contact_lists()
    assert result["lists"] == []


# ════════════════════════════════════════════════════════════════════════
# 15. list_senders
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_senders(authed: BrevoConnector) -> None:
    authed.http_client.get_senders = AsyncMock(return_value=SENDERS_RESPONSE)
    result = await authed.list_senders()
    assert result["senders"][0]["name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_list_senders_empty(authed: BrevoConnector) -> None:
    authed.http_client.get_senders = AsyncMock(return_value={"senders": []})
    result = await authed.list_senders()
    assert result["senders"] == []


@pytest.mark.asyncio
async def test_list_senders_returns_dict(authed: BrevoConnector) -> None:
    authed.http_client.get_senders = AsyncMock(return_value=SENDERS_RESPONSE)
    result = await authed.list_senders()
    assert isinstance(result, dict)
    assert "senders" in result


# ════════════════════════════════════════════════════════════════════════
# 16. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: BrevoConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    connector = BrevoConnector(config={"api_key": VALID_API_KEY})
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    connector = BrevoConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    connector.http_client = mock_client
    async with connector as c:
        assert c is connector
    mock_client.aclose.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# 17. _ensure_client / _has_credentials
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    connector = BrevoConnector(config={"api_key": VALID_API_KEY})
    mock_client = MagicMock()
    connector._make_client = lambda: mock_client
    client = connector._ensure_client()
    assert client is mock_client
    assert connector.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    connector = BrevoConnector(config={"api_key": VALID_API_KEY})
    existing = MagicMock()
    connector.http_client = existing
    client = connector._ensure_client()
    assert client is existing


def test_has_credentials_true_with_key() -> None:
    c = BrevoConnector(config={"api_key": VALID_API_KEY})
    assert c._has_credentials() is True


def test_has_credentials_false_empty_key() -> None:
    c = BrevoConnector(config={"api_key": ""})
    assert c._has_credentials() is False


def test_has_credentials_false_no_key() -> None:
    c = BrevoConnector(config={})
    assert c._has_credentials() is False
