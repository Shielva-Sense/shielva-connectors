"""
RingCentral connector — 67+ tests.

Groups:
  - Exceptions (5)
  - Models (8)
  - Normalize functions (10)
  - with_retry (7)
  - HTTP client mocked (16)
  - install (5)
  - health_check (5)
  - sync (8)
  - list_* methods (5)
  - authorize URL (3)
  - token refresh (3)
"""

from __future__ import annotations

import hashlib
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the package root is importable without installation
# ---------------------------------------------------------------------------
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ringcentral_connector.exceptions import (
    RingCentralAuthError,
    RingCentralError,
    RingCentralNetworkError,
    RingCentralNotFoundError,
    RingCentralRateLimitError,
)
from ringcentral_connector.helpers.utils import (
    _stable_id,
    normalize_call_log,
    normalize_contact,
    normalize_extension,
    normalize_meeting,
    normalize_message,
    with_retry,
)
from ringcentral_connector.models import (
    CallDirection,
    ConnectorDocument,
    HealthCheckResult,
    HealthStatus,
    InstallResult,
    MessageType,
    MeetingStatus,
    OAuthToken,
    PagingInfo,
    ResourceType,
    SyncResult,
    SyncStatus,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def base_config() -> dict[str, Any]:
    return {
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "server_url": "https://platform.ringcentral.com",
    }


@pytest.fixture
def connector(base_config: dict[str, Any]):
    from ringcentral_connector.connector import RingCentralConnector

    return RingCentralConnector(
        tenant_id="Tenant-test",
        connector_id="rc-test",
        config=base_config,
    )


@pytest.fixture
def http_client(base_config: dict[str, Any]):
    from ringcentral_connector.client.http_client import RingCentralHTTPClient

    return RingCentralHTTPClient(config=base_config)


@pytest.fixture
def sample_call_log() -> dict[str, Any]:
    return {
        "id": "CL001",
        "uri": "https://platform.ringcentral.com/restapi/v1.0/account/~/call-log/CL001",
        "direction": "Inbound",
        "type": "Voice",
        "startTime": "2026-06-01T10:00:00Z",
        "duration": 120,
        "result": "Accepted",
        "action": "Phone Call",
        "from": {"phoneNumber": "+15551234567", "name": "Alice"},
        "to": {"phoneNumber": "+15557654321", "name": "Bob"},
    }


@pytest.fixture
def sample_message() -> dict[str, Any]:
    return {
        "id": "MSG001",
        "uri": "https://platform.ringcentral.com/restapi/v1.0/...",
        "type": "SMS",
        "subject": "Hello",
        "direction": "Inbound",
        "readStatus": "Unread",
        "creationTime": "2026-06-01T09:00:00Z",
        "lastModifiedTime": "2026-06-01T09:01:00Z",
        "messageStatus": "Received",
        "conversationId": "CONV001",
        "from": {"phoneNumber": "+15551234567", "name": "Alice"},
        "to": [{"phoneNumber": "+15557654321", "name": "Bob"}],
    }


@pytest.fixture
def sample_extension() -> dict[str, Any]:
    return {
        "id": "EXT001",
        "uri": "https://platform.ringcentral.com/restapi/v1.0/account/~/extension/EXT001",
        "extensionNumber": "101",
        "type": "User",
        "status": "Enabled",
        "name": "Alice Smith",
        "contact": {
            "firstName": "Alice",
            "lastName": "Smith",
            "email": "alice@example.com",
            "department": "Engineering",
        },
    }


@pytest.fixture
def sample_contact() -> dict[str, Any]:
    return {
        "id": "CON001",
        "uri": "https://platform.ringcentral.com/restapi/v1.0/...",
        "firstName": "Bob",
        "lastName": "Jones",
        "company": "Acme Corp",
        "jobTitle": "Engineer",
        "phoneNumbers": [{"type": "Business", "phoneNumber": "+15551234567"}],
        "emails": [{"type": "Business", "email": "bob@acme.com"}],
        "notes": "Important client",
    }


@pytest.fixture
def sample_meeting() -> dict[str, Any]:
    return {
        "id": "MTG001",
        "uri": "https://platform.ringcentral.com/restapi/v1.0/...",
        "topic": "Weekly Standup",
        "meetingType": "Scheduled",
        "status": "NotStarted",
        "password": "secret",
        "schedule": {
            "startTime": "2026-06-10T14:00:00Z",
            "durationInMinutes": 30,
            "timeZone": {"name": "UTC"},
        },
        "host": {"id": "EXT001"},
    }


# ===========================================================================
# 1. Exceptions (5 tests)
# ===========================================================================


class TestExceptions:
    def test_base_error_inherits_exception(self):
        err = RingCentralError("oops", status_code=400)
        assert isinstance(err, Exception)
        assert err.message == "oops"
        assert err.status_code == 400

    def test_auth_error_inherits_base(self):
        err = RingCentralAuthError("auth failed", status_code=401)
        assert isinstance(err, RingCentralError)
        assert err.status_code == 401

    def test_network_error_inherits_base(self):
        err = RingCentralNetworkError("server down", status_code=503)
        assert isinstance(err, RingCentralError)

    def test_not_found_error_inherits_base(self):
        err = RingCentralNotFoundError("not found", status_code=404)
        assert isinstance(err, RingCentralError)
        assert err.status_code == 404

    def test_rate_limit_error_inherits_base(self):
        err = RingCentralRateLimitError("slow down", status_code=429)
        assert isinstance(err, RingCentralError)
        assert err.status_code == 429


# ===========================================================================
# 2. Models (8 tests)
# ===========================================================================


class TestModels:
    def test_install_result_to_dict(self):
        r = InstallResult(success=True, message="ok")
        d = r.to_dict()
        assert d["success"] is True
        assert d["connector_type"] == "ringcentral"
        assert d["auth_type"] == "oauth2"

    def test_health_check_result_healthy(self):
        r = HealthCheckResult(status=HealthStatus.HEALTHY)
        assert r.healthy is True
        assert r.to_dict()["healthy"] is True

    def test_health_check_result_unhealthy(self):
        r = HealthCheckResult(status=HealthStatus.UNHEALTHY, message="error")
        assert r.healthy is False

    def test_sync_result_to_dict(self):
        r = SyncResult(status=SyncStatus.OK, records_synced=42, resources={"call_logs": 42})
        d = r.to_dict()
        assert d["status"] == "ok"
        assert d["records_synced"] == 42

    def test_connector_document_to_dict(self):
        doc = ConnectorDocument(
            id="abc123", resource_type=ResourceType.CALL_LOG, raw={}, normalized={}
        )
        d = doc.to_dict()
        assert d["resource_type"] == "call_log"

    def test_oauth_token_from_dict(self):
        raw = {
            "access_token": "tok",
            "refresh_token": "ref",
            "expires_in": 3600,
            "scope": "ReadCallLog",
        }
        tok = OAuthToken.from_dict(raw)
        assert tok.access_token == "tok"
        assert tok.scope == "ReadCallLog"

    def test_paging_info_from_dict(self):
        raw = {"page": 2, "perPage": 100, "totalPages": 5, "totalElements": 450}
        paging = PagingInfo.from_dict(raw)
        assert paging.total_pages == 5
        assert paging.page == 2

    def test_sync_status_enum_values(self):
        assert SyncStatus.OK == "ok"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"


# ===========================================================================
# 3. Normalize functions (10 tests)
# ===========================================================================


class TestNormalizeFunctions:
    def test_stable_id_length(self):
        sid = _stable_id("call_log:", "CL001")
        assert len(sid) == 16

    def test_stable_id_deterministic(self):
        a = _stable_id("call_log:", "CL001")
        b = _stable_id("call_log:", "CL001")
        assert a == b

    def test_stable_id_different_inputs(self):
        a = _stable_id("call_log:", "CL001")
        b = _stable_id("message:", "CL001")
        assert a != b

    def test_normalize_call_log(self, sample_call_log: dict[str, Any]):
        doc = normalize_call_log(sample_call_log)
        assert doc.resource_type == ResourceType.CALL_LOG
        assert doc.normalized["direction"] == "Inbound"
        assert doc.normalized["from_name"] == "Alice"
        assert doc.normalized["duration"] == 120
        assert len(doc.id) == 16

    def test_normalize_call_log_stable_id(self, sample_call_log: dict[str, Any]):
        doc1 = normalize_call_log(sample_call_log)
        doc2 = normalize_call_log(sample_call_log)
        assert doc1.id == doc2.id

    def test_normalize_message(self, sample_message: dict[str, Any]):
        doc = normalize_message(sample_message)
        assert doc.resource_type == ResourceType.MESSAGE
        assert doc.normalized["type"] == "SMS"
        assert doc.normalized["from_name"] == "Alice"
        assert len(doc.normalized["to"]) == 1

    def test_normalize_extension(self, sample_extension: dict[str, Any]):
        doc = normalize_extension(sample_extension)
        assert doc.resource_type == ResourceType.EXTENSION
        assert doc.normalized["extension_number"] == "101"
        assert doc.normalized["first_name"] == "Alice"

    def test_normalize_contact(self, sample_contact: dict[str, Any]):
        doc = normalize_contact(sample_contact)
        assert doc.resource_type == ResourceType.CONTACT
        assert doc.normalized["first_name"] == "Bob"
        assert len(doc.normalized["phone_numbers"]) == 1
        assert len(doc.normalized["emails"]) == 1

    def test_normalize_meeting(self, sample_meeting: dict[str, Any]):
        doc = normalize_meeting(sample_meeting)
        assert doc.resource_type == ResourceType.MEETING
        assert doc.normalized["topic"] == "Weekly Standup"
        assert doc.normalized["start_time"] == "2026-06-10T14:00:00Z"
        assert doc.normalized["duration_in_minutes"] == 30

    def test_normalize_handles_missing_fields(self):
        doc = normalize_call_log({"id": "X1"})
        assert doc.normalized["direction"] == ""
        assert doc.normalized["from_number"] == ""


# ===========================================================================
# 4. with_retry (7 tests)
# ===========================================================================


class TestWithRetry:
    async def test_success_first_attempt(self):
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await with_retry(fn)
        assert result == "ok"
        assert call_count == 1

    async def test_retries_on_network_error(self):
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RingCentralNetworkError("server error", status_code=503)
            return "success"

        with patch("ringcentral_connector.helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, initial_delay=0.0)
        assert result == "success"
        assert call_count == 3

    async def test_raises_after_max_attempts(self):
        async def fn():
            raise RingCentralNetworkError("persistent error", status_code=502)

        with patch("ringcentral_connector.helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RingCentralNetworkError):
                await with_retry(fn, max_attempts=3, initial_delay=0.0)

    async def test_auth_error_not_retried(self):
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            raise RingCentralAuthError("auth failed", status_code=401)

        with pytest.raises(RingCentralAuthError):
            await with_retry(fn, max_attempts=3)
        assert call_count == 1  # No retries

    async def test_retry_with_custom_max_attempts(self):
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            raise RingCentralNetworkError("error", status_code=500)

        with patch("ringcentral_connector.helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RingCentralNetworkError):
                await with_retry(fn, max_attempts=5, initial_delay=0.0)
        assert call_count == 5

    async def test_retry_returns_value_on_second_attempt(self):
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RingCentralRateLimitError("rate limit", status_code=429)
            return {"data": "result"}

        with patch("ringcentral_connector.helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=2, initial_delay=0.0)
        assert result == {"data": "result"}

    async def test_retry_propagates_generic_exception(self):
        async def fn():
            raise ValueError("unexpected")

        with patch("ringcentral_connector.helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ValueError):
                await with_retry(fn, max_attempts=2, initial_delay=0.0)


# ===========================================================================
# 5. HTTP client mocked (16 tests)
# ===========================================================================


class TestHTTPClient:
    def test_auth_header_contains_bearer(self, http_client):
        headers = http_client._auth_headers()
        assert headers["Authorization"] == "Bearer test_access_token"

    def test_raise_for_status_401(self, http_client):
        with pytest.raises(RingCentralAuthError) as exc_info:
            http_client._raise_for_status(401, "unauthorized")
        assert exc_info.value.status_code == 401

    def test_raise_for_status_403(self, http_client):
        with pytest.raises(RingCentralAuthError) as exc_info:
            http_client._raise_for_status(403, "forbidden")
        assert exc_info.value.status_code == 403

    def test_raise_for_status_404(self, http_client):
        with pytest.raises(RingCentralNotFoundError):
            http_client._raise_for_status(404, "not found")

    def test_raise_for_status_429(self, http_client):
        with pytest.raises(RingCentralRateLimitError):
            http_client._raise_for_status(429, "rate limited")

    def test_raise_for_status_500(self, http_client):
        with pytest.raises(RingCentralNetworkError):
            http_client._raise_for_status(500, "server error")

    def test_raise_for_status_503(self, http_client):
        with pytest.raises(RingCentralNetworkError):
            http_client._raise_for_status(503, "unavailable")

    def test_raise_for_status_200_does_not_raise(self, http_client):
        # Should not raise
        http_client._raise_for_status(200, {})

    async def test_get_extension_info(self, http_client):
        mock_data = {"id": "EXT001", "extensionNumber": "101", "name": "Alice"}
        http_client._get = AsyncMock(return_value=mock_data)
        result = await http_client.get_extension_info()
        assert result["id"] == "EXT001"
        http_client._get.assert_called_once_with("/account/~/extension/~")

    async def test_get_call_logs(self, http_client):
        mock_data = {"records": [{"id": "CL001"}], "paging": {"page": 1, "totalPages": 1}}
        http_client._get = AsyncMock(return_value=mock_data)
        result = await http_client.get_call_logs(page=1, per_page=100)
        assert result["records"][0]["id"] == "CL001"

    async def test_get_call_logs_pagination(self, http_client):
        """Verify page/perPage params are passed correctly."""
        mock_data = {"records": [], "paging": {"page": 2, "totalPages": 3}}
        http_client._get = AsyncMock(return_value=mock_data)
        await http_client.get_call_logs(page=2, per_page=50)
        call_kwargs = http_client._get.call_args
        assert call_kwargs[1]["params"]["page"] == 2
        assert call_kwargs[1]["params"]["perPage"] == 50

    async def test_get_messages(self, http_client):
        mock_data = {"records": [{"id": "MSG001"}], "paging": {"totalPages": 1}}
        http_client._get = AsyncMock(return_value=mock_data)
        result = await http_client.get_messages()
        assert result["records"][0]["id"] == "MSG001"

    async def test_get_extensions(self, http_client):
        mock_data = {"records": [{"id": "EXT001"}], "paging": {"totalPages": 1}}
        http_client._get = AsyncMock(return_value=mock_data)
        result = await http_client.get_extensions()
        assert result["records"][0]["id"] == "EXT001"

    async def test_get_contacts(self, http_client):
        mock_data = {"records": [{"id": "CON001"}], "paging": {"totalPages": 1}}
        http_client._get = AsyncMock(return_value=mock_data)
        result = await http_client.get_contacts()
        assert result["records"][0]["id"] == "CON001"

    async def test_get_meetings(self, http_client):
        mock_data = {"records": [{"id": "MTG001"}], "paging": {"totalPages": 1}}
        http_client._get = AsyncMock(return_value=mock_data)
        result = await http_client.get_meetings()
        assert result["records"][0]["id"] == "MTG001"

    async def test_paginate_all_respects_total_pages(self, http_client):
        """paginate_all must fetch all pages until totalPages is reached."""
        responses = [
            {"records": [{"id": f"R{i}"}], "paging": {"page": i, "totalPages": 3}}
            for i in range(1, 4)
        ]
        call_count = 0

        async def fake_fetch(page=1, per_page=100, **kwargs):
            nonlocal call_count
            idx = call_count
            call_count += 1
            return responses[idx]

        result = await http_client.paginate_all(fake_fetch, per_page=100)
        assert len(result) == 3
        assert call_count == 3


# ===========================================================================
# 6. install (5 tests)
# ===========================================================================


class TestInstall:
    async def test_install_success_with_required_fields(self, connector):
        result = await connector.install()
        assert result.success is True
        assert result.connector_type == "ringcentral"

    async def test_install_fails_missing_client_id(self, base_config):
        from ringcentral_connector.connector import RingCentralConnector

        cfg = {**base_config, "client_id": ""}
        c = RingCentralConnector(config=cfg)
        result = await c.install()
        assert result.success is False
        assert "client_id" in result.message

    async def test_install_fails_missing_client_secret(self, base_config):
        from ringcentral_connector.connector import RingCentralConnector

        cfg = {**base_config, "client_secret": ""}
        c = RingCentralConnector(config=cfg)
        result = await c.install()
        assert result.success is False
        assert "client_secret" in result.message

    async def test_install_returns_install_fields(self, connector):
        result = await connector.install()
        keys = [f["key"] for f in result.install_fields]
        assert "client_id" in keys
        assert "client_secret" in keys
        assert "server_url" in keys

    async def test_install_result_auth_type(self, connector):
        result = await connector.install()
        assert result.auth_type == "oauth2"


# ===========================================================================
# 7. health_check (5 tests)
# ===========================================================================


class TestHealthCheck:
    async def test_health_check_healthy(self, connector):
        connector.client.get_extension_info = AsyncMock(
            return_value={"id": "EXT001", "extensionNumber": "101", "name": "Alice"}
        )
        result = await connector.health_check()
        assert result.status == HealthStatus.HEALTHY
        assert result.healthy is True
        assert result.details["extension_id"] == "EXT001"

    async def test_health_check_auth_error(self, connector):
        connector.client.get_extension_info = AsyncMock(
            side_effect=RingCentralAuthError("invalid token", status_code=401)
        )
        result = await connector.health_check()
        assert result.status == HealthStatus.UNHEALTHY
        assert "Auth error" in result.message

    async def test_health_check_network_error(self, connector):
        connector.client.get_extension_info = AsyncMock(
            side_effect=RingCentralNetworkError("server down", status_code=503)
        )
        result = await connector.health_check()
        assert result.status == HealthStatus.UNHEALTHY

    async def test_health_check_unexpected_error(self, connector):
        connector.client.get_extension_info = AsyncMock(
            side_effect=RuntimeError("unexpected")
        )
        result = await connector.health_check()
        assert result.status == HealthStatus.UNHEALTHY
        assert "Unexpected error" in result.message

    async def test_health_check_returns_extension_name(self, connector):
        connector.client.get_extension_info = AsyncMock(
            return_value={"id": "EXT001", "extensionNumber": "200", "name": "Bob Smith"}
        )
        result = await connector.health_check()
        assert result.details["name"] == "Bob Smith"


# ===========================================================================
# 8. sync (8 tests)
# ===========================================================================


def _make_doc(resource_type) -> ConnectorDocument:
    return ConnectorDocument(id="abc", resource_type=resource_type, raw={}, normalized={})


class TestSync:
    async def test_sync_ok_all_resources(self, connector):
        connector.list_call_logs = AsyncMock(return_value=[_make_doc(ResourceType.CALL_LOG)] * 2)
        connector.list_messages = AsyncMock(return_value=[_make_doc(ResourceType.MESSAGE)] * 3)
        connector.list_extensions = AsyncMock(return_value=[_make_doc(ResourceType.EXTENSION)])
        connector.list_contacts = AsyncMock(return_value=[_make_doc(ResourceType.CONTACT)] * 4)
        connector.list_meetings = AsyncMock(return_value=[_make_doc(ResourceType.MEETING)])
        result = await connector.sync()
        assert result.status == SyncStatus.OK
        assert result.records_synced == 11

    async def test_sync_partial_on_one_error(self, connector):
        connector.list_call_logs = AsyncMock(return_value=[_make_doc(ResourceType.CALL_LOG)])
        connector.list_messages = AsyncMock(
            side_effect=RingCentralNetworkError("fail", status_code=503)
        )
        connector.list_extensions = AsyncMock(return_value=[])
        connector.list_contacts = AsyncMock(return_value=[])
        connector.list_meetings = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert len(result.errors) == 1

    async def test_sync_failed_when_all_resources_error(self, connector):
        connector.list_call_logs = AsyncMock(
            side_effect=RingCentralNetworkError("fail", status_code=503)
        )
        connector.list_messages = AsyncMock(
            side_effect=RingCentralNetworkError("fail", status_code=503)
        )
        connector.list_extensions = AsyncMock(
            side_effect=RingCentralNetworkError("fail", status_code=503)
        )
        connector.list_contacts = AsyncMock(
            side_effect=RingCentralNetworkError("fail", status_code=503)
        )
        connector.list_meetings = AsyncMock(
            side_effect=RingCentralNetworkError("fail", status_code=503)
        )
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert result.records_synced == 0

    async def test_sync_resources_dict_keys(self, connector):
        connector.list_call_logs = AsyncMock(return_value=[])
        connector.list_messages = AsyncMock(return_value=[])
        connector.list_extensions = AsyncMock(return_value=[])
        connector.list_contacts = AsyncMock(return_value=[])
        connector.list_meetings = AsyncMock(return_value=[])
        result = await connector.sync()
        assert "call_logs" in result.resources
        assert "messages" in result.resources
        assert "extensions" in result.resources
        assert "contacts" in result.resources
        assert "meetings" in result.resources

    async def test_sync_captures_auth_error(self, connector):
        connector.list_call_logs = AsyncMock(
            side_effect=RingCentralAuthError("bad token", status_code=401)
        )
        connector.list_messages = AsyncMock(return_value=[])
        connector.list_extensions = AsyncMock(return_value=[])
        connector.list_contacts = AsyncMock(return_value=[])
        connector.list_meetings = AsyncMock(return_value=[])
        result = await connector.sync()
        assert any("auth error" in e.lower() for e in result.errors)

    async def test_sync_result_to_dict(self, connector):
        connector.list_call_logs = AsyncMock(return_value=[])
        connector.list_messages = AsyncMock(return_value=[])
        connector.list_extensions = AsyncMock(return_value=[])
        connector.list_contacts = AsyncMock(return_value=[])
        connector.list_meetings = AsyncMock(return_value=[])
        result = await connector.sync()
        d = result.to_dict()
        assert "status" in d
        assert "records_synced" in d
        assert "resources" in d
        assert "errors" in d

    async def test_sync_ok_status_with_empty_resources(self, connector):
        connector.list_call_logs = AsyncMock(return_value=[])
        connector.list_messages = AsyncMock(return_value=[])
        connector.list_extensions = AsyncMock(return_value=[])
        connector.list_contacts = AsyncMock(return_value=[])
        connector.list_meetings = AsyncMock(return_value=[])
        result = await connector.sync()
        # No errors, no records — still OK
        assert result.status == SyncStatus.OK
        assert result.records_synced == 0

    async def test_sync_counts_per_resource(self, connector):
        connector.list_call_logs = AsyncMock(return_value=[_make_doc(ResourceType.CALL_LOG)] * 5)
        connector.list_messages = AsyncMock(return_value=[_make_doc(ResourceType.MESSAGE)] * 2)
        connector.list_extensions = AsyncMock(return_value=[])
        connector.list_contacts = AsyncMock(return_value=[])
        connector.list_meetings = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.resources["call_logs"] == 5
        assert result.resources["messages"] == 2


# ===========================================================================
# 9. list_* methods (5 tests)
# ===========================================================================


class TestListMethods:
    async def test_list_call_logs_returns_documents(self, connector):
        connector.client.paginate_all = AsyncMock(
            return_value=[{"id": "CL001", "direction": "Inbound", "duration": 60}]
        )
        docs = await connector.list_call_logs()
        assert len(docs) == 1
        assert docs[0].resource_type == ResourceType.CALL_LOG

    async def test_list_messages_returns_documents(self, connector):
        connector.client.paginate_all = AsyncMock(
            return_value=[{"id": "MSG001", "type": "SMS"}]
        )
        docs = await connector.list_messages()
        assert len(docs) == 1
        assert docs[0].resource_type == ResourceType.MESSAGE

    async def test_list_extensions_returns_documents(self, connector):
        connector.client.paginate_all = AsyncMock(
            return_value=[{"id": "EXT001", "extensionNumber": "101"}]
        )
        docs = await connector.list_extensions()
        assert docs[0].resource_type == ResourceType.EXTENSION

    async def test_list_contacts_returns_documents(self, connector):
        connector.client.paginate_all = AsyncMock(
            return_value=[{"id": "CON001", "firstName": "Alice"}]
        )
        docs = await connector.list_contacts()
        assert docs[0].resource_type == ResourceType.CONTACT

    async def test_list_meetings_returns_documents(self, connector):
        connector.client.paginate_all = AsyncMock(
            return_value=[{"id": "MTG001", "topic": "Standup"}]
        )
        docs = await connector.list_meetings()
        assert docs[0].resource_type == ResourceType.MEETING


# ===========================================================================
# 10. authorize URL (3 tests)
# ===========================================================================


class TestAuthorize:
    async def test_authorize_contains_client_id(self, connector):
        url = await connector.authorize()
        assert "test_client_id" in url

    async def test_authorize_contains_response_type_code(self, connector):
        url = await connector.authorize()
        assert "response_type=code" in url

    async def test_authorize_contains_scopes(self, connector):
        url = await connector.authorize()
        assert "ReadCallLog" in url

    async def test_authorize_uses_server_url(self, connector):
        url = await connector.authorize()
        assert "platform.ringcentral.com" in url

    async def test_authorize_includes_redirect_uri_when_set(self, base_config):
        from ringcentral_connector.connector import RingCentralConnector

        cfg = {**base_config, "redirect_uri": "https://myapp.com/callback"}
        c = RingCentralConnector(config=cfg)
        url = await c.authorize()
        assert "redirect_uri" in url
        assert "myapp.com" in url


# ===========================================================================
# 11. Token refresh (3 tests)
# ===========================================================================


class TestTokenRefresh:
    async def test_refresh_updates_access_token(self, http_client):
        new_token_response = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
        }
        # Patch the entire _post path via patching aiohttp
        http_client._access_token = "old_token"
        http_client._refresh_token = "valid_refresh"

        import aiohttp

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=new_token_response)

        mock_post = AsyncMock()
        mock_post.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_post)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await http_client.refresh_access_token()

        assert result["access_token"] == "new_access_token"
        assert http_client._access_token == "new_access_token"

    async def test_refresh_updates_refresh_token(self, http_client):
        new_token_response = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
        }
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=new_token_response)

        mock_post = AsyncMock()
        mock_post.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_post)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await http_client.refresh_access_token()

        assert http_client._refresh_token == "new_refresh"

    async def test_refresh_raises_auth_error_on_401(self, http_client):
        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.json = AsyncMock(return_value={"error": "invalid_token"})

        mock_post = AsyncMock()
        mock_post.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_post)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(RingCentralAuthError):
                await http_client.refresh_access_token()
