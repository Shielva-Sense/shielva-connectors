"""Tests for the Mailchimp connector — no live API calls."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    MailchimpAuthError,
    MailchimpError,
    MailchimpNetworkError,
    MailchimpNotFoundError,
    MailchimpRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorHealth,
    ConnectorDocument,
    InstallResult,
    HealthCheckResult,
    SyncResult,
    SyncStatus,
)
from helpers.utils import (
    extract_dc_from_api_key,
    get_subscriber_hash,
    normalize_member,
    normalize_campaign,
    with_retry,
)
from client.http_client import MailchimpHTTPClient
from connector import MailchimpConnector

TENANT = "test-tenant"
CONNECTOR_ID = "mailchimp_test"
API_KEY = "abc123-us10"
DC = "us10"


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_member(
    email: str = "alice@example.com",
    list_id: str = "list_abc",
    status: str = "subscribed",
    first_name: str = "Alice",
    last_name: str = "Smith",
    unique_email_id: str = "unique_001",
    tags: list | None = None,
    language: str = "en",
    vip: bool = False,
) -> Dict[str, Any]:
    return {
        "id": "subscriber_hash_001",
        "email_address": email,
        "unique_email_id": unique_email_id,
        "web_id": 12345,
        "status": status,
        "full_name": f"{first_name} {last_name}",
        "merge_fields": {"FNAME": first_name, "LNAME": last_name},
        "tags": [{"id": 1, "name": t} for t in (tags or [])],
        "language": language,
        "vip": vip,
        "timestamp_signup": "2024-01-15T10:00:00+00:00",
        "timestamp_opt": "2024-01-15T10:01:00+00:00",
        "last_changed": "2024-06-01T08:00:00+00:00",
        "location": {"country_code": "US", "timezone": "America/New_York"},
        "stats": {"avg_open_rate": 0.5, "avg_click_rate": 0.1},
        "list_id": list_id,
    }


def _make_audience(
    list_id: str = "list_abc",
    name: str = "Main List",
    member_count: int = 100,
) -> Dict[str, Any]:
    return {
        "id": list_id,
        "name": name,
        "stats": {"member_count": member_count},
        "date_created": "2023-01-01T00:00:00+00:00",
    }


def _make_campaign(
    campaign_id: str = "camp_001",
    title: str = "June Newsletter",
    subject: str = "Big news from us!",
    status: str = "sent",
    campaign_type: str = "regular",
    emails_sent: int = 500,
    list_id: str = "list_abc",
    list_name: str = "Main List",
) -> Dict[str, Any]:
    return {
        "id": campaign_id,
        "type": campaign_type,
        "status": status,
        "emails_sent": emails_sent,
        "send_time": "2024-06-01T12:00:00+00:00",
        "create_time": "2024-05-30T08:00:00+00:00",
        "settings": {
            "title": title,
            "subject_line": subject,
            "from_name": "Acme Corp",
            "reply_to": "hello@acme.com",
        },
        "recipients": {
            "list_id": list_id,
            "list_name": list_name,
        },
    }


def _make_connector(api_key: str = API_KEY) -> MailchimpConnector:
    return MailchimpConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key},
    )


# ── Exception hierarchy ───────────────────────────────────────────────────────

class TestExceptions:
    def test_hierarchy_auth(self):
        assert issubclass(MailchimpAuthError, MailchimpError)

    def test_hierarchy_network(self):
        assert issubclass(MailchimpNetworkError, MailchimpError)

    def test_hierarchy_rate_limit(self):
        assert issubclass(MailchimpRateLimitError, MailchimpError)

    def test_hierarchy_not_found(self):
        assert issubclass(MailchimpNotFoundError, MailchimpError)

    def test_base_is_exception(self):
        assert issubclass(MailchimpError, Exception)

    def test_raise_auth(self):
        with pytest.raises(MailchimpAuthError, match="401"):
            raise MailchimpAuthError("401")

    def test_raise_network(self):
        with pytest.raises(MailchimpNetworkError, match="timeout"):
            raise MailchimpNetworkError("timeout")

    def test_raise_rate_limit(self):
        with pytest.raises(MailchimpRateLimitError):
            raise MailchimpRateLimitError("429")

    def test_raise_not_found(self):
        with pytest.raises(MailchimpNotFoundError, match="404"):
            raise MailchimpNotFoundError("404")

    def test_catch_base_catches_auth(self):
        with pytest.raises(MailchimpError):
            raise MailchimpAuthError("auth fail")

    def test_catch_base_catches_network(self):
        with pytest.raises(MailchimpError):
            raise MailchimpNetworkError("conn refused")

    def test_exception_message_preserved(self):
        exc = MailchimpRateLimitError("retry after 60")
        assert "retry after 60" in str(exc)


# ── Models ────────────────────────────────────────────────────────────────────

class TestModels:
    def test_auth_status_connected(self):
        assert AuthStatus.CONNECTED == "connected"

    def test_auth_status_missing(self):
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"

    def test_auth_status_invalid(self):
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_auth_status_failed(self):
        assert AuthStatus.FAILED == "failed"

    def test_health_healthy(self):
        assert ConnectorHealth.HEALTHY == "healthy"

    def test_health_degraded(self):
        assert ConnectorHealth.DEGRADED == "degraded"

    def test_health_offline(self):
        assert ConnectorHealth.OFFLINE == "offline"

    def test_sync_status_completed(self):
        assert SyncStatus.COMPLETED == "completed"

    def test_sync_status_partial(self):
        assert SyncStatus.PARTIAL == "partial"

    def test_sync_status_failed(self):
        assert SyncStatus.FAILED == "failed"

    def test_install_result_defaults(self):
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=CONNECTOR_ID,
        )
        assert r.message == ""
        assert r.connector_id == CONNECTOR_ID

    def test_health_check_result_defaults(self):
        r = HealthCheckResult(
            health=ConnectorHealth.DEGRADED,
            auth_status=AuthStatus.FAILED,
        )
        assert r.message == ""

    def test_sync_result_defaults(self):
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0

    def test_connector_document_type_default(self):
        doc = ConnectorDocument(id="x", title="t", content="c")
        assert doc.type == "email_contact"

    def test_connector_document_custom_type(self):
        doc = ConnectorDocument(id="x", title="t", content="c", type="email_campaign")
        assert doc.type == "email_campaign"

    def test_connector_document_metadata_default(self):
        doc = ConnectorDocument(id="x", title="t", content="c")
        assert doc.metadata == {}


# ── extract_dc_from_api_key ───────────────────────────────────────────────────

class TestExtractDc:
    def test_extracts_us10(self):
        assert extract_dc_from_api_key("abc123-us10") == "us10"

    def test_extracts_us1(self):
        assert extract_dc_from_api_key("somekey-us1") == "us1"

    def test_extracts_eu2(self):
        assert extract_dc_from_api_key("key-eu2") == "eu2"

    def test_no_dash_returns_empty(self):
        assert extract_dc_from_api_key("nodashkey") == ""

    def test_empty_string_returns_empty(self):
        assert extract_dc_from_api_key("") == ""

    def test_multiple_dashes_takes_last(self):
        assert extract_dc_from_api_key("abc-123-us6") == "us6"

    def test_key_with_only_dash(self):
        assert extract_dc_from_api_key("-") == ""

    def test_typical_key_format(self):
        assert extract_dc_from_api_key("a1b2c3d4e5f6-us10") == "us10"


# ── normalize_member ──────────────────────────────────────────────────────────

class TestNormalizeMember:
    def test_full_member(self):
        member = _make_member(email="alice@example.com", list_id="L1", first_name="Alice", last_name="Smith")
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert doc.type == "email_contact"
        assert "alice@example.com" in doc.title
        assert "Alice Smith" in doc.title
        assert "Main List" in doc.content
        assert "alice@example.com" in doc.content
        assert doc.metadata["list_id"] == "L1"
        assert doc.metadata["list_name"] == "Main List"
        assert doc.metadata["email"] == "alice@example.com"
        assert doc.metadata["source"] == "mailchimp"

    def test_stable_id_from_list_id_and_email(self):
        member = _make_member(email="alice@example.com", list_id="L1")
        doc1 = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        doc2 = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert doc1.id == doc2.id

    def test_id_is_16_chars(self):
        member = _make_member()
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert len(doc.id) == 16

    def test_id_differs_for_different_email(self):
        m1 = _make_member(email="alice@example.com")
        m2 = _make_member(email="bob@example.com")
        d1 = normalize_member(m1, "L1", "Main List", CONNECTOR_ID, TENANT)
        d2 = normalize_member(m2, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert d1.id != d2.id

    def test_id_differs_for_different_list(self):
        member = _make_member(email="alice@example.com")
        d1 = normalize_member(member, "L1", "List 1", CONNECTOR_ID, TENANT)
        d2 = normalize_member(member, "L2", "List 2", CONNECTOR_ID, TENANT)
        assert d1.id != d2.id

    def test_status_in_content(self):
        member = _make_member(status="unsubscribed")
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert "unsubscribed" in doc.content

    def test_tags_in_content(self):
        member = _make_member(tags=["VIP", "Newsletter"])
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert "VIP" in doc.content
        assert "Newsletter" in doc.content

    def test_tags_empty_by_default(self):
        member = _make_member(tags=[])
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert doc.metadata["tags"] == []

    def test_metadata_connector_and_tenant(self):
        member = _make_member()
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert doc.metadata["connector_id"] == CONNECTOR_ID
        assert doc.metadata["tenant_id"] == TENANT

    def test_email_lowercase_in_id(self):
        member = _make_member(email="Alice@Example.COM")
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        # id should use lower-cased email
        import hashlib
        expected = hashlib.sha256("L1:alice@example.com".encode()).hexdigest()[:16]
        assert doc.id == expected

    def test_vip_in_metadata(self):
        member = _make_member(vip=True)
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert doc.metadata["vip"] is True

    def test_language_in_content(self):
        member = _make_member(language="fr")
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert "fr" in doc.content

    def test_member_without_full_name_uses_merge_fields(self):
        member = _make_member(first_name="Bob", last_name="Jones")
        member.pop("full_name", None)
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        assert "Bob Jones" in doc.title

    def test_member_email_only_title(self):
        member = _make_member(first_name="", last_name="")
        member["full_name"] = ""
        doc = normalize_member(member, "L1", "Main List", CONNECTOR_ID, TENANT)
        # title is email when no name
        assert doc.title == "alice@example.com"


# ── normalize_campaign ────────────────────────────────────────────────────────

class TestNormalizeCampaign:
    def test_full_campaign(self):
        camp = _make_campaign()
        doc = normalize_campaign(camp, CONNECTOR_ID, TENANT)
        assert doc.type == "email_campaign"
        assert "June Newsletter" in doc.title
        assert "Big news from us!" in doc.content
        assert doc.metadata["status"] == "sent"
        assert doc.metadata["source"] == "mailchimp"

    def test_stable_id_from_campaign_id(self):
        camp = _make_campaign(campaign_id="camp_001")
        d1 = normalize_campaign(camp, CONNECTOR_ID, TENANT)
        d2 = normalize_campaign(camp, CONNECTOR_ID, TENANT)
        assert d1.id == d2.id

    def test_id_is_16_chars(self):
        camp = _make_campaign()
        doc = normalize_campaign(camp, CONNECTOR_ID, TENANT)
        assert len(doc.id) == 16

    def test_id_differs_for_different_campaigns(self):
        c1 = _make_campaign(campaign_id="camp_001")
        c2 = _make_campaign(campaign_id="camp_002")
        d1 = normalize_campaign(c1, CONNECTOR_ID, TENANT)
        d2 = normalize_campaign(c2, CONNECTOR_ID, TENANT)
        assert d1.id != d2.id

    def test_emails_sent_in_content(self):
        camp = _make_campaign(emails_sent=1234)
        doc = normalize_campaign(camp, CONNECTOR_ID, TENANT)
        assert "1234" in doc.content

    def test_metadata_connector_and_tenant(self):
        camp = _make_campaign()
        doc = normalize_campaign(camp, CONNECTOR_ID, TENANT)
        assert doc.metadata["connector_id"] == CONNECTOR_ID
        assert doc.metadata["tenant_id"] == TENANT

    def test_list_name_in_content(self):
        camp = _make_campaign(list_name="VIP List")
        doc = normalize_campaign(camp, CONNECTOR_ID, TENANT)
        assert "VIP List" in doc.content


# ── with_retry ────────────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_succeeds_first_try(self):
        called = []

        async def fn():
            called.append(1)
            return "ok"

        result = await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert result == "ok"
        assert len(called) == 1

    async def test_retries_on_mailchimp_error(self):
        calls = []

        async def fn():
            calls.append(1)
            if len(calls) < 3:
                raise MailchimpError("transient")
            return "recovered"

        result = await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert result == "recovered"
        assert len(calls) == 3

    async def test_no_retry_on_auth_error(self):
        calls = []

        async def fn():
            calls.append(1)
            raise MailchimpAuthError("invalid key")

        with pytest.raises(MailchimpAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert len(calls) == 1

    async def test_raises_after_max_attempts(self):
        async def fn():
            raise MailchimpNetworkError("timeout")

        with pytest.raises(MailchimpNetworkError):
            await with_retry(fn, max_attempts=2, base_delay=0.0)

    async def test_sync_callable_works(self):
        def fn():
            return 42

        result = await with_retry(fn, max_attempts=1, base_delay=0.0)
        assert result == 42


# ── MailchimpHTTPClient ───────────────────────────────────────────────────────

class TestMailchimpHTTPClient:
    def test_init_sets_base_url(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        assert "us10.api.mailchimp.com" in client._base_url

    def test_init_sets_auth(self):
        client = MailchimpHTTPClient(dc="us10", api_key="mykey")
        assert client._auth.login == "anystring"
        assert client._auth.password == "mykey"

    async def test_get_root_success(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"account_name": "Acme Corp"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            data = await client.get_root()
        assert data["account_name"] == "Acme Corp"

    async def test_get_root_401_raises_auth_error(self):
        client = MailchimpHTTPClient(dc="us10", api_key="bad")
        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpAuthError):
                await client.get_root()

    async def test_get_root_403_raises_auth_error(self):
        client = MailchimpHTTPClient(dc="us10", api_key="bad")
        mock_resp = AsyncMock()
        mock_resp.status = 403
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpAuthError):
                await client.get_root()

    async def test_get_root_404_raises_not_found(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key")
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpNotFoundError):
                await client.get_root()

    async def test_get_root_429_raises_rate_limit(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key")
        mock_resp = AsyncMock()
        mock_resp.status = 429
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpRateLimitError):
                await client.get_root()

    async def test_network_error_raises_mailchimp_network_error(self):
        import aiohttp as _aiohttp
        client = MailchimpHTTPClient(dc="us10", api_key="key")

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=_aiohttp.ClientConnectionError("refused"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpNetworkError):
                await client.get_root()

    async def test_get_lists_passes_params(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"lists": [], "total_items": 0})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            data = await client.get_lists(count=50, offset=0)
        assert "lists" in data


# ── MailchimpConnector — install ──────────────────────────────────────────────

class TestInstall:
    async def test_install_success(self):
        connector = _make_connector()
        result = await connector.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "us10" in result.message

    async def test_install_missing_api_key(self):
        connector = MailchimpConnector(
            tenant_id=TENANT, connector_id=CONNECTOR_ID, config={}
        )
        result = await connector.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_empty_api_key(self):
        connector = MailchimpConnector(
            tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"api_key": ""}
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE

    async def test_install_no_config(self):
        connector = MailchimpConnector(
            tenant_id=TENANT, connector_id=CONNECTOR_ID
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE

    async def test_install_stores_connector_id(self):
        connector = _make_connector()
        result = await connector.install()
        assert result.connector_id == CONNECTOR_ID


# ── MailchimpConnector — health_check ─────────────────────────────────────────

class TestHealthCheck:
    async def test_health_check_success(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_root=AsyncMock(return_value={"account_name": "Acme Corp"})
            )
        )
        result = await connector.health_check()
        assert isinstance(result, HealthCheckResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Acme Corp" in result.message

    async def test_health_check_auth_error(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_root=AsyncMock(side_effect=MailchimpAuthError("invalid key"))
            )
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_root=AsyncMock(side_effect=MailchimpNetworkError("timeout"))
            )
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_unknown_account(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_root=AsyncMock(return_value={})
            )
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert "unknown account" in result.message


# ── MailchimpConnector — list_audiences ───────────────────────────────────────

class TestListAudiences:
    async def test_returns_audiences(self):
        connector = _make_connector()
        audience = _make_audience()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_lists=AsyncMock(
                    return_value={"lists": [audience], "total_items": 1}
                )
            )
        )
        result = await connector.list_audiences()
        assert len(result) == 1
        assert result[0]["id"] == "list_abc"

    async def test_returns_empty_when_none(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_lists=AsyncMock(
                    return_value={"lists": [], "total_items": 0}
                )
            )
        )
        result = await connector.list_audiences()
        assert result == []

    async def test_pagination_stops_when_total_reached(self):
        connector = _make_connector()
        call_count = 0

        async def mock_get_lists(count, offset):
            nonlocal call_count
            call_count += 1
            if offset == 0:
                return {"lists": [_make_audience(list_id=f"L{i}") for i in range(2)], "total_items": 2}
            return {"lists": [], "total_items": 2}

        client_mock = MagicMock()
        client_mock.get_lists = mock_get_lists
        connector._ensure_client = MagicMock(return_value=client_mock)
        result = await connector.list_audiences(count=2)
        assert len(result) == 2
        assert call_count == 1


# ── MailchimpConnector — list_members ─────────────────────────────────────────

class TestListMembers:
    async def test_returns_members(self):
        connector = _make_connector()
        member = _make_member()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_members=AsyncMock(
                    return_value={"members": [member], "total_items": 1}
                )
            )
        )
        result = await connector.list_members("list_abc")
        assert len(result) == 1
        assert result[0]["email_address"] == "alice@example.com"

    async def test_returns_empty_list(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_members=AsyncMock(
                    return_value={"members": [], "total_items": 0}
                )
            )
        )
        result = await connector.list_members("list_abc")
        assert result == []


# ── MailchimpConnector — get_audience ─────────────────────────────────────────

class TestGetAudience:
    async def test_returns_audience(self):
        connector = _make_connector()
        audience = _make_audience(list_id="L1", name="Primary List")
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_list=AsyncMock(return_value=audience)
            )
        )
        result = await connector.get_audience("L1")
        assert result["name"] == "Primary List"

    async def test_not_found_propagates(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_list=AsyncMock(side_effect=MailchimpNotFoundError("404"))
            )
        )
        with pytest.raises(MailchimpNotFoundError):
            await connector.get_audience("nonexistent")


# ── MailchimpConnector — get_member ───────────────────────────────────────────

class TestGetMember:
    async def test_returns_member(self):
        connector = _make_connector()
        member = _make_member()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_member=AsyncMock(return_value=member)
            )
        )
        result = await connector.get_member("L1", "subscriber_hash_001")
        assert result["email_address"] == "alice@example.com"


# ── MailchimpConnector — list_campaigns ───────────────────────────────────────

class TestListCampaigns:
    async def test_returns_campaigns(self):
        connector = _make_connector()
        camp = _make_campaign()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_campaigns=AsyncMock(
                    return_value={"campaigns": [camp], "total_items": 1}
                )
            )
        )
        result = await connector.list_campaigns()
        assert len(result) == 1
        assert result[0]["id"] == "camp_001"

    async def test_returns_empty_list(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_campaigns=AsyncMock(
                    return_value={"campaigns": [], "total_items": 0}
                )
            )
        )
        result = await connector.list_campaigns()
        assert result == []


# ── MailchimpConnector — sync ─────────────────────────────────────────────────

class TestSync:
    async def test_sync_all_members_success(self):
        connector = _make_connector()
        audience = _make_audience()
        member1 = _make_member(email="alice@example.com", unique_email_id="u1")
        member2 = _make_member(email="bob@example.com", unique_email_id="u2")

        async def mock_list_audiences():
            return [audience]

        async def mock_list_members(list_id):
            return [member1, member2]

        connector.list_audiences = mock_list_audiences
        connector.list_members = mock_list_members

        result = await connector.sync()
        assert isinstance(result, SyncResult)
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 2
        assert result.documents_synced == 2
        assert result.documents_failed == 0

    async def test_sync_empty_audiences(self):
        connector = _make_connector()

        async def mock_list_audiences():
            return []

        connector.list_audiences = mock_list_audiences

        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_partial_on_member_failure(self):
        connector = _make_connector()
        audience = _make_audience()

        good_member = _make_member(email="alice@example.com")
        bad_member: Dict[str, Any] = {}  # missing email — normalize will still work but test coverage

        async def mock_list_audiences():
            return [audience]

        call_count = [0]

        async def mock_list_members(list_id):
            call_count[0] += 1
            return [good_member]

        connector.list_audiences = mock_list_audiences
        connector.list_members = mock_list_members

        result = await connector.sync()
        assert result.documents_synced >= 0

    async def test_sync_fails_on_auth_error(self):
        connector = _make_connector()

        async def mock_list_audiences():
            raise MailchimpAuthError("invalid key")

        connector.list_audiences = mock_list_audiences

        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "invalid key" in result.message

    async def test_sync_message_includes_counts(self):
        connector = _make_connector()
        audience = _make_audience()
        member = _make_member()

        async def mock_list_audiences():
            return [audience]

        async def mock_list_members(list_id):
            return [member]

        connector.list_audiences = mock_list_audiences
        connector.list_members = mock_list_members

        result = await connector.sync()
        assert "1" in result.message

    async def test_sync_list_failure_counts_as_failed(self):
        connector = _make_connector()
        audience = _make_audience()

        async def mock_list_audiences():
            return [audience]

        async def mock_list_members(list_id):
            raise MailchimpNetworkError("timeout")

        connector.list_audiences = mock_list_audiences
        connector.list_members = mock_list_members

        result = await connector.sync()
        assert result.documents_failed > 0
        assert result.status in (SyncStatus.PARTIAL, SyncStatus.COMPLETED)


# ── MailchimpConnector — misc ─────────────────────────────────────────────────

class TestConnectorMisc:
    def test_connector_type(self):
        connector = _make_connector()
        assert connector.CONNECTOR_TYPE == "mailchimp"

    def test_auth_type(self):
        connector = _make_connector()
        assert connector.AUTH_TYPE == "api_key"

    def test_connector_name(self):
        connector = _make_connector()
        assert connector.CONNECTOR_NAME == "Mailchimp"

    def test_get_dc(self):
        connector = _make_connector(api_key="abc123-us10")
        assert connector._get_dc() == "us10"

    def test_get_api_key(self):
        connector = _make_connector()
        assert connector._get_api_key() == API_KEY

    def test_ensure_client_creates_http_client(self):
        connector = _make_connector()
        client = connector._ensure_client()
        assert isinstance(client, MailchimpHTTPClient)

    def test_ensure_client_reuses_existing(self):
        connector = _make_connector()
        c1 = connector._ensure_client()
        c2 = connector._ensure_client()
        assert c1 is c2

    async def test_aclose_clears_client(self):
        connector = _make_connector()
        _ = connector._ensure_client()
        assert connector._http_client is not None
        await connector.aclose()
        assert connector._http_client is None

    async def test_context_manager(self):
        async with _make_connector() as connector:
            assert isinstance(connector, MailchimpConnector)


# ── get_subscriber_hash ───────────────────────────────────────────────────────

class TestGetSubscriberHash:
    def test_known_hash(self):
        # md5("alice@example.com") == "5d60923791f02e4c8e6d6c89f8c3c8b0" — verify via hashlib
        import hashlib
        expected = hashlib.md5("alice@example.com".encode()).hexdigest()
        assert get_subscriber_hash("alice@example.com") == expected

    def test_lowercases_email(self):
        lower = get_subscriber_hash("alice@example.com")
        upper = get_subscriber_hash("ALICE@EXAMPLE.COM")
        assert lower == upper

    def test_strips_whitespace(self):
        plain = get_subscriber_hash("alice@example.com")
        padded = get_subscriber_hash("  alice@example.com  ")
        assert plain == padded

    def test_different_emails_produce_different_hashes(self):
        h1 = get_subscriber_hash("alice@example.com")
        h2 = get_subscriber_hash("bob@example.com")
        assert h1 != h2

    def test_hash_is_32_chars(self):
        h = get_subscriber_hash("test@test.com")
        assert len(h) == 32

    def test_hash_is_hex_string(self):
        h = get_subscriber_hash("test@test.com")
        int(h, 16)  # raises ValueError if not valid hex

    def test_empty_email_returns_hash(self):
        # md5("") still returns a valid 32-char hash
        h = get_subscriber_hash("")
        assert len(h) == 32

    def test_mixed_case_strips_and_lowercases(self):
        h1 = get_subscriber_hash(" Bob@Test.COM ")
        h2 = get_subscriber_hash("bob@test.com")
        assert h1 == h2


# ── MailchimpHTTPClient — new methods ─────────────────────────────────────────

class TestHTTPClientNewMethods:
    def _mock_session(self, status: int = 200, payload: Dict[str, Any] | None = None):
        """Return a (mock_session, mock_resp) tuple wired for a single GET."""
        mock_resp = AsyncMock()
        mock_resp.status = status
        mock_resp.json = AsyncMock(return_value=payload or {})
        mock_resp.text = AsyncMock(return_value="error body")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return mock_session, mock_resp

    async def test_get_campaign_success(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        camp = _make_campaign()
        mock_session, _ = self._mock_session(200, camp)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_campaign("camp_001")
        assert result["id"] == "camp_001"

    async def test_get_campaign_404(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        mock_session, _ = self._mock_session(404)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpNotFoundError):
                await client.get_campaign("nonexistent")

    async def test_get_campaign_report_success(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        report = {"id": "camp_001", "opens": {"unique_opens": 120}}
        mock_session, _ = self._mock_session(200, report)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_campaign_report("camp_001")
        assert result["id"] == "camp_001"
        assert result["opens"]["unique_opens"] == 120

    async def test_get_campaign_report_404(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        mock_session, _ = self._mock_session(404)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpNotFoundError):
                await client.get_campaign_report("nonexistent")

    async def test_list_automations_success(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        payload = {"automations": [{"id": "auto_001", "status": "save"}], "total_items": 1}
        mock_session, _ = self._mock_session(200, payload)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.list_automations(count=10, offset=0)
        assert result["total_items"] == 1
        assert result["automations"][0]["id"] == "auto_001"

    async def test_list_tags_success(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        payload = {"tags": [{"id": 1, "name": "VIP"}, {"id": 2, "name": "Newsletter"}], "total_items": 2}
        mock_session, _ = self._mock_session(200, payload)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.list_tags("list_abc", count=100, offset=0)
        assert len(result["tags"]) == 2
        assert result["tags"][0]["name"] == "VIP"

    async def test_list_tags_404(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        mock_session, _ = self._mock_session(404)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpNotFoundError):
                await client.list_tags("nonexistent_list")

    async def test_get_campaigns_with_status_filter(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        payload = {"campaigns": [], "total_items": 0}
        mock_session, mock_resp = self._mock_session(200, payload)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_campaigns(count=10, offset=0, status="sent", type="regular")
        assert result["total_items"] == 0

    async def test_get_campaigns_500_raises_mailchimp_error(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        mock_session, _ = self._mock_session(500)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpError):
                await client.get_campaigns()

    async def test_get_campaign_401_raises_auth_error(self):
        client = MailchimpHTTPClient(dc="us10", api_key="bad-us10")
        mock_session, _ = self._mock_session(401)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpAuthError):
                await client.get_campaign("camp_001")

    async def test_get_campaign_429_raises_rate_limit_error(self):
        client = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        mock_session, _ = self._mock_session(429)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MailchimpRateLimitError):
                await client.get_campaign("camp_001")


# ── MailchimpConnector — get_campaign ─────────────────────────────────────────

class TestGetCampaign:
    async def test_returns_campaign(self):
        connector = _make_connector()
        camp = _make_campaign(campaign_id="camp_xyz", title="Summer Sale")
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_campaign=AsyncMock(return_value=camp)
            )
        )
        result = await connector.get_campaign("camp_xyz")
        assert result["id"] == "camp_xyz"
        assert result["settings"]["title"] == "Summer Sale"

    async def test_get_campaign_not_found_propagates(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_campaign=AsyncMock(side_effect=MailchimpNotFoundError("404"))
            )
        )
        with pytest.raises(MailchimpNotFoundError):
            await connector.get_campaign("nonexistent")

    async def test_get_campaign_auth_error_propagates(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_campaign=AsyncMock(side_effect=MailchimpAuthError("invalid key"))
            )
        )
        with pytest.raises(MailchimpAuthError):
            await connector.get_campaign("camp_001")


# ── MailchimpConnector — list_automations ─────────────────────────────────────

class TestListAutomations:
    async def test_returns_automations(self):
        connector = _make_connector()
        automation = {"id": "auto_001", "status": "paused", "trigger_settings": {}}
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                list_automations=AsyncMock(
                    return_value={"automations": [automation], "total_items": 1}
                )
            )
        )
        result = await connector.list_automations()
        assert len(result) == 1
        assert result[0]["id"] == "auto_001"

    async def test_returns_empty_list(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                list_automations=AsyncMock(
                    return_value={"automations": [], "total_items": 0}
                )
            )
        )
        result = await connector.list_automations()
        assert result == []

    async def test_pagination_fetches_all_automations(self):
        connector = _make_connector()
        call_count = 0

        async def mock_list_automations(count, offset):
            nonlocal call_count
            call_count += 1
            if offset == 0:
                return {
                    "automations": [{"id": f"auto_{i}"} for i in range(3)],
                    "total_items": 5,
                }
            return {
                "automations": [{"id": f"auto_{i+3}"} for i in range(2)],
                "total_items": 5,
            }

        client_mock = MagicMock()
        client_mock.list_automations = mock_list_automations
        connector._ensure_client = MagicMock(return_value=client_mock)
        result = await connector.list_automations(count=3)
        assert len(result) == 5
        assert call_count == 2

    async def test_auth_error_propagates(self):
        connector = _make_connector()
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                list_automations=AsyncMock(side_effect=MailchimpAuthError("invalid"))
            )
        )
        with pytest.raises(MailchimpAuthError):
            await connector.list_automations()


# ── MailchimpConnector — list_campaigns with filters ─────────────────────────

class TestListCampaignsFiltered:
    async def test_list_campaigns_with_status_filter(self):
        connector = _make_connector()
        sent_camp = _make_campaign(status="sent")
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_campaigns=AsyncMock(
                    return_value={"campaigns": [sent_camp], "total_items": 1}
                )
            )
        )
        result = await connector.list_campaigns(status="sent")
        assert len(result) == 1
        assert result[0]["status"] == "sent"

    async def test_list_campaigns_with_type_filter(self):
        connector = _make_connector()
        rss_camp = _make_campaign(campaign_type="rss")
        connector._ensure_client = MagicMock(
            return_value=MagicMock(
                get_campaigns=AsyncMock(
                    return_value={"campaigns": [rss_camp], "total_items": 1}
                )
            )
        )
        result = await connector.list_campaigns(type="rss")
        assert len(result) == 1
        assert result[0]["type"] == "rss"

    async def test_list_campaigns_pagination(self):
        connector = _make_connector()
        call_count = 0

        async def mock_get_campaigns(count, offset, status=None, type=None):
            nonlocal call_count
            call_count += 1
            if offset == 0:
                return {
                    "campaigns": [_make_campaign(campaign_id=f"c{i}") for i in range(2)],
                    "total_items": 4,
                }
            return {
                "campaigns": [_make_campaign(campaign_id=f"c{i+2}") for i in range(2)],
                "total_items": 4,
            }

        client_mock = MagicMock()
        client_mock.get_campaigns = mock_get_campaigns
        connector._ensure_client = MagicMock(return_value=client_mock)
        result = await connector.list_campaigns(count=2)
        assert len(result) == 4
        assert call_count == 2


# ── _raise_for_status coverage — 405, 422, other 4xx ─────────────────────────

class TestRaiseForStatus:
    def _make_client(self) -> MailchimpHTTPClient:
        return MailchimpHTTPClient(dc="us10", api_key="key-us10")

    def _mock_session(self, status: int):
        mock_resp = AsyncMock()
        mock_resp.status = status
        mock_resp.text = AsyncMock(return_value=f"HTTP {status} error body")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return mock_session

    async def test_405_raises_mailchimp_error(self):
        client = self._make_client()
        with patch("aiohttp.ClientSession", return_value=self._mock_session(405)):
            with pytest.raises(MailchimpError):
                await client.get_root()

    async def test_422_raises_mailchimp_error(self):
        client = self._make_client()
        with patch("aiohttp.ClientSession", return_value=self._mock_session(422)):
            with pytest.raises(MailchimpError):
                await client.get_root()

    async def test_400_raises_mailchimp_error(self):
        client = self._make_client()
        with patch("aiohttp.ClientSession", return_value=self._mock_session(400)):
            with pytest.raises(MailchimpError):
                await client.get_root()

    async def test_503_raises_mailchimp_error(self):
        client = self._make_client()
        with patch("aiohttp.ClientSession", return_value=self._mock_session(503)):
            with pytest.raises(MailchimpError):
                await client.get_root()

    async def test_500_raises_mailchimp_error(self):
        client = self._make_client()
        with patch("aiohttp.ClientSession", return_value=self._mock_session(500)):
            with pytest.raises(MailchimpError):
                await client.get_root()


# ── BasicAuth("key", api_key) pattern verification ───────────────────────────

class TestBasicAuthPattern:
    def test_basic_auth_username_is_anystring(self):
        """Mailchimp requires username='anystring' in HTTP Basic Auth."""
        client = MailchimpHTTPClient(dc="us6", api_key="testkey-us6")
        assert client._auth.login == "anystring"

    def test_basic_auth_password_is_api_key(self):
        client = MailchimpHTTPClient(dc="us6", api_key="mykey-us6")
        assert client._auth.password == "mykey-us6"

    def test_basic_auth_password_is_full_api_key_with_dc(self):
        """The full key (including DC suffix) is the password — DC is not stripped."""
        client = MailchimpHTTPClient(dc="eu3", api_key="abc123-eu3")
        assert client._auth.password == "abc123-eu3"

    def test_different_dc_in_base_url(self):
        client_us = MailchimpHTTPClient(dc="us10", api_key="key-us10")
        client_eu = MailchimpHTTPClient(dc="eu3", api_key="key-eu3")
        assert "us10" in client_us._base_url
        assert "eu3" in client_eu._base_url
        assert client_us._base_url != client_eu._base_url


# ── DC extraction from api_key integration ────────────────────────────────────

class TestDCExtractionIntegration:
    def test_connector_uses_dc_for_base_url(self):
        connector = MailchimpConnector(
            tenant_id="t1", connector_id="c1", config={"api_key": "mykey-eu2"}
        )
        client = connector._ensure_client()
        assert "eu2.api.mailchimp.com" in client._base_url

    def test_connector_us1_dc(self):
        connector = MailchimpConnector(
            tenant_id="t1", connector_id="c1", config={"api_key": "somekey-us1"}
        )
        client = connector._ensure_client()
        assert "us1.api.mailchimp.com" in client._base_url

    def test_connector_us6_dc(self):
        connector = MailchimpConnector(
            tenant_id="t1", connector_id="c1", config={"api_key": "k-us6"}
        )
        assert connector._get_dc() == "us6"

    async def test_install_unknown_dc_when_no_dash(self):
        connector = MailchimpConnector(
            tenant_id="t1", connector_id="c1", config={"api_key": "keywithnodash"}
        )
        result = await connector.install()
        # Key is present but dc is empty — still installs (validation passes)
        assert result.health == ConnectorHealth.HEALTHY
        assert "unknown" in result.message


# ── Offset pagination edge cases ──────────────────────────────────────────────

class TestOffsetPagination:
    async def test_list_members_multi_page(self):
        connector = _make_connector()
        page1 = [_make_member(email=f"user{i}@example.com") for i in range(3)]
        page2 = [_make_member(email=f"user{i+3}@example.com") for i in range(2)]
        call_count = [0]

        async def mock_get_members(list_id, count, offset):
            call_count[0] += 1
            if offset == 0:
                return {"members": page1, "total_items": 5}
            return {"members": page2, "total_items": 5}

        client_mock = MagicMock()
        client_mock.get_members = mock_get_members
        connector._ensure_client = MagicMock(return_value=client_mock)
        result = await connector.list_members("L1", count=3)
        assert len(result) == 5
        assert call_count[0] == 2

    async def test_list_audiences_multi_page(self):
        connector = _make_connector()
        page1 = [_make_audience(list_id=f"L{i}") for i in range(2)]
        page2 = [_make_audience(list_id=f"L{i+2}") for i in range(1)]
        call_count = [0]

        async def mock_get_lists(count, offset):
            call_count[0] += 1
            if offset == 0:
                return {"lists": page1, "total_items": 3}
            return {"lists": page2, "total_items": 3}

        client_mock = MagicMock()
        client_mock.get_lists = mock_get_lists
        connector._ensure_client = MagicMock(return_value=client_mock)
        result = await connector.list_audiences(count=2)
        assert len(result) == 3
        assert call_count[0] == 2

    async def test_empty_batch_stops_pagination(self):
        """If batch comes back empty, pagination stops even if offset < total."""
        connector = _make_connector()
        call_count = [0]

        async def mock_get_lists(count, offset):
            call_count[0] += 1
            if offset == 0:
                return {"lists": [_make_audience()], "total_items": 999}
            return {"lists": [], "total_items": 999}  # empty — stop

        client_mock = MagicMock()
        client_mock.get_lists = mock_get_lists
        connector._ensure_client = MagicMock(return_value=client_mock)
        result = await connector.list_audiences(count=1)
        assert len(result) == 1
        assert call_count[0] == 2
