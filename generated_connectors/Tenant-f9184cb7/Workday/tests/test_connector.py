"""Unit tests for WorkdayConnector — all HTTP calls are mocked via AsyncMock.

Total: 65 tests covering exceptions, models, normalizers, retry logic,
HTTP client, install, health_check, sync, and list methods.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import WorkdayConnector, CONNECTOR_TYPE, AUTH_TYPE
from exceptions import (
    WorkdayAuthError,
    WorkdayError,
    WorkdayNetworkError,
    WorkdayNotFoundError,
    WorkdayRateLimitError,
)
from helpers.utils import (
    normalize_job_profile,
    normalize_location,
    normalize_organization,
    normalize_worker,
    with_retry,
    _short_hash,
    _make_id,
)
from models import AuthStatus, ConnectorHealth, SyncStatus, ConnectorDocument

# ── Test constants ─────────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_workday_test_001"
WORKDAY_TENANT = "mycompany"
BASE_URL = "https://mycompany.workday.com"

VALID_CONFIG: dict = {
    "client_id": "wday_client_id_test",
    "client_secret": "wday_client_secret_test",
    "tenant": WORKDAY_TENANT,
    "base_url": BASE_URL,
}

TOKEN_RESPONSE: dict = {
    "access_token": "eyJhbGciOiJSUzI1NiJ9.test_token",
    "token_type": "Bearer",
    "expires_in": 3600,
}

SAMPLE_WORKER: dict = {
    "id": "W001",
    "descriptor": "Jane Smith",
    "jobTitle": "Software Engineer",
    "workerType": {"descriptor": "Regular"},
    "primarySupervisoryOrg": {"descriptor": "Engineering"},
    "location": {"descriptor": "San Francisco, CA"},
    "primaryEmail": "jane.smith@mycompany.com",
    "hireDate": "2023-03-15",
    "active": "true",
}

SAMPLE_WORKER_2: dict = {
    "id": "W002",
    "descriptor": "Bob Jones",
    "jobTitle": "Product Manager",
    "workerType": {"descriptor": "Regular"},
    "primarySupervisoryOrg": {"descriptor": "Product"},
    "location": {"descriptor": "New York, NY"},
    "primaryEmail": "bob.jones@mycompany.com",
    "hireDate": "2022-07-01",
    "active": "true",
}

SAMPLE_ORG: dict = {
    "id": "ORG001",
    "descriptor": "Engineering Department",
    "orgType": {"descriptor": "Supervisory"},
    "manager": {"descriptor": "Alice Manager"},
    "topLevelOrganization": {"descriptor": "Acme Corp"},
    "memberCount": 42,
}

SAMPLE_JOB_PROFILE: dict = {
    "id": "JP001",
    "descriptor": "Software Engineer II",
    "jobFamily": {"descriptor": "Technology"},
    "managementLevel": {"descriptor": "Individual Contributor"},
    "jobLevel": {"descriptor": "Level 4"},
    "payRateType": {"descriptor": "Salary"},
    "active": "true",
    "summary": "Designs and develops software systems.",
}

SAMPLE_LOCATION: dict = {
    "id": "LOC001",
    "descriptor": "San Francisco HQ",
    "locationType": {"descriptor": "Office"},
    "addressLine1": "123 Market Street",
    "city": "San Francisco",
    "country": {"descriptor": "United States of America"},
    "timeZone": {"descriptor": "America/Los_Angeles"},
    "active": "true",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTIONS
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_workday_error_base(self):
        exc = WorkdayError("something went wrong", status_code=500, code="server_error")
        assert str(exc) == "something went wrong"
        assert exc.message == "something went wrong"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_workday_error_defaults(self):
        exc = WorkdayError("minimal error")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_workday_auth_error_is_workday_error(self):
        exc = WorkdayAuthError("auth failed", status_code=401, code="auth_error")
        assert isinstance(exc, WorkdayError)
        assert exc.status_code == 401

    def test_workday_network_error_is_workday_error(self):
        exc = WorkdayNetworkError("timeout", status_code=0)
        assert isinstance(exc, WorkdayError)

    def test_workday_not_found_error(self):
        exc = WorkdayNotFoundError("worker", "W999")
        assert isinstance(exc, WorkdayError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "W999" in str(exc)

    def test_workday_rate_limit_error(self):
        exc = WorkdayRateLimitError("too many requests", retry_after=30.0)
        assert isinstance(exc, WorkdayError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 30.0

    def test_workday_rate_limit_error_zero_retry(self):
        exc = WorkdayRateLimitError("rate limited")
        assert exc.retry_after == 0.0

    def test_exception_hierarchy(self):
        """All subclasses are catchable as WorkdayError."""
        errors = [
            WorkdayAuthError("a"),
            WorkdayNetworkError("n"),
            WorkdayNotFoundError("x", "1"),
            WorkdayRateLimitError("r"),
        ]
        for e in errors:
            assert isinstance(e, WorkdayError)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_connector_health_values(self):
        assert ConnectorHealth.HEALTHY.value == "healthy"
        assert ConnectorHealth.DEGRADED.value == "degraded"
        assert ConnectorHealth.OFFLINE.value == "offline"

    def test_auth_status_values(self):
        assert AuthStatus.CONNECTED.value == "connected"
        assert AuthStatus.FAILED.value == "failed"
        assert AuthStatus.MISSING_CREDENTIALS.value == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS.value == "invalid_credentials"

    def test_sync_status_values(self):
        assert SyncStatus.COMPLETED.value == "completed"
        assert SyncStatus.PARTIAL.value == "partial"
        assert SyncStatus.FAILED.value == "failed"
        assert SyncStatus.RUNNING.value == "running"

    def test_install_result_defaults(self):
        from models import InstallResult
        r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result_defaults(self):
        from models import HealthCheckResult
        r = HealthCheckResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.message == ""

    def test_sync_result_defaults(self):
        from models import SyncResult
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_connector_document(self):
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="content",
            connector_id="c1",
            tenant_id="t1",
            source_url="https://example.com",
            metadata={"key": "value"},
        )
        assert doc.source_id == "abc123"
        assert doc.metadata["key"] == "value"

    def test_connector_document_default_metadata(self):
        doc = ConnectorDocument(
            source_id="x", title="T", content="C", connector_id="c", tenant_id="t"
        )
        assert doc.metadata == {}
        assert doc.source_url == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZERS
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeWorker:
    def test_basic_fields(self):
        doc = normalize_worker(SAMPLE_WORKER, "conn1", "tenant1", BASE_URL)
        assert isinstance(doc, ConnectorDocument)
        assert "Jane Smith" in doc.title
        assert "Jane Smith" in doc.content
        assert "Software Engineer" in doc.content
        assert doc.connector_id == "conn1"
        assert doc.tenant_id == "tenant1"

    def test_stable_source_id(self):
        doc1 = normalize_worker(SAMPLE_WORKER, "c1", "t1")
        doc2 = normalize_worker(SAMPLE_WORKER, "c2", "t2")
        assert doc1.source_id == doc2.source_id
        expected = hashlib.sha256(b"worker:W001").hexdigest()[:16]
        assert doc1.source_id == expected

    def test_source_id_is_16_chars(self):
        doc = normalize_worker(SAMPLE_WORKER)
        assert len(doc.source_id) == 16

    def test_metadata_fields(self):
        doc = normalize_worker(SAMPLE_WORKER)
        assert doc.metadata["worker_id"] == "W001"
        assert doc.metadata["full_name"] == "Jane Smith"
        assert doc.metadata["job_title"] == "Software Engineer"
        assert doc.metadata["department"] == "Engineering"
        assert doc.metadata["email"] == "jane.smith@mycompany.com"
        assert doc.metadata["hire_date"] == "2023-03-15"

    def test_different_workers_different_source_ids(self):
        doc1 = normalize_worker(SAMPLE_WORKER)
        doc2 = normalize_worker(SAMPLE_WORKER_2)
        assert doc1.source_id != doc2.source_id

    def test_minimal_worker(self):
        raw = {"id": "W999"}
        doc = normalize_worker(raw)
        assert "W999" in doc.title
        assert doc.source_id == hashlib.sha256(b"worker:W999").hexdigest()[:16]

    def test_empty_worker(self):
        doc = normalize_worker({})
        assert isinstance(doc, ConnectorDocument)
        assert len(doc.source_id) == 16


class TestNormalizeOrganization:
    def test_basic_fields(self):
        doc = normalize_organization(SAMPLE_ORG, "conn1", "tenant1", BASE_URL)
        assert "Engineering Department" in doc.title
        assert "Engineering Department" in doc.content
        assert "Supervisory" in doc.content
        assert "Alice Manager" in doc.content

    def test_stable_source_id(self):
        doc1 = normalize_organization(SAMPLE_ORG)
        doc2 = normalize_organization(SAMPLE_ORG)
        assert doc1.source_id == doc2.source_id
        expected = hashlib.sha256(b"organization:ORG001").hexdigest()[:16]
        assert doc1.source_id == expected

    def test_source_id_16_chars(self):
        doc = normalize_organization(SAMPLE_ORG)
        assert len(doc.source_id) == 16

    def test_metadata_fields(self):
        doc = normalize_organization(SAMPLE_ORG)
        assert doc.metadata["org_id"] == "ORG001"
        assert doc.metadata["org_name"] == "Engineering Department"
        assert doc.metadata["org_type"] == "Supervisory"
        assert doc.metadata["manager"] == "Alice Manager"

    def test_minimal_org(self):
        raw = {"id": "O1", "descriptor": "My Org"}
        doc = normalize_organization(raw)
        assert "My Org" in doc.title

    def test_different_orgs_different_ids(self):
        doc1 = normalize_organization({"id": "O1", "descriptor": "Org1"})
        doc2 = normalize_organization({"id": "O2", "descriptor": "Org2"})
        assert doc1.source_id != doc2.source_id


class TestNormalizeJobProfile:
    def test_basic_fields(self):
        doc = normalize_job_profile(SAMPLE_JOB_PROFILE, "conn1", "tenant1", BASE_URL)
        assert "Software Engineer II" in doc.title
        assert "Technology" in doc.content
        assert "Individual Contributor" in doc.content

    def test_stable_source_id(self):
        doc1 = normalize_job_profile(SAMPLE_JOB_PROFILE)
        doc2 = normalize_job_profile(SAMPLE_JOB_PROFILE)
        assert doc1.source_id == doc2.source_id
        expected = hashlib.sha256(b"job_profile:JP001").hexdigest()[:16]
        assert doc1.source_id == expected

    def test_source_id_16_chars(self):
        doc = normalize_job_profile(SAMPLE_JOB_PROFILE)
        assert len(doc.source_id) == 16

    def test_metadata_fields(self):
        doc = normalize_job_profile(SAMPLE_JOB_PROFILE)
        assert doc.metadata["profile_id"] == "JP001"
        assert doc.metadata["profile_name"] == "Software Engineer II"
        assert doc.metadata["job_family"] == "Technology"
        assert doc.metadata["management_level"] == "Individual Contributor"
        assert doc.metadata["pay_rate_type"] == "Salary"
        assert "Designs and develops" in doc.metadata["summary"]

    def test_minimal_profile(self):
        raw = {"id": "JP99"}
        doc = normalize_job_profile(raw)
        assert len(doc.source_id) == 16


class TestNormalizeLocation:
    def test_basic_fields(self):
        doc = normalize_location(SAMPLE_LOCATION, "conn1", "tenant1", BASE_URL)
        assert "San Francisco HQ" in doc.title
        assert "Office" in doc.content
        assert "San Francisco" in doc.content

    def test_stable_source_id(self):
        doc1 = normalize_location(SAMPLE_LOCATION)
        doc2 = normalize_location(SAMPLE_LOCATION)
        assert doc1.source_id == doc2.source_id
        expected = hashlib.sha256(b"location:LOC001").hexdigest()[:16]
        assert doc1.source_id == expected

    def test_source_id_16_chars(self):
        doc = normalize_location(SAMPLE_LOCATION)
        assert len(doc.source_id) == 16

    def test_metadata_fields(self):
        doc = normalize_location(SAMPLE_LOCATION)
        assert doc.metadata["location_id"] == "LOC001"
        assert doc.metadata["location_name"] == "San Francisco HQ"
        assert doc.metadata["location_type"] == "Office"
        assert doc.metadata["city"] == "San Francisco"
        assert doc.metadata["country"] == "United States of America"
        assert doc.metadata["timezone"] == "America/Los_Angeles"

    def test_minimal_location(self):
        raw = {"id": "L1"}
        doc = normalize_location(raw)
        assert len(doc.source_id) == 16


# ═══════════════════════════════════════════════════════════════════════════════
# 4. WITH_RETRY
# ═══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_success_on_first_attempt(self):
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_workday_error(self):
        fn = AsyncMock(side_effect=[WorkdayError("fail"), WorkdayError("fail2"), {"data": []}])
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"data": []}
        assert fn.call_count == 3

    async def test_auth_error_not_retried(self):
        fn = AsyncMock(side_effect=WorkdayAuthError("bad creds"))
        with pytest.raises(WorkdayAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 1

    async def test_exhausts_retries_raises_last_exception(self):
        fn = AsyncMock(side_effect=WorkdayNetworkError("timeout"))
        with pytest.raises(WorkdayNetworkError, match="timeout"):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    async def test_rate_limit_retried_with_retry_after(self):
        exc = WorkdayRateLimitError("limited", retry_after=0.001)
        fn = AsyncMock(side_effect=[exc, []])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_retry(fn, max_attempts=2, base_delay=0)
        assert result == []
        mock_sleep.assert_called_once()

    async def test_rate_limit_exhausted_raises(self):
        exc = WorkdayRateLimitError("limited", retry_after=0)
        fn = AsyncMock(side_effect=[exc, exc, exc])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(WorkdayRateLimitError):
                await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    async def test_passes_args_to_fn(self):
        fn = AsyncMock(return_value="result")
        await with_retry(fn, "arg1", "arg2", kwarg1="val1")
        fn.assert_called_once_with("arg1", "arg2", kwarg1="val1")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HTTP CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkdayHTTPClient:
    def _make_client(self) -> "WorkdayHTTPClient":
        from client.http_client import WorkdayHTTPClient
        return WorkdayHTTPClient(config=VALID_CONFIG)

    def _mock_response(self, status: int, body: dict) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.headers = {}
        resp.json = AsyncMock(return_value=body)
        return resp

    async def test_authenticate_success(self):
        client = self._make_client()
        mock_resp = self._mock_response(200, TOKEN_RESPONSE)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_ctx)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("client.http_client.aiohttp.ClientSession", return_value=session_ctx):
            token = await client.authenticate()
        assert token == TOKEN_RESPONSE["access_token"]
        assert client._access_token == TOKEN_RESPONSE["access_token"]

    async def test_authenticate_401_raises_auth_error(self):
        client = self._make_client()
        mock_resp = self._mock_response(401, {"error": "invalid_client", "error_description": "Bad credentials"})
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_ctx)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("client.http_client.aiohttp.ClientSession", return_value=session_ctx):
            with pytest.raises(WorkdayAuthError):
                await client.authenticate()

    async def test_authenticate_missing_token_raises(self):
        client = self._make_client()
        mock_resp = self._mock_response(200, {"token_type": "Bearer"})  # no access_token
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_ctx)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("client.http_client.aiohttp.ClientSession", return_value=session_ctx):
            with pytest.raises(WorkdayAuthError, match="access_token"):
                await client.authenticate()

    async def test_get_workers_returns_list(self):
        client = self._make_client()
        client._access_token = "test_token"
        workers_response = {"data": [SAMPLE_WORKER, SAMPLE_WORKER_2], "total": 2}
        mock_resp = self._mock_response(200, workers_response)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_ctx)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("client.http_client.aiohttp.ClientSession", return_value=session_ctx):
            result = await client.get_workers()
        assert len(result) == 2
        assert result[0]["id"] == "W001"

    async def test_get_organizations_returns_list(self):
        client = self._make_client()
        client._access_token = "test_token"
        resp_body = {"data": [SAMPLE_ORG], "total": 1}
        mock_resp = self._mock_response(200, resp_body)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_ctx)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("client.http_client.aiohttp.ClientSession", return_value=session_ctx):
            result = await client.get_organizations()
        assert len(result) == 1
        assert result[0]["id"] == "ORG001"

    async def test_get_job_profiles_returns_list(self):
        client = self._make_client()
        client._access_token = "test_token"
        resp_body = {"data": [SAMPLE_JOB_PROFILE], "total": 1}
        mock_resp = self._mock_response(200, resp_body)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_ctx)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("client.http_client.aiohttp.ClientSession", return_value=session_ctx):
            result = await client.get_job_profiles()
        assert len(result) == 1
        assert result[0]["id"] == "JP001"

    async def test_get_locations_returns_list(self):
        client = self._make_client()
        client._access_token = "test_token"
        resp_body = {"data": [SAMPLE_LOCATION], "total": 1}
        mock_resp = self._mock_response(200, resp_body)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_ctx)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("client.http_client.aiohttp.ClientSession", return_value=session_ctx):
            result = await client.get_locations()
        assert len(result) == 1
        assert result[0]["id"] == "LOC001"

    def test_raise_for_status_401(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        with pytest.raises(WorkdayAuthError):
            client._raise_for_status(401, {"error": "unauthorized"})

    def test_raise_for_status_403(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        with pytest.raises(WorkdayAuthError):
            client._raise_for_status(403, {"error": "forbidden"})

    def test_raise_for_status_404(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        with pytest.raises(WorkdayNotFoundError):
            client._raise_for_status(404, {})

    def test_raise_for_status_429(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        with pytest.raises(WorkdayRateLimitError):
            client._raise_for_status(429, {"error": "rate_limit"})

    def test_raise_for_status_500(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        with pytest.raises(WorkdayNetworkError):
            client._raise_for_status(500, {"error": "server error"})

    def test_raise_for_status_503(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        with pytest.raises(WorkdayNetworkError):
            client._raise_for_status(503, {})

    def test_raise_for_status_400(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        with pytest.raises(WorkdayError):
            client._raise_for_status(400, {"error": "bad_request"})

    def test_token_url_format(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        # base_url = "https://mycompany.workday.com" → hostname = "mycompany.workday.com"
        assert client._token_url() == f"https://mycompany.workday.com/ccx/oauth2/mycompany/token"

    def test_token_url_format_with_hostname_key(self):
        from client.http_client import WorkdayHTTPClient
        config = {
            "client_id": "cid",
            "client_secret": "cs",
            "hostname": "wd2-impl-services1.workday.com",
            "tenant": "acme",
        }
        client = WorkdayHTTPClient(config=config)
        assert client._token_url() == "https://wd2-impl-services1.workday.com/ccx/oauth2/acme/token"

    def test_api_base_format(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        assert client._api_base() == f"{BASE_URL}/ccx/api/v1/{WORKDAY_TENANT}"

    def test_api_base_format_with_hostname_key(self):
        from client.http_client import WorkdayHTTPClient
        config = {
            "client_id": "cid",
            "client_secret": "cs",
            "hostname": "wd2-impl-services1.workday.com",
            "tenant": "acme",
        }
        client = WorkdayHTTPClient(config=config)
        assert client._api_base() == "https://wd2-impl-services1.workday.com/ccx/api/v1/acme"

    async def test_get_worker_single_record(self):
        client = self._make_client()
        client._access_token = "test_token"
        resp_body = SAMPLE_WORKER
        mock_resp = self._mock_response(200, resp_body)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_ctx)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("client.http_client.aiohttp.ClientSession", return_value=session_ctx):
            result = await client.get_worker("W001")
        assert result["id"] == "W001"
        assert result["descriptor"] == "Jane Smith"

    async def test_ensure_token_fetches_if_missing(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        client.authenticate = AsyncMock(return_value="new_token")
        client._access_token = ""
        # Mock authenticate to set the token
        async def mock_auth():
            client._access_token = "new_token"
            return "new_token"
        client.authenticate = mock_auth
        token = await client._ensure_token()
        assert token == "new_token"

    async def test_ensure_token_returns_cached(self):
        from client.http_client import WorkdayHTTPClient
        client = WorkdayHTTPClient(config=VALID_CONFIG)
        client._access_token = "cached_token"
        client.authenticate = AsyncMock()
        token = await client._ensure_token()
        assert token == "cached_token"
        client.authenticate.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CONNECTOR — INSTALL
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    def _make_connector(self, config: dict | None = None) -> WorkdayConnector:
        return WorkdayConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config if config is not None else VALID_CONFIG,
        )

    async def test_install_success(self):
        conn = self._make_connector()
        with patch("connector.WorkdayHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.authenticate = AsyncMock(return_value="tok")
            instance.get_workers = AsyncMock(return_value=[SAMPLE_WORKER])
            result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "mycompany" in result.message

    async def test_install_missing_client_id(self):
        config = {**VALID_CONFIG}
        del config["client_id"]
        conn = self._make_connector(config)
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in result.message

    async def test_install_missing_client_secret(self):
        config = {**VALID_CONFIG}
        del config["client_secret"]
        conn = self._make_connector(config)
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_secret" in result.message

    async def test_install_missing_tenant(self):
        config = {**VALID_CONFIG}
        del config["tenant"]
        conn = self._make_connector(config)
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "tenant" in result.message

    async def test_install_missing_hostname(self):
        # Config with neither 'hostname' nor 'base_url' → should fail with missing hostname
        config = {
            "client_id": VALID_CONFIG["client_id"],
            "client_secret": VALID_CONFIG["client_secret"],
            "tenant": VALID_CONFIG["tenant"],
        }
        conn = self._make_connector(config)
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "hostname" in result.message

    async def test_install_auth_error(self):
        conn = self._make_connector()
        with patch("connector.WorkdayHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.authenticate = AsyncMock(side_effect=WorkdayAuthError("invalid client"))
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self):
        conn = self._make_connector()
        with patch("connector.WorkdayHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.authenticate = AsyncMock(side_effect=WorkdayNetworkError("timeout"))
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 7. CONNECTOR — HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    def _make_connector(self) -> WorkdayConnector:
        return WorkdayConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)

    async def test_health_check_healthy(self):
        conn = self._make_connector()
        with patch("connector.WorkdayHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.authenticate = AsyncMock(return_value="tok")
            instance.get_workers = AsyncMock(return_value=[SAMPLE_WORKER])
            result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "mycompany" in result.message

    async def test_health_check_auth_error(self):
        conn = self._make_connector()
        with patch("connector.WorkdayHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.authenticate = AsyncMock(side_effect=WorkdayAuthError("expired"))
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self):
        conn = self._make_connector()
        with patch("connector.WorkdayHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.authenticate = AsyncMock(side_effect=WorkdayNetworkError("no route"))
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_missing_credentials(self):
        conn = WorkdayConnector(tenant_id=TENANT_ID, config={})
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════════
# 8. CONNECTOR — SYNC
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _make_connector(self) -> WorkdayConnector:
        return WorkdayConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)

    async def test_sync_success_all_resources(self):
        conn = self._make_connector()
        conn.client.authenticate = AsyncMock(return_value="tok")
        conn.client.get_workers = AsyncMock(return_value=[SAMPLE_WORKER, SAMPLE_WORKER_2])
        conn.client.get_organizations = AsyncMock(return_value=[SAMPLE_ORG])
        conn.client.get_job_profiles = AsyncMock(return_value=[SAMPLE_JOB_PROFILE])
        conn.client.get_locations = AsyncMock(return_value=[SAMPLE_LOCATION])
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 5  # 2 workers + 1 org + 1 profile + 1 location
        assert result.documents_synced == 5
        assert result.documents_failed == 0

    async def test_sync_empty_tenant(self):
        conn = self._make_connector()
        conn.client.authenticate = AsyncMock(return_value="tok")
        conn.client.get_workers = AsyncMock(return_value=[])
        conn.client.get_organizations = AsyncMock(return_value=[])
        conn.client.get_job_profiles = AsyncMock(return_value=[])
        conn.client.get_locations = AsyncMock(return_value=[])
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_auth_failure_returns_failed(self):
        conn = self._make_connector()
        conn.client.authenticate = AsyncMock(side_effect=WorkdayAuthError("bad token"))
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_worker_resource_failure_is_non_fatal(self):
        conn = self._make_connector()
        conn.client.authenticate = AsyncMock(return_value="tok")
        conn.client.get_workers = AsyncMock(side_effect=WorkdayNetworkError("timeout"))
        conn.client.get_organizations = AsyncMock(return_value=[SAMPLE_ORG])
        conn.client.get_job_profiles = AsyncMock(return_value=[])
        conn.client.get_locations = AsyncMock(return_value=[])
        result = await conn.sync()
        # Workers failed but org still synced — sync should complete or partial
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)
        assert result.documents_synced >= 1

    async def test_sync_with_kb_id_calls_ingest(self):
        conn = self._make_connector()
        conn.client.authenticate = AsyncMock(return_value="tok")
        conn.client.get_workers = AsyncMock(return_value=[SAMPLE_WORKER])
        conn.client.get_organizations = AsyncMock(return_value=[])
        conn.client.get_job_profiles = AsyncMock(return_value=[])
        conn.client.get_locations = AsyncMock(return_value=[])
        conn._ingest_document = AsyncMock()
        result = await conn.sync(kb_id="kb_hr")
        assert conn._ingest_document.called
        assert result.documents_synced == 1

    async def test_sync_partial_on_normalize_failure(self):
        conn = self._make_connector()
        conn.client.authenticate = AsyncMock(return_value="tok")
        # Return a worker that will successfully normalize and one broken one (covered by except)
        conn.client.get_workers = AsyncMock(return_value=[SAMPLE_WORKER])
        conn.client.get_organizations = AsyncMock(return_value=[])
        conn.client.get_job_profiles = AsyncMock(return_value=[])
        conn.client.get_locations = AsyncMock(return_value=[])
        # Patch normalize_worker to raise on second call
        original_norm = normalize_worker
        call_count = [0]

        def norm_side_effect(*a, **kw):
            call_count[0] += 1
            raise ValueError("broken")

        with patch("connector.normalize_worker", side_effect=norm_side_effect):
            result = await conn.sync()
        assert result.documents_failed == 1
        assert result.status == SyncStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════════════
# 9. CONNECTOR — LIST METHODS
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    def _make_connector(self) -> WorkdayConnector:
        return WorkdayConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)

    async def test_list_workers(self):
        conn = self._make_connector()
        conn.client.get_workers = AsyncMock(return_value=[SAMPLE_WORKER, SAMPLE_WORKER_2])
        result = await conn.list_workers()
        assert len(result) == 2
        assert result[0]["id"] == "W001"

    async def test_list_organizations(self):
        conn = self._make_connector()
        conn.client.get_organizations = AsyncMock(return_value=[SAMPLE_ORG])
        result = await conn.list_organizations()
        assert len(result) == 1
        assert result[0]["id"] == "ORG001"

    async def test_list_job_profiles(self):
        conn = self._make_connector()
        conn.client.get_job_profiles = AsyncMock(return_value=[SAMPLE_JOB_PROFILE])
        result = await conn.list_job_profiles()
        assert len(result) == 1
        assert result[0]["id"] == "JP001"

    async def test_list_locations(self):
        conn = self._make_connector()
        conn.client.get_locations = AsyncMock(return_value=[SAMPLE_LOCATION])
        result = await conn.list_locations()
        assert len(result) == 1
        assert result[0]["id"] == "LOC001"

    async def test_list_workers_propagates_error(self):
        conn = self._make_connector()
        conn.client.get_workers = AsyncMock(side_effect=WorkdayAuthError("denied"))
        with pytest.raises(WorkdayAuthError):
            await conn.list_workers()


# ═══════════════════════════════════════════════════════════════════════════════
# 10. CONNECTOR — MODULE-LEVEL CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorConstants:
    def test_connector_type(self):
        assert CONNECTOR_TYPE == "workday"

    def test_auth_type(self):
        assert AUTH_TYPE == "api_key"

    def test_class_connector_type(self):
        assert WorkdayConnector.CONNECTOR_TYPE == "workday"

    def test_class_auth_type(self):
        assert WorkdayConnector.AUTH_TYPE == "api_key"

    def test_short_hash_length(self):
        h = _short_hash("test_value")
        assert len(h) == 16

    def test_short_hash_deterministic(self):
        assert _short_hash("worker:W001") == _short_hash("worker:W001")

    def test_short_hash_unique(self):
        assert _short_hash("worker:W001") != _short_hash("worker:W002")

    def test_make_id_length(self):
        result = _make_id("worker", "W001")
        assert len(result) == 16

    def test_make_id_deterministic(self):
        assert _make_id("worker", "W001") == _make_id("worker", "W001")

    def test_make_id_equivalent_to_short_hash(self):
        assert _make_id("worker", "W001") == _short_hash("worker:W001")

    def test_make_id_unique_across_prefixes(self):
        assert _make_id("worker", "001") != _make_id("organization", "001")
