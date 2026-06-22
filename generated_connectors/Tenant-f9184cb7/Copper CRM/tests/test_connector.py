"""
Copper CRM connector — comprehensive test suite.
65+ tests across: exceptions, models, normalizers, retry, HTTP client,
install, health_check, sync, list methods, POST-based search pattern.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ── package under test ────────────────────────────────────────────────────────
from copper_connector.exceptions import (
    CopperAuthError,
    CopperError,
    CopperNetworkError,
    CopperNotFoundError,
    CopperRateLimitError,
)
from copper_connector.models import (
    ConnectorDocument,
    CopperCompany,
    CopperOpportunity,
    CopperPerson,
    CopperTask,
    HealthCheckResult,
    InstallResult,
    OpportunityStatus,
    ResourceType,
    SyncResult,
    SyncStatus,
    TaskStatus,
)
from copper_connector.helpers.utils import (
    normalize_company,
    normalize_opportunity,
    normalize_person,
    normalize_task,
    with_retry,
)
from copper_connector.client.http_client import CopperHTTPClient
from copper_connector.connector import AUTH_TYPE, CONNECTOR_TYPE, CopperConnector


# =============================================================================
# Fixtures
# =============================================================================

RAW_PERSON: dict[str, Any] = {
    "id": 1001,
    "name": "Alice Smith",
    "emails": [{"email": "alice@example.com", "category": "work"}],
    "phone_numbers": [{"number": "555-1234", "category": "mobile"}],
    "company_id": 200,
    "company_name": "Acme Corp",
    "title": "VP Sales",
    "details": "Key contact",
    "date_created": 1700000000,
    "date_modified": 1700001000,
    "tags": ["prospect"],
    "custom_fields": [],
}

RAW_COMPANY: dict[str, Any] = {
    "id": 200,
    "name": "Acme Corp",
    "email_domain": "acme.com",
    "phone_numbers": [{"number": "555-9000", "category": "work"}],
    "details": "Big corp",
    "date_created": 1699000000,
    "date_modified": 1699001000,
    "tags": ["enterprise"],
    "custom_fields": [],
}

RAW_OPPORTUNITY: dict[str, Any] = {
    "id": 3001,
    "name": "Big Deal",
    "status": "Open",
    "monetary_value": 50000.0,
    "company_id": 200,
    "company_name": "Acme Corp",
    "assignee_id": 42,
    "close_date": "2026-12-31",
    "details": "Major contract",
    "date_created": 1700000000,
    "date_modified": 1700002000,
    "tags": ["q4"],
    "custom_fields": [],
}

RAW_TASK: dict[str, Any] = {
    "id": 4001,
    "name": "Follow-up call",
    "status": "Open",
    "due_date": 1700100000,
    "reminder_date": 1700090000,
    "assignee_id": 42,
    "details": "Call back Alice",
    "date_created": 1700000000,
    "date_modified": 1700000500,
    "tags": [],
    "custom_fields": [],
}

GOOD_CONFIG: dict[str, Any] = {
    "api_key": "test-api-key-abc123",
    "user_email": "admin@acme.com",
}


# =============================================================================
# 1. Exceptions (5 tests)
# =============================================================================

class TestExceptions:
    def test_copper_error_base(self):
        err = CopperError("something broke", status_code=500)
        assert str(err) == "something broke"
        assert err.message == "something broke"
        assert err.status_code == 500

    def test_copper_error_no_status(self):
        err = CopperError("generic")
        assert err.status_code is None

    def test_copper_auth_error_defaults(self):
        err = CopperAuthError()
        assert err.status_code == 401
        assert "Authentication" in err.message

    def test_copper_not_found_error(self):
        err = CopperNotFoundError("person 999 not found")
        assert err.status_code == 404
        assert "person 999" in err.message

    def test_copper_rate_limit_error(self):
        err = CopperRateLimitError()
        assert err.status_code == 429

    def test_copper_network_error(self):
        err = CopperNetworkError("connection refused")
        assert "connection refused" in err.message
        assert err.status_code is None

    def test_exception_repr(self):
        err = CopperNotFoundError("oops")
        r = repr(err)
        assert "CopperNotFoundError" in r
        assert "oops" in r

    def test_exception_inheritance(self):
        assert issubclass(CopperAuthError, CopperError)
        assert issubclass(CopperNetworkError, CopperError)
        assert issubclass(CopperNotFoundError, CopperError)
        assert issubclass(CopperRateLimitError, CopperError)


# =============================================================================
# 2. Models (8 tests)
# =============================================================================

class TestModels:
    def test_install_result_success(self):
        r = InstallResult(success=True, connector_id="abc")
        d = r.to_dict()
        assert d["success"] is True
        assert d["connector_id"] == "abc"
        assert d["error"] == ""

    def test_install_result_failure(self):
        r = InstallResult(success=False, error="bad key")
        assert r.to_dict()["error"] == "bad key"

    def test_health_check_result(self):
        r = HealthCheckResult(healthy=True, message="ok", details={"x": 1})
        d = r.to_dict()
        assert d["healthy"] is True
        assert d["details"]["x"] == 1

    def test_sync_result_to_dict(self):
        r = SyncResult(
            status=SyncStatus.SUCCESS,
            total_synced=10,
            resource_counts={"people": 5, "companies": 5},
        )
        d = r.to_dict()
        assert d["status"] == "success"
        assert d["total_synced"] == 10

    def test_connector_document_to_dict(self):
        doc = ConnectorDocument(id="abc123", resource_type="person", raw={"id": 1}, display_name="Bob")
        d = doc.to_dict()
        assert d["id"] == "abc123"
        assert d["resource_type"] == "person"
        assert d["display_name"] == "Bob"

    def test_copper_person_from_raw(self):
        p = CopperPerson.from_raw(RAW_PERSON)
        assert p.id == 1001
        assert p.name == "Alice Smith"
        assert p.company_name == "Acme Corp"
        assert p.title == "VP Sales"
        assert p.tags == ["prospect"]

    def test_copper_company_from_raw(self):
        c = CopperCompany.from_raw(RAW_COMPANY)
        assert c.id == 200
        assert c.name == "Acme Corp"
        assert c.email_domain == "acme.com"

    def test_copper_opportunity_from_raw(self):
        o = CopperOpportunity.from_raw(RAW_OPPORTUNITY)
        assert o.id == 3001
        assert o.status == "Open"
        assert o.monetary_value == 50000.0

    def test_copper_task_from_raw(self):
        t = CopperTask.from_raw(RAW_TASK)
        assert t.id == 4001
        assert t.name == "Follow-up call"
        assert t.status == "Open"

    def test_enums(self):
        assert SyncStatus.SUCCESS.value == "success"
        assert ResourceType.PERSON.value == "person"
        assert OpportunityStatus.WON.value == "Won"
        assert TaskStatus.COMPLETED.value == "Completed"


# =============================================================================
# 3. Normalizers (10 tests)
# =============================================================================

def _expected_id(prefix: str, record_id: Any) -> str:
    return hashlib.sha256(f"{prefix}:{record_id}".encode()).hexdigest()[:16]


class TestNormalizers:
    def test_normalize_person_stable_id(self):
        doc = normalize_person(RAW_PERSON)
        assert doc.id == _expected_id("person", 1001)

    def test_normalize_person_resource_type(self):
        doc = normalize_person(RAW_PERSON)
        assert doc.resource_type == ResourceType.PERSON.value

    def test_normalize_person_display_name(self):
        doc = normalize_person(RAW_PERSON)
        assert doc.display_name == "Alice Smith"

    def test_normalize_person_metadata(self):
        doc = normalize_person(RAW_PERSON)
        assert doc.metadata["copper_id"] == 1001
        assert doc.metadata["primary_email"] == "alice@example.com"
        assert doc.metadata["company_name"] == "Acme Corp"

    def test_normalize_person_no_emails(self):
        raw = {**RAW_PERSON, "emails": []}
        doc = normalize_person(raw)
        assert doc.metadata["primary_email"] == ""

    def test_normalize_company_stable_id(self):
        doc = normalize_company(RAW_COMPANY)
        assert doc.id == _expected_id("company", 200)
        assert doc.resource_type == ResourceType.COMPANY.value

    def test_normalize_company_metadata(self):
        doc = normalize_company(RAW_COMPANY)
        assert doc.metadata["email_domain"] == "acme.com"
        assert doc.metadata["copper_id"] == 200

    def test_normalize_opportunity_stable_id(self):
        doc = normalize_opportunity(RAW_OPPORTUNITY)
        assert doc.id == _expected_id("opportunity", 3001)
        assert doc.resource_type == ResourceType.OPPORTUNITY.value

    def test_normalize_opportunity_metadata(self):
        doc = normalize_opportunity(RAW_OPPORTUNITY)
        assert doc.metadata["status"] == "Open"
        assert doc.metadata["monetary_value"] == 50000.0

    def test_normalize_task_stable_id(self):
        doc = normalize_task(RAW_TASK)
        assert doc.id == _expected_id("task", 4001)
        assert doc.resource_type == ResourceType.TASK.value

    def test_normalize_task_metadata(self):
        doc = normalize_task(RAW_TASK)
        assert doc.metadata["status"] == "Open"
        assert doc.metadata["due_date"] == 1700100000

    def test_normalize_person_raw_preserved(self):
        doc = normalize_person(RAW_PERSON)
        assert doc.raw["id"] == 1001

    def test_normalize_company_to_dict(self):
        doc = normalize_company(RAW_COMPANY)
        d = doc.to_dict()
        assert d["resource_type"] == "company"
        assert "metadata" in d


# =============================================================================
# 4. with_retry (7 tests)
# =============================================================================

class TestWithRetry:
    async def test_retry_succeeds_first_attempt(self):
        fn = AsyncMock(return_value="ok")
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == "ok"
        assert fn.call_count == 1

    async def test_retry_succeeds_on_second_attempt(self):
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise CopperNetworkError("timeout")
            return "ok"

        result = await with_retry(flaky, max_attempts=3, base_delay=0)
        assert result == "ok"
        assert call_count == 2

    async def test_retry_exhausts_all_attempts(self):
        fn = AsyncMock(side_effect=CopperNetworkError("always fails"))
        with pytest.raises(CopperNetworkError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    async def test_retry_skips_on_auth_error(self):
        fn = AsyncMock(side_effect=CopperAuthError("bad key"))
        with pytest.raises(CopperAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        # Must NOT retry on auth error
        assert fn.call_count == 1

    async def test_retry_handles_generic_exception(self):
        fn = AsyncMock(side_effect=ValueError("unexpected"))
        with pytest.raises(ValueError):
            await with_retry(fn, max_attempts=2, base_delay=0)
        assert fn.call_count == 2

    async def test_retry_max_attempts_1(self):
        fn = AsyncMock(side_effect=CopperRateLimitError())
        with pytest.raises(CopperRateLimitError):
            await with_retry(fn, max_attempts=1, base_delay=0)
        assert fn.call_count == 1

    async def test_retry_returns_correct_value(self):
        fn = AsyncMock(return_value={"id": 42})
        result = await with_retry(fn, max_attempts=1, base_delay=0)
        assert result == {"id": 42}


# =============================================================================
# 5. CopperHTTPClient — mocked (16 tests)
# =============================================================================

class TestCopperHTTPClient:
    """All HTTP calls are mocked — no real network."""

    def _client(self) -> CopperHTTPClient:
        return CopperHTTPClient(config=GOOD_CONFIG)

    def test_headers_contain_access_token(self):
        client = self._client()
        headers = client._get_headers()
        assert headers["X-PW-AccessToken"] == "test-api-key-abc123"

    def test_headers_contain_application(self):
        client = self._client()
        headers = client._get_headers()
        assert headers["X-PW-Application"] == "developer_api"

    def test_headers_contain_user_email(self):
        client = self._client()
        headers = client._get_headers()
        assert headers["X-PW-UserEmail"] == "admin@acme.com"

    def test_headers_contain_content_type(self):
        client = self._client()
        headers = client._get_headers()
        assert headers["Content-Type"] == "application/json"

    def test_raise_for_status_401(self):
        client = self._client()
        with pytest.raises(CopperAuthError):
            client._raise_for_status(401, "Unauthorized")

    def test_raise_for_status_403(self):
        client = self._client()
        with pytest.raises(CopperAuthError):
            client._raise_for_status(403, "Forbidden")

    def test_raise_for_status_404(self):
        client = self._client()
        with pytest.raises(CopperNotFoundError):
            client._raise_for_status(404, "Not found")

    def test_raise_for_status_429(self):
        client = self._client()
        with pytest.raises(CopperRateLimitError):
            client._raise_for_status(429, "Too many requests")

    def test_raise_for_status_500(self):
        client = self._client()
        with pytest.raises(CopperError) as exc_info:
            client._raise_for_status(500, "Server error")
        assert exc_info.value.status_code == 500

    def test_raise_for_status_200_no_raise(self):
        client = self._client()
        # Should not raise
        client._raise_for_status(200, None)

    async def test_get_account_calls_correct_endpoint(self):
        client = self._client()
        account_data = {"id": 1, "name": "My Org", "email": "me@org.com"}
        client._get = AsyncMock(return_value=account_data)
        result = await client.get_account()
        client._get.assert_called_once_with("account")
        assert result == account_data

    async def test_search_people_uses_post(self):
        client = self._client()
        client._post = AsyncMock(return_value=[RAW_PERSON])
        result = await client.search_people(page_number=1, page_size=200)
        client._post.assert_called_once_with(
            "people/search", {"page_number": 1, "page_size": 200}
        )
        assert result == [RAW_PERSON]

    async def test_search_companies_uses_post(self):
        client = self._client()
        client._post = AsyncMock(return_value=[RAW_COMPANY])
        result = await client.search_companies(page_number=2, page_size=100)
        client._post.assert_called_once_with(
            "companies/search", {"page_number": 2, "page_size": 100}
        )
        assert result == [RAW_COMPANY]

    async def test_search_opportunities_uses_post(self):
        client = self._client()
        client._post = AsyncMock(return_value=[RAW_OPPORTUNITY])
        result = await client.search_opportunities()
        client._post.assert_called_once_with(
            "opportunities/search", {"page_number": 1, "page_size": 200}
        )

    async def test_search_tasks_uses_post(self):
        client = self._client()
        client._post = AsyncMock(return_value=[RAW_TASK])
        result = await client.search_tasks()
        client._post.assert_called_once_with(
            "tasks/search", {"page_number": 1, "page_size": 200}
        )

    async def test_get_person_calls_correct_endpoint(self):
        client = self._client()
        client._get = AsyncMock(return_value=RAW_PERSON)
        result = await client.get_person(1001)
        client._get.assert_called_once_with("people/1001")
        assert result == RAW_PERSON

    async def test_search_people_returns_empty_list_on_non_list(self):
        client = self._client()
        # If Copper returns a dict instead of list (edge case), should return []
        client._post = AsyncMock(return_value={})
        result = await client.search_people()
        assert result == []

    async def test_raise_for_status_dict_body(self):
        client = self._client()
        with pytest.raises(CopperAuthError) as exc_info:
            client._raise_for_status(401, {"message": "invalid token"})
        assert "invalid token" in exc_info.value.message


# =============================================================================
# 6. Install (6 tests)
# =============================================================================

class TestInstall:
    async def test_install_success(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn.client.get_account = AsyncMock(return_value={"id": 1, "name": "Org"})
        result = await conn.install()
        assert result.success is True
        assert result.error == ""

    async def test_install_missing_api_key(self):
        conn = CopperConnector(config={"user_email": "admin@acme.com"})
        result = await conn.install()
        assert result.success is False
        assert "api_key" in result.error

    async def test_install_missing_user_email(self):
        conn = CopperConnector(config={"api_key": "key123"})
        result = await conn.install()
        assert result.success is False
        assert "user_email" in result.error

    async def test_install_auth_error(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn.client.get_account = AsyncMock(side_effect=CopperAuthError("bad key"))
        result = await conn.install()
        assert result.success is False
        assert "Authentication" in result.error

    async def test_install_network_error(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn.client.get_account = AsyncMock(side_effect=CopperNetworkError("timeout"))
        result = await conn.install()
        assert result.success is False
        assert result.error != ""

    async def test_install_returns_account_details(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        account = {"id": 99, "name": "Test Org"}
        conn.client.get_account = AsyncMock(return_value=account)
        result = await conn.install()
        assert result.details["account"] == account


# =============================================================================
# 7. Health check (5 tests)
# =============================================================================

class TestHealthCheck:
    async def test_health_check_success(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn.client.get_account = AsyncMock(return_value={"id": 1})
        result = await conn.health_check()
        assert result.healthy is True
        assert "reachable" in result.message.lower()

    async def test_health_check_auth_failure(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn.client.get_account = AsyncMock(side_effect=CopperAuthError("denied"))
        result = await conn.health_check()
        assert result.healthy is False
        assert "Authentication" in result.message

    async def test_health_check_network_error(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn.client.get_account = AsyncMock(side_effect=CopperNetworkError("unreachable"))
        result = await conn.health_check()
        assert result.healthy is False

    async def test_health_check_returns_account_details(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn.client.get_account = AsyncMock(return_value={"id": 5, "name": "Acme"})
        result = await conn.health_check()
        assert result.details["account"]["name"] == "Acme"

    async def test_health_check_rate_limit(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn.client.get_account = AsyncMock(side_effect=CopperRateLimitError())
        result = await conn.health_check()
        assert result.healthy is False


# =============================================================================
# 8. Sync (9 tests)
# =============================================================================

class TestSync:
    def _patched_connector(self, people=None, companies=None, opps=None, tasks=None):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn._paginate_people = AsyncMock(
            return_value=[normalize_person(p) for p in (people or [])]
        )
        conn._paginate_companies = AsyncMock(
            return_value=[normalize_company(c) for c in (companies or [])]
        )
        conn._paginate_opportunities = AsyncMock(
            return_value=[normalize_opportunity(o) for o in (opps or [])]
        )
        conn._paginate_tasks = AsyncMock(
            return_value=[normalize_task(t) for t in (tasks or [])]
        )
        return conn

    async def test_sync_success_empty(self):
        conn = self._patched_connector()
        result = await conn.sync()
        assert result.status == SyncStatus.SUCCESS
        assert result.total_synced == 0

    async def test_sync_counts_people(self):
        conn = self._patched_connector(people=[RAW_PERSON])
        result = await conn.sync()
        assert result.resource_counts["people"] == 1
        assert result.total_synced == 1

    async def test_sync_counts_all_resources(self):
        conn = self._patched_connector(
            people=[RAW_PERSON],
            companies=[RAW_COMPANY],
            opps=[RAW_OPPORTUNITY],
            tasks=[RAW_TASK],
        )
        result = await conn.sync()
        assert result.total_synced == 4
        assert result.resource_counts["people"] == 1
        assert result.resource_counts["companies"] == 1
        assert result.resource_counts["opportunities"] == 1
        assert result.resource_counts["tasks"] == 1

    async def test_sync_status_success(self):
        conn = self._patched_connector()
        result = await conn.sync()
        assert result.status == SyncStatus.SUCCESS

    async def test_sync_auth_error_returns_failed(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn._paginate_people = AsyncMock(side_effect=CopperAuthError("denied"))
        conn._paginate_companies = AsyncMock(return_value=[])
        conn._paginate_opportunities = AsyncMock(return_value=[])
        conn._paginate_tasks = AsyncMock(return_value=[])
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED
        assert "Authentication" in result.error

    async def test_sync_network_error_returns_failed(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn._paginate_people = AsyncMock(side_effect=CopperNetworkError("timeout"))
        conn._paginate_companies = AsyncMock(return_value=[])
        conn._paginate_opportunities = AsyncMock(return_value=[])
        conn._paginate_tasks = AsyncMock(return_value=[])
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_to_dict(self):
        conn = self._patched_connector(people=[RAW_PERSON])
        result = await conn.sync()
        d = result.to_dict()
        assert d["status"] == "success"
        assert d["total_synced"] == 1

    async def test_sync_multiple_people(self):
        person2 = {**RAW_PERSON, "id": 1002, "name": "Bob Jones"}
        conn = self._patched_connector(people=[RAW_PERSON, person2])
        result = await conn.sync()
        assert result.resource_counts["people"] == 2

    async def test_sync_details_included(self):
        conn = self._patched_connector(companies=[RAW_COMPANY])
        result = await conn.sync()
        assert "resources" in result.details


# =============================================================================
# 9. List methods (6 tests)
# =============================================================================

class TestListMethods:
    async def test_list_people_returns_dicts(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn._paginate_people = AsyncMock(return_value=[normalize_person(RAW_PERSON)])
        result = await conn.list_people()
        assert isinstance(result, list)
        assert result[0]["resource_type"] == "person"

    async def test_list_companies_returns_dicts(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn._paginate_companies = AsyncMock(return_value=[normalize_company(RAW_COMPANY)])
        result = await conn.list_companies()
        assert isinstance(result, list)
        assert result[0]["resource_type"] == "company"

    async def test_list_opportunities_returns_dicts(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn._paginate_opportunities = AsyncMock(
            return_value=[normalize_opportunity(RAW_OPPORTUNITY)]
        )
        result = await conn.list_opportunities()
        assert isinstance(result, list)
        assert result[0]["resource_type"] == "opportunity"

    async def test_list_tasks_returns_dicts(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn._paginate_tasks = AsyncMock(return_value=[normalize_task(RAW_TASK)])
        result = await conn.list_tasks()
        assert isinstance(result, list)
        assert result[0]["resource_type"] == "task"

    async def test_get_person_returns_dict(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn.client.get_person = AsyncMock(return_value=RAW_PERSON)
        result = await conn.get_person(1001)
        assert isinstance(result, dict)
        assert result["resource_type"] == "person"
        assert result["display_name"] == "Alice Smith"

    async def test_list_people_empty(self):
        conn = CopperConnector(config=GOOD_CONFIG)
        conn._paginate_people = AsyncMock(return_value=[])
        result = await conn.list_people()
        assert result == []


# =============================================================================
# 10. POST-based search pattern (4 tests)
# =============================================================================

class TestPostSearchPattern:
    """Verify the Copper POST-for-listing pattern is enforced correctly."""

    async def test_search_people_sends_page_body(self):
        client = CopperHTTPClient(config=GOOD_CONFIG)
        client._post = AsyncMock(return_value=[RAW_PERSON])
        await client.search_people(page_number=3, page_size=50)
        client._post.assert_called_once_with(
            "people/search", {"page_number": 3, "page_size": 50}
        )

    async def test_search_companies_sends_page_body(self):
        client = CopperHTTPClient(config=GOOD_CONFIG)
        client._post = AsyncMock(return_value=[])
        await client.search_companies(page_number=1, page_size=200)
        body = client._post.call_args[0][1]
        assert body["page_number"] == 1
        assert body["page_size"] == 200

    async def test_paginator_stops_on_short_page(self):
        """Pagination must stop when page returns fewer results than page_size."""
        conn = CopperConnector(config=GOOD_CONFIG)
        # Return 1 item on first call (< page_size=200) → no second call
        conn.client.search_people = AsyncMock(return_value=[RAW_PERSON])
        docs = await conn._paginate_people(page_size=200)
        assert len(docs) == 1
        conn.client.search_people.assert_called_once()

    async def test_paginator_continues_on_full_page(self):
        """Pagination must advance to next page when page is full."""
        conn = CopperConnector(config=GOOD_CONFIG)
        # First call returns page_size items, second returns 0
        full_page = [RAW_PERSON] * 2
        conn.client.search_people = AsyncMock(side_effect=[full_page, []])
        docs = await conn._paginate_people(page_size=2)
        assert len(docs) == 2
        assert conn.client.search_people.call_count == 2


# =============================================================================
# 11. Module-level constants (3 tests)
# =============================================================================

class TestConstants:
    def test_connector_type(self):
        assert CONNECTOR_TYPE == "copper"

    def test_auth_type(self):
        assert AUTH_TYPE == "api_key"

    def test_connector_class_name(self):
        conn = CopperConnector()
        assert conn.__class__.__name__ == "CopperConnector"
