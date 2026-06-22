"""
Comprehensive unit test suite for the Microsoft Dynamics 365 connector.
70+ tests — no live network calls.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make the connector root importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent))

from exceptions import (
    Dynamics365AuthError,
    Dynamics365Error,
    Dynamics365NetworkError,
    Dynamics365NotFoundError,
    Dynamics365RateLimitError,
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
from helpers.utils import (
    normalize_contact,
    normalize_account,
    normalize_lead,
    normalize_opportunity,
    with_retry,
)
from client.http_client import Dynamics365HTTPClient
from connector import Dynamics365Connector, CONNECTOR_TYPE, AUTH_TYPE


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

INSTANCE_URL = "https://testorg.crm.dynamics.com"

BASE_CONFIG: dict[str, Any] = {
    "client_id": "CLIENT_ID",
    "client_secret": "CLIENT_SECRET",
    "tenant_id": "TENANT_UUID",
    "instance_url": INSTANCE_URL,
    "redirect_uri": "https://app.example.com/callback",
    "access_token": "ACCESS_TOKEN",
    "refresh_token": "REFRESH_TOKEN",
    "token_expires_at": time.monotonic() + 3600,
}


def _make_connector(**overrides: Any) -> Dynamics365Connector:
    cfg = {**BASE_CONFIG, **overrides}
    return Dynamics365Connector(
        tenant_id="shielva-tenant",
        connector_id="conn-abc",
        config=cfg,
    )


def _raw_contact(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contactid": "c1c1c1c1-0000-0000-0000-000000000001",
        "firstname": "Alice",
        "lastname": "Smith",
        "emailaddress1": "alice@example.com",
        "telephone1": "+1-555-0100",
        "jobtitle": "Engineer",
        "_parentcustomerid_value": "acc-001",
        "createdon": "2024-01-01T00:00:00Z",
        "modifiedon": "2024-06-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _raw_account(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "accountid": "a1a1a1a1-0000-0000-0000-000000000001",
        "name": "Acme Corp",
        "emailaddress1": "info@acme.com",
        "telephone1": "+1-555-0200",
        "websiteurl": "https://acme.com",
        "industry": "Technology",
        "revenue": 5000000,
        "createdon": "2023-01-01T00:00:00Z",
        "modifiedon": "2024-06-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _raw_lead(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "leadid": "l1l1l1l1-0000-0000-0000-000000000001",
        "firstname": "Bob",
        "lastname": "Jones",
        "companyname": "Startup Inc",
        "emailaddress1": "bob@startup.io",
        "telephone1": "+1-555-0300",
        "leadsourcecode": 1,
        "statuscode": 1,
        "createdon": "2024-02-01T00:00:00Z",
        "modifiedon": "2024-06-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _raw_opportunity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "opportunityid": "o1o1o1o1-0000-0000-0000-000000000001",
        "name": "Big Deal",
        "estimatedvalue": 100000,
        "actualclosedate": "2024-12-31",
        "closeprobability": 75,
        "stepname": "Proposal",
        "_parentaccountid_value": "acc-001",
        "statecode": 0,
        "createdon": "2024-03-01T00:00:00Z",
        "modifiedon": "2024-06-01T00:00:00Z",
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# 1. Exception hierarchy (5 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptions:
    def test_base_error_instantiation(self) -> None:
        exc = Dynamics365Error("something went wrong", status_code=400, code="BAD")
        assert str(exc) == "[400] something went wrong"
        assert exc.status_code == 400
        assert exc.code == "BAD"

    def test_base_error_no_status(self) -> None:
        exc = Dynamics365Error("oops")
        assert str(exc) == "oops"
        assert exc.status_code == 0

    def test_auth_error_is_base(self) -> None:
        exc = Dynamics365AuthError("invalid token", status_code=401)
        assert isinstance(exc, Dynamics365Error)
        assert exc.status_code == 401

    def test_network_error_is_base(self) -> None:
        exc = Dynamics365NetworkError("timeout", status_code=503)
        assert isinstance(exc, Dynamics365Error)

    def test_not_found_error(self) -> None:
        exc = Dynamics365NotFoundError("contact", "abc-123")
        assert isinstance(exc, Dynamics365Error)
        assert exc.status_code == 404
        assert "abc-123" in str(exc)
        assert exc.code == "NOT_FOUND"

    def test_not_found_error_no_id(self) -> None:
        exc = Dynamics365NotFoundError("account")
        assert "not found" in str(exc)

    def test_rate_limit_error(self) -> None:
        exc = Dynamics365RateLimitError("slow down", retry_after=30.0)
        assert isinstance(exc, Dynamics365Error)
        assert exc.status_code == 429
        assert exc.retry_after == 30.0
        assert exc.code == "rate_limit"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Model dataclasses (5 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestModels:
    def test_install_result_defaults(self) -> None:
        r = InstallResult(success=True, message="ok")
        assert r.connector_type == "dynamics365"
        assert r.success is True
        assert r.connector_id == ""

    def test_health_check_result(self) -> None:
        r = HealthCheckResult(healthy=True, message="ok", details={"x": 1})
        assert r.healthy is True
        assert r.details == {"x": 1}

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(success=True)
        assert r.documents == []
        assert r.metadata == {}
        assert r.documents_found == 0

    def test_connector_document(self) -> None:
        doc = ConnectorDocument(
            id="abc123",
            source="dynamics365",
            type="contact",
            title="Alice",
            content="Name: Alice",
            metadata={"email": "alice@x.com"},
            synced_at="2024-01-01T00:00:00+00:00",
        )
        assert doc.id == "abc123"
        assert doc.type == "contact"

    def test_enums(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert AuthStatus.CONNECTED == "connected"
        assert SyncStatus.COMPLETED == "completed"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(id="x", source="d365", type="lead", title="T", content="C")
        assert doc.metadata == {}
        assert doc.synced_at == ""
        assert doc.source_url == ""


# ─────────────────────────────────────────────────────────────────────────────
# 3. Normalizers (8 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizers:
    def test_normalize_contact_stable_id(self) -> None:
        raw = _raw_contact()
        doc1 = normalize_contact(raw, INSTANCE_URL)
        doc2 = normalize_contact(raw, INSTANCE_URL)
        assert doc1.id == doc2.id
        assert len(doc1.id) == 16

    def test_normalize_contact_fields(self) -> None:
        doc = normalize_contact(_raw_contact(), INSTANCE_URL)
        assert doc.type == "contact"
        assert doc.title == "Alice Smith"
        assert "alice@example.com" in doc.content
        assert doc.metadata["email"] == "alice@example.com"
        assert INSTANCE_URL in doc.source_url

    def test_normalize_contact_empty(self) -> None:
        doc = normalize_contact({}, "")
        assert doc.id  # still produces a stable id (for empty contactid "")
        assert doc.title == "Unknown Contact"
        assert doc.source_url == ""

    def test_normalize_account_fields(self) -> None:
        doc = normalize_account(_raw_account(), INSTANCE_URL)
        assert doc.type == "account"
        assert doc.title == "Acme Corp"
        assert "Technology" in doc.content
        assert doc.metadata["accountid"] == "a1a1a1a1-0000-0000-0000-000000000001"

    def test_normalize_account_stable_id(self) -> None:
        raw = _raw_account()
        assert normalize_account(raw).id == normalize_account(raw).id

    def test_normalize_lead_fields(self) -> None:
        doc = normalize_lead(_raw_lead(), INSTANCE_URL)
        assert doc.type == "lead"
        assert "Bob Jones" in doc.title
        assert "Startup Inc" in doc.content
        assert INSTANCE_URL in doc.source_url

    def test_normalize_opportunity_fields(self) -> None:
        doc = normalize_opportunity(_raw_opportunity(), INSTANCE_URL)
        assert doc.type == "opportunity"
        assert doc.title == "Big Deal"
        assert "Proposal" in doc.content
        assert doc.metadata["probability"] == "75"

    def test_normalize_opportunity_stable_id(self) -> None:
        raw = _raw_opportunity()
        assert normalize_opportunity(raw).id == normalize_opportunity(raw).id

    def test_normalize_contact_no_instance_url(self) -> None:
        doc = normalize_contact(_raw_contact())
        assert doc.source_url == ""

    def test_normalize_lead_empty(self) -> None:
        doc = normalize_lead({}, "")
        assert doc.title == "Unknown Lead"
        assert doc.type == "lead"


# ─────────────────────────────────────────────────────────────────────────────
# 4. with_retry (6 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        fn.assert_called_once()

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                Dynamics365NetworkError("timeout"),
                Dynamics365NetworkError("timeout"),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=Dynamics365AuthError("bad token"))
        with pytest.raises(Dynamics365AuthError):
            await with_retry(fn, max_attempts=3)
        fn.assert_called_once()

    async def test_raises_after_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=Dynamics365NetworkError("always fails"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(Dynamics365NetworkError):
                await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    async def test_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[
                Dynamics365RateLimitError("slow down", retry_after=5.0),
                {"ok": True},
            ]
        )
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        sleep_mock.assert_called_once_with(5.0)

    async def test_passes_args_to_fn(self) -> None:
        fn = AsyncMock(return_value="pong")
        result = await with_retry(fn, "arg1", key="val")
        fn.assert_called_once_with("arg1", key="val")
        assert result == "pong"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Dynamics365HTTPClient (12 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestHTTPClient:
    def _make_client(self, **extra: Any) -> Dynamics365HTTPClient:
        cfg = {**BASE_CONFIG, **extra}
        return Dynamics365HTTPClient(config=cfg)

    def _mock_response(self, status: int, body: Any) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        resp.json = AsyncMock(return_value=body)
        return resp

    async def test_get_contacts_returns_list(self) -> None:
        client = self._make_client()
        records = [_raw_contact()]
        resp = self._mock_response(200, {"value": records})
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=resp)
        mock_session.closed = False
        client._session = mock_session
        result = await client.get_contacts()
        assert result == records

    async def test_get_accounts_returns_list(self) -> None:
        client = self._make_client()
        records = [_raw_account()]
        resp = self._mock_response(200, {"value": records})
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=resp)
        mock_session.closed = False
        client._session = mock_session
        result = await client.get_accounts()
        assert result == records

    async def test_get_leads_returns_list(self) -> None:
        client = self._make_client()
        records = [_raw_lead()]
        resp = self._mock_response(200, {"value": records})
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=resp)
        mock_session.closed = False
        client._session = mock_session
        result = await client.get_leads()
        assert result == records

    async def test_get_opportunities_returns_list(self) -> None:
        client = self._make_client()
        records = [_raw_opportunity()]
        resp = self._mock_response(200, {"value": records})
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=resp)
        mock_session.closed = False
        client._session = mock_session
        result = await client.get_opportunities()
        assert result == records

    async def test_get_me_calls_graph(self) -> None:
        client = self._make_client()
        user_data = {"userPrincipalName": "user@tenant.onmicrosoft.com"}
        resp = self._mock_response(200, user_data)
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=resp)
        mock_session.closed = False
        client._session = mock_session
        result = await client.get_me()
        assert result["userPrincipalName"] == "user@tenant.onmicrosoft.com"

    async def test_raise_for_status_401(self) -> None:
        with pytest.raises(Dynamics365AuthError) as exc_info:
            Dynamics365HTTPClient._raise_for_status(401, {"error": {"message": "Unauthorized"}})
        assert exc_info.value.status_code == 401

    async def test_raise_for_status_403(self) -> None:
        with pytest.raises(Dynamics365AuthError) as exc_info:
            Dynamics365HTTPClient._raise_for_status(403, {"message": "Forbidden"})
        assert exc_info.value.status_code == 403

    async def test_raise_for_status_404(self) -> None:
        with pytest.raises(Dynamics365NotFoundError):
            Dynamics365HTTPClient._raise_for_status(404, {"message": "Entity not found"})

    async def test_raise_for_status_429(self) -> None:
        with pytest.raises(Dynamics365RateLimitError):
            Dynamics365HTTPClient._raise_for_status(429, {"message": "Too many requests"})

    async def test_raise_for_status_500(self) -> None:
        with pytest.raises(Dynamics365NetworkError) as exc_info:
            Dynamics365HTTPClient._raise_for_status(500, {"message": "Internal server error"})
        assert exc_info.value.status_code == 500

    async def test_raise_for_status_generic(self) -> None:
        with pytest.raises(Dynamics365Error) as exc_info:
            Dynamics365HTTPClient._raise_for_status(400, {"message": "Bad request"})
        assert exc_info.value.status_code == 400

    async def test_refresh_token_success(self) -> None:
        client = self._make_client(token_expires_at=time.monotonic() - 10)
        token_resp = self._mock_response(
            200,
            {"access_token": "NEW_TOKEN", "refresh_token": "NEW_REFRESH", "expires_in": 3600},
        )
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=token_resp)
        mock_session.closed = False
        client._session = mock_session
        result = await client.refresh_token()
        assert result["access_token"] == "NEW_TOKEN"
        assert client._access_token == "NEW_TOKEN"

    async def test_refresh_token_no_refresh_token_raises(self) -> None:
        client = self._make_client(refresh_token="")
        with pytest.raises(Dynamics365AuthError, match="No refresh_token"):
            await client.refresh_token()

    async def test_is_token_expired_no_token(self) -> None:
        client = self._make_client(access_token="", token_expires_at=0)
        assert client._is_token_expired() is True

    async def test_is_token_expired_far_future(self) -> None:
        client = self._make_client(token_expires_at=time.monotonic() + 7200)
        assert client._is_token_expired() is False

    async def test_empty_value_list(self) -> None:
        client = self._make_client()
        resp = self._mock_response(200, {"value": []})
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=resp)
        mock_session.closed = False
        client._session = mock_session
        result = await client.get_contacts()
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# 6. Dynamics365Connector.install (8 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectorInstall:
    async def test_install_success(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(return_value={"userPrincipalName": "user@ms.com"})
        result = await conn.install()
        assert result.success is True
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_type == "dynamics365"

    async def test_install_missing_client_id(self) -> None:
        conn = _make_connector(client_id="")
        result = await conn.install()
        assert result.success is False
        assert "client_id" in result.message
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_missing_client_secret(self) -> None:
        conn = _make_connector(client_secret="")
        result = await conn.install()
        assert result.success is False
        assert "client_secret" in result.message

    async def test_install_missing_tenant_id(self) -> None:
        conn = _make_connector(tenant_id="")
        result = await conn.install()
        assert result.success is False
        assert "tenant_id" in result.message

    async def test_install_missing_instance_url(self) -> None:
        conn = _make_connector(instance_url="")
        result = await conn.install()
        assert result.success is False
        assert "instance_url" in result.message

    async def test_install_missing_access_token(self) -> None:
        conn = _make_connector(access_token="")
        result = await conn.install()
        assert result.success is False
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_auth_error(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(side_effect=Dynamics365AuthError("invalid token"))
        result = await conn.install()
        assert result.success is False
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
        assert result.health == ConnectorHealth.OFFLINE

    async def test_install_network_error(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(side_effect=Dynamics365NetworkError("timeout"))
        result = await conn.install()
        assert result.success is False
        assert result.health == ConnectorHealth.OFFLINE


# ─────────────────────────────────────────────────────────────────────────────
# 7. health_check (8 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthCheck:
    async def test_health_check_success(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(return_value={"userPrincipalName": "user@ms.com"})
        result = await conn.health_check()
        assert result.healthy is True
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_no_access_token(self) -> None:
        conn = _make_connector(access_token="")
        result = await conn.health_check()
        assert result.healthy is False
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(side_effect=Dynamics365AuthError("expired"))
        result = await conn.health_check()
        assert result.healthy is False
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(side_effect=Dynamics365NetworkError("timeout"))
        result = await conn.health_check()
        assert result.healthy is False
        assert result.health == ConnectorHealth.DEGRADED

    async def test_health_check_unexpected_error(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(side_effect=RuntimeError("unexpected"))
        result = await conn.health_check()
        assert result.healthy is False
        assert result.health == ConnectorHealth.DEGRADED

    async def test_health_check_includes_user(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(return_value={"userPrincipalName": "alice@corp.com"})
        result = await conn.health_check()
        assert result.details.get("user") == "alice@corp.com"

    async def test_health_check_result_type(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(return_value={})
        result = await conn.health_check()
        assert isinstance(result, HealthCheckResult)

    async def test_health_check_message_non_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(return_value={})
        result = await conn.health_check()
        assert len(result.message) > 0


# ─────────────────────────────────────────────────────────────────────────────
# 8. Sync (10 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestSync:
    def _mock_client(self, conn: Dynamics365Connector, contacts=None, accounts=None, leads=None, opportunities=None) -> None:
        conn.client.get_contacts = AsyncMock(return_value=contacts or [])
        conn.client.get_accounts = AsyncMock(return_value=accounts or [])
        conn.client.get_leads = AsyncMock(return_value=leads or [])
        conn.client.get_opportunities = AsyncMock(return_value=opportunities or [])

    async def test_sync_success_all_entities(self) -> None:
        conn = _make_connector()
        self._mock_client(
            conn,
            contacts=[_raw_contact()],
            accounts=[_raw_account()],
            leads=[_raw_lead()],
            opportunities=[_raw_opportunity()],
        )
        result = await conn.sync()
        assert result.success is True
        assert result.documents_synced == 4
        assert result.documents_failed == 0
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_empty_results(self) -> None:
        conn = _make_connector()
        self._mock_client(conn)
        result = await conn.sync()
        assert result.success is True
        assert result.documents_synced == 0
        assert result.documents == []

    async def test_sync_returns_sync_result(self) -> None:
        conn = _make_connector()
        self._mock_client(conn)
        result = await conn.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_documents_are_connector_documents(self) -> None:
        conn = _make_connector()
        self._mock_client(conn, contacts=[_raw_contact()])
        result = await conn.sync()
        assert all(isinstance(d, ConnectorDocument) for d in result.documents)

    async def test_sync_partial_when_entity_fails(self) -> None:
        conn = _make_connector()
        conn.client.get_contacts = AsyncMock(side_effect=Dynamics365NetworkError("timeout"))
        conn.client.get_accounts = AsyncMock(return_value=[_raw_account()])
        conn.client.get_leads = AsyncMock(return_value=[])
        conn.client.get_opportunities = AsyncMock(return_value=[])
        result = await conn.sync()
        assert result.success is False
        assert result.documents_synced >= 1
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_failed_when_all_fail(self) -> None:
        conn = _make_connector()
        err = Dynamics365NetworkError("net error")
        conn.client.get_contacts = AsyncMock(side_effect=err)
        conn.client.get_accounts = AsyncMock(side_effect=err)
        conn.client.get_leads = AsyncMock(side_effect=err)
        conn.client.get_opportunities = AsyncMock(side_effect=err)
        result = await conn.sync()
        assert result.success is False
        assert result.documents_synced == 0
        assert result.status == SyncStatus.FAILED

    async def test_sync_metadata_keys(self) -> None:
        conn = _make_connector()
        self._mock_client(conn, contacts=[_raw_contact()])
        result = await conn.sync()
        assert "total" in result.metadata
        assert "entities" in result.metadata

    async def test_sync_ingest_document_called_with_kb_id(self) -> None:
        conn = _make_connector()
        self._mock_client(conn, contacts=[_raw_contact()])
        conn._ingest_document = AsyncMock()
        await conn.sync(kb_id="kb-001")
        conn._ingest_document.assert_called()

    async def test_sync_multiple_contacts(self) -> None:
        conn = _make_connector()
        contacts = [_raw_contact(contactid=f"c-{i}") for i in range(5)]
        self._mock_client(conn, contacts=contacts)
        result = await conn.sync()
        assert result.documents_synced == 5

    async def test_sync_documents_found_equals_synced_plus_failed(self) -> None:
        conn = _make_connector()
        self._mock_client(conn, contacts=[_raw_contact()], accounts=[_raw_account()])
        result = await conn.sync()
        assert result.documents_found == result.documents_synced + result.documents_failed


# ─────────────────────────────────────────────────────────────────────────────
# 9. list_contacts / list_accounts / list_leads / list_opportunities (6 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestEntityListMethods:
    async def test_list_contacts(self) -> None:
        conn = _make_connector()
        conn.client.get_contacts = AsyncMock(return_value=[_raw_contact()])
        result = await conn.list_contacts()
        assert isinstance(result, list)
        assert result[0]["contactid"] == "c1c1c1c1-0000-0000-0000-000000000001"

    async def test_list_accounts(self) -> None:
        conn = _make_connector()
        conn.client.get_accounts = AsyncMock(return_value=[_raw_account()])
        result = await conn.list_accounts()
        assert result[0]["name"] == "Acme Corp"

    async def test_list_leads(self) -> None:
        conn = _make_connector()
        conn.client.get_leads = AsyncMock(return_value=[_raw_lead()])
        result = await conn.list_leads()
        assert result[0]["leadid"] == "l1l1l1l1-0000-0000-0000-000000000001"

    async def test_list_opportunities(self) -> None:
        conn = _make_connector()
        conn.client.get_opportunities = AsyncMock(return_value=[_raw_opportunity()])
        result = await conn.list_opportunities()
        assert result[0]["name"] == "Big Deal"

    async def test_list_contacts_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_contacts = AsyncMock(return_value=[])
        result = await conn.list_contacts()
        assert result == []

    async def test_list_methods_propagate_auth_error(self) -> None:
        conn = _make_connector()
        conn.client.get_leads = AsyncMock(side_effect=Dynamics365AuthError("expired"))
        with pytest.raises(Dynamics365AuthError):
            await conn.list_leads()


# ─────────────────────────────────────────────────────────────────────────────
# 10. authorize() (4 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthorize:
    async def test_authorize_returns_url(self) -> None:
        conn = _make_connector()
        url = await conn.authorize()
        assert url.startswith("https://login.microsoftonline.com/")
        assert "TENANT_UUID" in url

    async def test_authorize_contains_client_id(self) -> None:
        conn = _make_connector()
        url = await conn.authorize()
        assert "CLIENT_ID" in url

    async def test_authorize_contains_scope(self) -> None:
        conn = _make_connector()
        url = await conn.authorize()
        assert "scope" in url

    async def test_authorize_fallback_tenant(self) -> None:
        conn = _make_connector(tenant_id="")
        url = await conn.authorize()
        assert "common" in url


# ─────────────────────────────────────────────────────────────────────────────
# 11. Module-level constants (3 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestConstants:
    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "dynamics365"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "oauth2"

    def test_connector_class_constants(self) -> None:
        assert Dynamics365Connector.CONNECTOR_TYPE == "dynamics365"
        assert Dynamics365Connector.AUTH_TYPE == "oauth2"


# ─────────────────────────────────────────────────────────────────────────────
# 12. Edge cases and misc (6 tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_connector_stores_tenant_and_connector_id(self) -> None:
        conn = _make_connector()
        assert conn.tenant_id == "shielva-tenant"
        assert conn.connector_id == "conn-abc"

    def test_connector_has_client(self) -> None:
        conn = _make_connector()
        assert isinstance(conn.client, Dynamics365HTTPClient)

    def test_http_client_default_config(self) -> None:
        client = Dynamics365HTTPClient()
        assert client._access_token == ""
        assert client._instance_url == ""

    def test_http_client_strips_trailing_slash(self) -> None:
        client = Dynamics365HTTPClient(config={"instance_url": "https://org.crm.dynamics.com/"})
        assert not client._instance_url.endswith("/")

    async def test_aclose_is_safe_when_no_session(self) -> None:
        conn = _make_connector()
        conn.client._session = None
        await conn.aclose()  # should not raise

    async def test_sync_returns_documents_list(self) -> None:
        conn = _make_connector()
        conn.client.get_contacts = AsyncMock(return_value=[])
        conn.client.get_accounts = AsyncMock(return_value=[])
        conn.client.get_leads = AsyncMock(return_value=[])
        conn.client.get_opportunities = AsyncMock(return_value=[])
        result = await conn.sync()
        assert isinstance(result.documents, list)

    def test_stable_id_different_for_different_ids(self) -> None:
        doc1 = normalize_contact(_raw_contact(contactid="id-001"))
        doc2 = normalize_contact(_raw_contact(contactid="id-002"))
        assert doc1.id != doc2.id

    async def test_connector_as_async_context_manager(self) -> None:
        conn = _make_connector()
        conn.client._session = None
        async with conn as c:
            assert c is conn
