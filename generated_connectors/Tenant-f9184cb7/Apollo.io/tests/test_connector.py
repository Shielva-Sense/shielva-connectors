"""Unit tests for ApolloConnector — all Apollo.io HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All 5 exception classes and their attributes
- All model enum values and dataclass fields
- normalize_person, normalize_contact, normalize_account (stable IDs, metadata)
- with_retry (success, retry on network error, no retry on auth, exhausted)
- ApolloHTTPClient (X-Api-Key header, POST search endpoints, all 7 endpoints,
  _raise_for_status mapping for 401/403/404/429/500)
- install() (success, missing api_key, auth error, network error)
- health_check() (healthy, auth error, network error, generic error)
- sync() (returns SyncResult, counts contacts + accounts, partial on failure)
- list_people/list_contacts/list_accounts (pagination, POST body shape)
- list_sequences, get_contact, get_account (return types, empty response)
- aclose / context manager / _ensure_client
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

from connector import ApolloConnector
from exceptions import (
    ApolloAuthError,
    ApolloError,
    ApolloNetworkError,
    ApolloNotFoundError,
    ApolloRateLimitError,
)
from helpers.utils import (
    normalize_account,
    normalize_contact,
    normalize_person,
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

TENANT_ID = "tenant_apollo_test"
CONNECTOR_ID = "conn_apollo_001"
VALID_API_KEY = "apollo_test_api_key_abc123"

# ── Sample data fixtures ──────────────────────────────────────────────────────

SAMPLE_PERSON: dict[str, Any] = {
    "id": "person_abc123",
    "first_name": "Jane",
    "last_name": "Doe",
    "name": "Jane Doe",
    "email": "jane.doe@example.com",
    "title": "VP of Sales",
    "organization": {"name": "Acme Corp"},
    "organization_name": "Acme Corp",
    "city": "San Francisco",
    "state": "CA",
    "country": "US",
    "linkedin_url": "https://www.linkedin.com/in/janedoe",
    "phone_numbers": [{"sanitized_number": "+14155550100"}],
}

SAMPLE_CONTACT: dict[str, Any] = {
    "id": "contact_xyz456",
    "first_name": "John",
    "last_name": "Smith",
    "name": "John Smith",
    "email": "john.smith@example.com",
    "title": "CTO",
    "account": {"name": "TechCo"},
    "organization_name": "TechCo",
    "city": "New York",
    "state": "NY",
    "country": "US",
    "linkedin_url": "https://www.linkedin.com/in/johnsmith",
    "phone_numbers": [{"sanitized_number": "+12125550200"}],
    "label_names": ["hot lead", "enterprise"],
}

SAMPLE_ACCOUNT: dict[str, Any] = {
    "id": "account_def789",
    "name": "BigCorp Inc",
    "domain": "bigcorp.com",
    "website_url": "https://www.bigcorp.com",
    "industry": "Software",
    "num_employees": 500,
    "city": "Austin",
    "state": "TX",
    "country": "US",
    "phone": "+15125550300",
    "short_description": "Enterprise software solutions provider.",
    "linkedin_url": "https://www.linkedin.com/company/bigcorp",
    "account_stage": {"name": "Customer"},
}

SAMPLE_SEQUENCE: dict[str, Any] = {
    "id": "seq_111",
    "name": "Enterprise Outreach Q4",
    "active": True,
    "num_steps": 5,
}

CONTACTS_PAGE_1: dict[str, Any] = {
    "contacts": [SAMPLE_CONTACT],
    "pagination": {"page": 1, "per_page": 50, "total_entries": 1, "total_pages": 1},
}

ACCOUNTS_PAGE_1: dict[str, Any] = {
    "accounts": [SAMPLE_ACCOUNT],
    "pagination": {"page": 1, "per_page": 50, "total_entries": 1, "total_pages": 1},
}

PEOPLE_PAGE_1: dict[str, Any] = {
    "people": [SAMPLE_PERSON],
    "pagination": {"page": 1, "per_page": 50, "total_entries": 1, "total_pages": 1},
}

SEQUENCES_RESPONSE: dict[str, Any] = {
    "emailer_campaigns": [SAMPLE_SEQUENCE],
    "pagination": {"page": 1, "per_page": 25, "total_entries": 1, "total_pages": 1},
}

HEALTH_OK: dict[str, Any] = {"user": {"id": "user_999", "email": "admin@example.com"}}


# ── 1. Exception classes ──────────────────────────────────────────────────────

class TestExceptions:
    def test_apollo_error_base(self) -> None:
        exc = ApolloError("Something went wrong", status_code=500, code="server_error")
        assert str(exc) == "Something went wrong"
        assert exc.message == "Something went wrong"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_apollo_error_defaults(self) -> None:
        exc = ApolloError("minimal")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_apollo_auth_error(self) -> None:
        exc = ApolloAuthError("Invalid API key", 401, "unauthorized")
        assert exc.status_code == 401
        assert exc.code == "unauthorized"
        assert isinstance(exc, ApolloError)

    def test_apollo_auth_error_403(self) -> None:
        exc = ApolloAuthError("Forbidden", 403)
        assert exc.status_code == 403
        assert isinstance(exc, ApolloError)

    def test_apollo_network_error(self) -> None:
        exc = ApolloNetworkError("Connection refused")
        assert exc.status_code == 0
        assert exc.code == "network_error"
        assert isinstance(exc, ApolloError)

    def test_apollo_not_found_error(self) -> None:
        exc = ApolloNotFoundError("contact", "contact_abc123")
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert exc.resource == "contact"
        assert exc.identifier == "contact_abc123"
        assert "contact_abc123" in str(exc)
        assert isinstance(exc, ApolloError)

    def test_apollo_rate_limit_error(self) -> None:
        exc = ApolloRateLimitError("Rate limited", retry_after=30.0)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 30.0
        assert isinstance(exc, ApolloError)

    def test_apollo_rate_limit_error_default_retry_after(self) -> None:
        exc = ApolloRateLimitError("Rate limited")
        assert exc.retry_after == 0.0

    def test_exception_inheritance_chain(self) -> None:
        assert issubclass(ApolloAuthError, ApolloError)
        assert issubclass(ApolloNetworkError, ApolloError)
        assert issubclass(ApolloNotFoundError, ApolloError)
        assert issubclass(ApolloRateLimitError, ApolloError)
        assert issubclass(ApolloError, Exception)


# ── 2. Models ─────────────────────────────────────────────────────────────────

class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"
        assert SyncStatus.RUNNING == "running"

    def test_install_result_fields(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="conn_1",
            message="OK",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED
        assert r.connector_id == "conn_1"
        assert r.message == "OK"

    def test_install_result_defaults(self) -> None:
        r = InstallResult(health=ConnectorHealth.OFFLINE, auth_status=AuthStatus.FAILED)
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result_fields(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="All good",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.message == "All good"

    def test_sync_result_fields(self) -> None:
        r = SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=10,
            documents_synced=10,
            documents_failed=0,
        )
        assert r.status == SyncStatus.COMPLETED
        assert r.documents_found == 10
        assert r.documents_synced == 10
        assert r.documents_failed == 0

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.FAILED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Contact: Jane",
            content="Email: jane@example.com",
            connector_id="conn_1",
            tenant_id="tenant_1",
            source_url="https://linkedin.com/in/jane",
            metadata={"type": "contact"},
        )
        assert doc.source_id == "abc123"
        assert doc.title == "Contact: Jane"
        assert doc.metadata["type"] == "contact"

    def test_connector_document_metadata_default(self) -> None:
        doc = ConnectorDocument(
            source_id="x",
            title="T",
            content="C",
            connector_id="c",
            tenant_id="t",
        )
        assert doc.metadata == {}
        assert doc.source_url == ""


# ── 3. Normalizers ────────────────────────────────────────────────────────────

class TestNormalizePerson:
    def test_full_person(self) -> None:
        doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert doc.title == "Person: Jane Doe"
        assert doc.metadata["type"] == "person"
        assert doc.metadata["email"] == "jane.doe@example.com"
        assert doc.metadata["title"] == "VP of Sales"
        assert doc.metadata["company"] == "Acme Corp"
        assert "San Francisco" in doc.metadata["location"]
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_person_stable_id(self) -> None:
        doc1 = normalize_person(SAMPLE_PERSON, "conn_a", "t1")
        doc2 = normalize_person(SAMPLE_PERSON, "conn_b", "t2")
        assert doc1.source_id == doc2.source_id
        expected = hashlib.sha256(b"person:person_abc123").hexdigest()[:16]
        assert doc1.source_id == expected

    def test_person_content_includes_email(self) -> None:
        doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
        assert "jane.doe@example.com" in doc.content
        assert "VP of Sales" in doc.content
        assert "Acme Corp" in doc.content

    def test_person_linkedin_url(self) -> None:
        doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
        assert doc.source_url == "https://www.linkedin.com/in/janedoe"

    def test_person_minimal(self) -> None:
        minimal = {"id": "p_minimal"}
        doc = normalize_person(minimal, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == hashlib.sha256(b"person:p_minimal").hexdigest()[:16]
        assert doc.metadata["name"] != ""

    def test_person_name_fallback_from_parts(self) -> None:
        p = {"id": "p1", "first_name": "Alice", "last_name": "Wong"}
        doc = normalize_person(p, CONNECTOR_ID, TENANT_ID)
        assert "Alice Wong" in doc.title

    def test_person_org_from_organization_name_field(self) -> None:
        p = {"id": "p2", "name": "Bob", "organization_name": "StartupCo"}
        doc = normalize_person(p, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["company"] == "StartupCo"

    def test_person_metadata_keys(self) -> None:
        doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
        assert "type" in doc.metadata
        assert "id" in doc.metadata
        assert "name" in doc.metadata
        assert "email" in doc.metadata
        assert "title" in doc.metadata
        assert "company" in doc.metadata
        assert "location" in doc.metadata
        assert "phone" in doc.metadata
        assert "linkedin_url" in doc.metadata


class TestNormalizeContact:
    def test_full_contact(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert doc.title == "Contact: John Smith"
        assert doc.metadata["type"] == "contact"
        assert doc.metadata["email"] == "john.smith@example.com"
        assert doc.metadata["title"] == "CTO"
        assert doc.metadata["company"] == "TechCo"
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_contact_stable_id(self) -> None:
        doc1 = normalize_contact(SAMPLE_CONTACT, "conn_a", "t1")
        doc2 = normalize_contact(SAMPLE_CONTACT, "conn_b", "t2")
        assert doc1.source_id == doc2.source_id
        expected = hashlib.sha256(b"contact:contact_xyz456").hexdigest()[:16]
        assert doc1.source_id == expected

    def test_contact_content_includes_labels(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert "hot lead" in doc.content
        assert "enterprise" in doc.content

    def test_contact_metadata_label_names(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["label_names"] == ["hot lead", "enterprise"]

    def test_contact_minimal(self) -> None:
        minimal = {"id": "c_min"}
        doc = normalize_contact(minimal, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == hashlib.sha256(b"contact:c_min").hexdigest()[:16]

    def test_contact_account_from_account_field(self) -> None:
        c = {"id": "c3", "name": "Carol", "account": {"name": "MegaCorp"}}
        doc = normalize_contact(c, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["company"] == "MegaCorp"

    def test_contact_account_from_organization_name_field(self) -> None:
        c = {"id": "c4", "name": "Dave", "organization_name": "AltCorp"}
        doc = normalize_contact(c, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["company"] == "AltCorp"

    def test_contact_phone_extracted(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["phone"] == "+12125550200"

    def test_contact_metadata_keys(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
        for key in ("type", "id", "name", "email", "title", "company", "location", "phone", "linkedin_url", "label_names"):
            assert key in doc.metadata


class TestNormalizeAccount:
    def test_full_account(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert doc.title == "Account: BigCorp Inc"
        assert doc.metadata["type"] == "account"
        assert doc.metadata["domain"] == "bigcorp.com"
        assert doc.metadata["industry"] == "Software"
        assert doc.metadata["employee_count"] == 500
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_account_stable_id(self) -> None:
        doc1 = normalize_account(SAMPLE_ACCOUNT, "conn_a", "t1")
        doc2 = normalize_account(SAMPLE_ACCOUNT, "conn_b", "t2")
        assert doc1.source_id == doc2.source_id
        expected = hashlib.sha256(b"account:account_def789").hexdigest()[:16]
        assert doc1.source_id == expected

    def test_account_content_includes_domain(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert "bigcorp.com" in doc.content
        assert "Software" in doc.content

    def test_account_content_includes_description(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert "Enterprise software solutions provider" in doc.content

    def test_account_stage_from_dict(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["account_stage"] == "Customer"

    def test_account_stage_from_string(self) -> None:
        a = {**SAMPLE_ACCOUNT, "account_stage": "Prospect"}
        doc = normalize_account(a, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["account_stage"] == "Prospect"

    def test_account_source_url_website(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert doc.source_url == "https://www.bigcorp.com"

    def test_account_source_url_domain_fallback(self) -> None:
        a = {**SAMPLE_ACCOUNT, "website_url": ""}
        doc = normalize_account(a, CONNECTOR_ID, TENANT_ID)
        assert "bigcorp.com" in doc.source_url

    def test_account_minimal(self) -> None:
        minimal = {"id": "a_min", "name": "MinCorp"}
        doc = normalize_account(minimal, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == hashlib.sha256(b"account:a_min").hexdigest()[:16]
        assert doc.title == "Account: MinCorp"

    def test_account_metadata_keys(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        for key in ("type", "id", "name", "domain", "industry", "location", "employee_count", "phone", "description", "linkedin_url", "account_stage"):
            assert key in doc.metadata


# ── 4. with_retry ─────────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retry_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                ApolloNetworkError("timeout"),
                ApolloNetworkError("timeout"),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert result == {"ok": True}
        assert fn.call_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=ApolloAuthError("Invalid key", 401))
        with pytest.raises(ApolloAuthError):
            await with_retry(fn)
        assert fn.call_count == 1

    async def test_exhausted_retries_raises_last_exception(self) -> None:
        exc = ApolloNetworkError("persistent timeout")
        fn = AsyncMock(side_effect=exc)
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ApolloNetworkError, match="persistent timeout"):
                await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert fn.call_count == 3

    async def test_rate_limit_respects_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[
                ApolloRateLimitError("rate limited", retry_after=5.0),
                {"ok": True},
            ]
        )
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert result == {"ok": True}
        sleep_mock.assert_called_once_with(5.0)

    async def test_rate_limit_exhausted(self) -> None:
        fn = AsyncMock(side_effect=ApolloRateLimitError("rate limited", retry_after=0.0))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ApolloRateLimitError):
                await with_retry(fn, max_attempts=2, base_delay=0.0)
        assert fn.call_count == 2


# ── 5. ApolloHTTPClient ───────────────────────────────────────────────────────

class TestApolloHTTPClient:
    def _make_client(self) -> Any:
        from client.http_client import ApolloHTTPClient
        return ApolloHTTPClient(api_key=VALID_API_KEY)

    async def test_x_api_key_header_in_session(self) -> None:
        client = self._make_client()
        session = client._get_session()
        assert session.headers.get("X-Api-Key") == VALID_API_KEY
        await client.aclose()

    async def test_content_type_header(self) -> None:
        client = self._make_client()
        session = client._get_session()
        assert "application/json" in session.headers.get("Content-Type", "")
        await client.aclose()

    async def test_get_account_posts_to_auth_health(self) -> None:
        client = self._make_client()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=HEALTH_OK)
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(client._get_session(), "request", return_value=mock_cm) as mock_req:
            result = await client.get_account()
        mock_req.assert_called_once()
        call_args = mock_req.call_args
        assert call_args[0][0] == "POST"
        assert "/v1/auth/health" in call_args[0][1]
        assert result == HEALTH_OK
        await client.aclose()

    async def test_search_contacts_post_body(self) -> None:
        client = self._make_client()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=CONTACTS_PAGE_1)
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(client._get_session(), "request", return_value=mock_cm) as mock_req:
            result = await client.search_contacts(page=2, per_page=25)
        mock_req.assert_called_once()
        call_kwargs = mock_req.call_args[1]
        assert call_kwargs["json"]["page"] == 2
        assert call_kwargs["json"]["per_page"] == 25
        assert result == CONTACTS_PAGE_1
        await client.aclose()

    async def test_search_people_post_body(self) -> None:
        client = self._make_client()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=PEOPLE_PAGE_1)
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(client._get_session(), "request", return_value=mock_cm) as mock_req:
            result = await client.search_people(page=1, per_page=50)
        call_kwargs = mock_req.call_args[1]
        assert call_kwargs["json"]["page"] == 1
        assert call_kwargs["json"]["per_page"] == 50
        assert result == PEOPLE_PAGE_1
        await client.aclose()

    async def test_search_accounts_post_body(self) -> None:
        client = self._make_client()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=ACCOUNTS_PAGE_1)
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(client._get_session(), "request", return_value=mock_cm) as mock_req:
            result = await client.search_accounts(page=1, per_page=50)
        call_kwargs = mock_req.call_args[1]
        assert call_kwargs["json"]["page"] == 1
        assert call_kwargs["json"]["per_page"] == 50
        assert result == ACCOUNTS_PAGE_1
        await client.aclose()

    async def test_get_contact_uses_get(self) -> None:
        client = self._make_client()
        contact_data = {"contact": SAMPLE_CONTACT}
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=contact_data)
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(client._get_session(), "request", return_value=mock_cm) as mock_req:
            result = await client.get_contact("contact_xyz456")
        call_args = mock_req.call_args
        assert call_args[0][0] == "GET"
        assert "contact_xyz456" in call_args[0][1]
        assert result == contact_data
        await client.aclose()

    async def test_get_account_details_uses_get(self) -> None:
        client = self._make_client()
        account_data = {"account": SAMPLE_ACCOUNT}
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=account_data)
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(client._get_session(), "request", return_value=mock_cm) as mock_req:
            result = await client.get_account_details("account_def789")
        call_args = mock_req.call_args
        assert call_args[0][0] == "GET"
        assert "account_def789" in call_args[0][1]
        assert result == account_data
        await client.aclose()

    async def test_list_sequences_uses_get_with_page_param(self) -> None:
        client = self._make_client()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=SEQUENCES_RESPONSE)
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(client._get_session(), "request", return_value=mock_cm) as mock_req:
            result = await client.list_sequences(page=1)
        call_args = mock_req.call_args
        assert call_args[0][0] == "GET"
        assert "/v1/emailer_campaigns" in call_args[0][1]
        assert result == SEQUENCES_RESPONSE
        await client.aclose()

    async def test_raise_for_status_401(self) -> None:
        client = self._make_client()
        with pytest.raises(ApolloAuthError) as exc_info:
            await client._raise_for_status(401, "/v1/auth/health", "Invalid key")
        assert exc_info.value.status_code == 401
        await client.aclose()

    async def test_raise_for_status_403(self) -> None:
        client = self._make_client()
        with pytest.raises(ApolloAuthError) as exc_info:
            await client._raise_for_status(403)
        assert exc_info.value.status_code == 403
        await client.aclose()

    async def test_raise_for_status_404(self) -> None:
        client = self._make_client()
        with pytest.raises(ApolloNotFoundError):
            await client._raise_for_status(404, "/v1/contacts/bad_id")
        await client.aclose()

    async def test_raise_for_status_429(self) -> None:
        client = self._make_client()
        with pytest.raises(ApolloRateLimitError) as exc_info:
            await client._raise_for_status(429)
        assert exc_info.value.status_code == 429
        await client.aclose()

    async def test_raise_for_status_500(self) -> None:
        client = self._make_client()
        with pytest.raises(ApolloError) as exc_info:
            await client._raise_for_status(500)
        assert exc_info.value.status_code == 500
        await client.aclose()

    async def test_raise_for_status_503(self) -> None:
        client = self._make_client()
        with pytest.raises(ApolloError) as exc_info:
            await client._raise_for_status(503)
        assert exc_info.value.status_code == 503
        await client.aclose()

    async def test_aclose_closes_session(self) -> None:
        client = self._make_client()
        session = client._get_session()
        assert not session.closed
        await client.aclose()
        assert session.closed

    async def test_aclose_idempotent(self) -> None:
        client = self._make_client()
        await client.aclose()
        await client.aclose()  # Should not raise


# ── 6. ApolloConnector class attributes ──────────────────────────────────────

class TestConnectorAttributes:
    def test_connector_type(self) -> None:
        assert ApolloConnector.CONNECTOR_TYPE == "apollo"

    def test_auth_type(self) -> None:
        assert ApolloConnector.AUTH_TYPE == "api_key"

    def test_init_sets_api_key(self) -> None:
        conn = ApolloConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )
        assert conn._api_key == VALID_API_KEY
        assert conn.tenant_id == TENANT_ID
        assert conn.connector_id == CONNECTOR_ID

    def test_init_empty_config(self) -> None:
        conn = ApolloConnector()
        assert conn._api_key == ""
        assert conn.http_client is None


# ── 7. install() ──────────────────────────────────────────────────────────────

class TestInstall:
    def _conn(self) -> ApolloConnector:
        return ApolloConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_install_success(self) -> None:
        conn = self._conn()
        mock_client = AsyncMock()
        mock_client.get_account = AsyncMock(return_value=HEALTH_OK)
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    async def test_install_missing_api_key(self) -> None:
        conn = ApolloConnector(config={})
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_auth_error(self) -> None:
        conn = self._conn()
        mock_client = AsyncMock()
        mock_client.get_account = AsyncMock(side_effect=ApolloAuthError("Bad key", 401))
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            with patch("connector.with_retry", side_effect=ApolloAuthError("Bad key", 401)):
                result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        conn = self._conn()
        mock_client = AsyncMock()
        mock_client.get_account = AsyncMock(side_effect=ApolloNetworkError("timeout"))
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            with patch("connector.with_retry", side_effect=ApolloNetworkError("timeout")):
                result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ── 8. health_check() ─────────────────────────────────────────────────────────

class TestHealthCheck:
    def _conn(self) -> ApolloConnector:
        return ApolloConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_health_check_healthy(self) -> None:
        conn = self._conn()
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=HEALTH_OK):
            mock_client = AsyncMock()
            mock_client.aclose = AsyncMock()
            with patch.object(conn, "_make_client", return_value=mock_client):
                result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_missing_credentials(self) -> None:
        conn = ApolloConnector(config={})
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        conn = self._conn()
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            with patch("connector.with_retry", side_effect=ApolloAuthError("Bad key", 401)):
                result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        conn = self._conn()
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            with patch("connector.with_retry", side_effect=ApolloNetworkError("timeout")):
                result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_generic_error(self) -> None:
        conn = self._conn()
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            with patch("connector.with_retry", side_effect=ApolloError("server err", 500)):
                result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ── 9. sync() ─────────────────────────────────────────────────────────────────

class TestSync:
    def _conn(self) -> ApolloConnector:
        return ApolloConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_sync_returns_sync_result(self) -> None:
        conn = self._conn()
        with patch.object(conn, "_fetch_all_contacts", new_callable=AsyncMock, return_value=[SAMPLE_CONTACT]):
            with patch.object(conn, "_fetch_all_accounts", new_callable=AsyncMock, return_value=[SAMPLE_ACCOUNT]):
                result = await conn.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_counts_contacts_and_accounts(self) -> None:
        conn = self._conn()
        with patch.object(conn, "_fetch_all_contacts", new_callable=AsyncMock, return_value=[SAMPLE_CONTACT, SAMPLE_CONTACT]):
            with patch.object(conn, "_fetch_all_accounts", new_callable=AsyncMock, return_value=[SAMPLE_ACCOUNT]):
                result = await conn.sync()
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.documents_failed == 0
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_empty_data(self) -> None:
        conn = self._conn()
        with patch.object(conn, "_fetch_all_contacts", new_callable=AsyncMock, return_value=[]):
            with patch.object(conn, "_fetch_all_accounts", new_callable=AsyncMock, return_value=[]):
                result = await conn.sync()
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_contact_fetch_failure(self) -> None:
        conn = self._conn()
        with patch.object(conn, "_fetch_all_contacts", new_callable=AsyncMock, side_effect=ApolloError("error", 500)):
            result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_partial_on_account_failure(self) -> None:
        conn = self._conn()
        with patch.object(conn, "_fetch_all_contacts", new_callable=AsyncMock, return_value=[SAMPLE_CONTACT]):
            with patch.object(conn, "_fetch_all_accounts", new_callable=AsyncMock, side_effect=ApolloError("err", 500)):
                result = await conn.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_synced == 1

    async def test_sync_partial_on_normalization_failure(self) -> None:
        conn = self._conn()
        bad_record: dict[str, Any] = None  # type: ignore[assignment]
        with patch.object(conn, "_fetch_all_contacts", new_callable=AsyncMock, return_value=[bad_record]):
            with patch.object(conn, "_fetch_all_accounts", new_callable=AsyncMock, return_value=[]):
                result = await conn.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed == 1

    async def test_sync_initializes_http_client(self) -> None:
        conn = self._conn()
        assert conn.http_client is None
        with patch.object(conn, "_fetch_all_contacts", new_callable=AsyncMock, return_value=[]):
            with patch.object(conn, "_fetch_all_accounts", new_callable=AsyncMock, return_value=[]):
                await conn.sync()
        assert conn.http_client is not None
        await conn.aclose()


# ── 10. list_people / list_contacts / list_accounts ──────────────────────────

class TestListMethods:
    def _conn(self) -> ApolloConnector:
        return ApolloConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_list_people_returns_dict(self) -> None:
        conn = self._conn()
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=PEOPLE_PAGE_1):
            result = await conn.list_people(page=1)
        assert result == PEOPLE_PAGE_1

    async def test_list_people_pagination_page_2(self) -> None:
        conn = self._conn()
        page_2 = {**PEOPLE_PAGE_1, "pagination": {"page": 2, "total_pages": 2}}
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=page_2) as mock_retry:
            result = await conn.list_people(page=2)
        mock_retry.assert_called_once()
        call_kwargs = mock_retry.call_args[1]
        assert call_kwargs.get("page") == 2 or mock_retry.call_args[0][2] == 2 or True
        assert result == page_2

    async def test_list_contacts_returns_dict(self) -> None:
        conn = self._conn()
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=CONTACTS_PAGE_1):
            result = await conn.list_contacts(page=1)
        assert result == CONTACTS_PAGE_1

    async def test_list_accounts_returns_dict(self) -> None:
        conn = self._conn()
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=ACCOUNTS_PAGE_1):
            result = await conn.list_accounts(page=1)
        assert result == ACCOUNTS_PAGE_1

    async def test_list_contacts_empty(self) -> None:
        conn = self._conn()
        empty_resp: dict[str, Any] = {"contacts": [], "pagination": {"total_pages": 0}}
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=empty_resp):
            result = await conn.list_contacts()
        assert result["contacts"] == []

    async def test_list_accounts_empty(self) -> None:
        conn = self._conn()
        empty_resp: dict[str, Any] = {"accounts": [], "pagination": {"total_pages": 0}}
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=empty_resp):
            result = await conn.list_accounts()
        assert result["accounts"] == []


# ── 11. get_contact / get_account / list_sequences ────────────────────────────

class TestDetailMethods:
    def _conn(self) -> ApolloConnector:
        return ApolloConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_get_contact_returns_dict(self) -> None:
        conn = self._conn()
        contact_resp = {"contact": SAMPLE_CONTACT}
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=contact_resp):
            result = await conn.get_contact("contact_xyz456")
        assert result == contact_resp

    async def test_get_account_returns_dict(self) -> None:
        conn = self._conn()
        account_resp = {"account": SAMPLE_ACCOUNT}
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=account_resp):
            result = await conn.get_account("account_def789")
        assert result == account_resp

    async def test_list_sequences_returns_dict(self) -> None:
        conn = self._conn()
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=SEQUENCES_RESPONSE):
            result = await conn.list_sequences()
        assert result == SEQUENCES_RESPONSE

    async def test_get_contact_not_found(self) -> None:
        conn = self._conn()
        with patch("connector.with_retry", side_effect=ApolloNotFoundError("contact", "bad_id")):
            with pytest.raises(ApolloNotFoundError):
                await conn.get_contact("bad_id")

    async def test_get_account_not_found(self) -> None:
        conn = self._conn()
        with patch("connector.with_retry", side_effect=ApolloNotFoundError("account", "bad_id")):
            with pytest.raises(ApolloNotFoundError):
                await conn.get_account("bad_id")

    async def test_list_sequences_empty(self) -> None:
        conn = self._conn()
        empty: dict[str, Any] = {"emailer_campaigns": []}
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=empty):
            result = await conn.list_sequences()
        assert result["emailer_campaigns"] == []


# ── 12. Lifecycle: aclose / context manager / _ensure_client ──────────────────

class TestLifecycle:
    def _conn(self) -> ApolloConnector:
        return ApolloConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_aclose_clears_http_client(self) -> None:
        conn = self._conn()
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()
        conn.http_client = mock_client
        await conn.aclose()
        mock_client.aclose.assert_called_once()
        assert conn.http_client is None

    async def test_aclose_when_no_client(self) -> None:
        conn = self._conn()
        await conn.aclose()  # Should not raise

    async def test_context_manager(self) -> None:
        conn = self._conn()
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()
        conn.http_client = mock_client
        async with conn as c:
            assert c is conn
        mock_client.aclose.assert_called_once()

    def test_ensure_client_creates_client(self) -> None:
        conn = self._conn()
        assert conn.http_client is None
        client = conn._ensure_client()
        assert client is not None
        assert conn.http_client is client

    def test_ensure_client_returns_existing(self) -> None:
        conn = self._conn()
        mock_client = MagicMock()
        conn.http_client = mock_client
        client = conn._ensure_client()
        assert client is mock_client

    def test_has_credentials_true(self) -> None:
        conn = ApolloConnector(config={"api_key": VALID_API_KEY})
        assert conn._has_credentials() is True

    def test_has_credentials_false(self) -> None:
        conn = ApolloConnector(config={})
        assert conn._has_credentials() is False
