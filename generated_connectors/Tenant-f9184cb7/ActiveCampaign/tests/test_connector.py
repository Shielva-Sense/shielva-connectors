"""Unit tests for ActiveCampaignConnector — all HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes
- All model enum values and dataclass fields
- stable_id helper
- Normalizer functions for contacts, deals, campaigns (full and minimal records)
- Retry logic (success, retry-on-error, auth-error short-circuits, rate-limit)
- install() — missing creds, success, auth error, generic exception
- health_check() — success (user name/email), auth error, network error, generic exception
- sync() — empty, single page, pagination, normalize failure, COMPLETED vs PARTIAL, FAILED
- list_contacts, get_contact
- list_deals, get_deal
- list_campaigns, list_lists, list_automations, list_tags
- aclose / context manager
- CircuitBreaker — threshold, reset, half-open, is_open
- _ensure_client, _has_credentials
- HTTP client construction (account_name → base_url)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import ActiveCampaignConnector
from exceptions import (
    ActiveCampaignAuthError,
    ActiveCampaignError,
    ActiveCampaignNetworkError,
    ActiveCampaignNotFoundError,
    ActiveCampaignRateLimitError,
    ActiveCampaignServerError,
)
from helpers.normalizer import normalize_campaign, normalize_contact, normalize_deal
from helpers.utils import CircuitBreaker, stable_id, with_retry
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
CONNECTOR_ID = "conn_activecampaign_test_001"
VALID_ACCOUNT_NAME = "testaccount"
VALID_API_KEY = "test_api_key_abc123xyz"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_CONTACT: dict = {
    "id": "101",
    "firstName": "Jane",
    "lastName": "Doe",
    "email": "jane@example.com",
    "phone": "+1-555-0100",
    "orgname": "Acme Corp",
    "cdate": "2024-01-01T00:00:00-05:00",
    "udate": "2024-06-01T00:00:00-05:00",
}

SAMPLE_DEAL: dict = {
    "id": "201",
    "title": "Enterprise Deal",
    "value": "50000",
    "currency": "usd",
    "status": "0",
    "stage": "1",
    "owner": "42",
    "cdate": "2024-01-15T00:00:00-05:00",
    "mdate": "2024-06-10T00:00:00-05:00",
}

SAMPLE_CAMPAIGN: dict = {
    "id": "301",
    "name": "Q2 Newsletter",
    "type": "single",
    "status": "3",
    "subject": "Big news from Acme",
    "send_amt": "5000",
    "opens": "1200",
    "cdate": "2024-04-01T00:00:00-05:00",
}

SAMPLE_AUTOMATION: dict = {
    "id": "401",
    "name": "Welcome Series",
    "status": "1",
    "cdate": "2024-01-10T00:00:00-05:00",
    "mdate": "2024-05-01T00:00:00-05:00",
}

SAMPLE_TAG: dict = {
    "id": "501",
    "tag": "VIP Customer",
    "tagType": "contact",
}

ME_RESPONSE: dict = {
    "user": {
        "firstName": "Admin",
        "lastName": "User",
        "email": "admin@testaccount.com",
    }
}

CONTACTS_PAGE: dict = {"contacts": [SAMPLE_CONTACT], "meta": {"total": "1"}}
DEALS_PAGE: dict = {"deals": [SAMPLE_DEAL], "meta": {"total": "1"}}
CAMPAIGNS_PAGE: dict = {"campaigns": [SAMPLE_CAMPAIGN], "meta": {"total": "1"}}
LISTS_PAGE: dict = {"lists": [{"id": "1", "name": "Main List"}], "meta": {"total": "1"}}
AUTOMATIONS_PAGE: dict = {"automations": [SAMPLE_AUTOMATION], "meta": {"total": "1"}}
TAGS_PAGE: dict = {"tags": [SAMPLE_TAG], "meta": {"total": "1"}}
EMPTY_CONTACTS: dict = {"contacts": [], "meta": {"total": "0"}}
EMPTY_DEALS: dict = {"deals": [], "meta": {"total": "0"}}
EMPTY_CAMPAIGNS: dict = {"campaigns": [], "meta": {"total": "0"}}


# ── Connector fixture ────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> ActiveCampaignConnector:
    c = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert ActiveCampaignConnector.CONNECTOR_TYPE == "activecampaign"


def test_auth_type_attr() -> None:
    assert ActiveCampaignConnector.AUTH_TYPE == "api_key"


def test_connector_stores_tenant_id() -> None:
    c = ActiveCampaignConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = ActiveCampaignConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_api_key_from_config() -> None:
    c = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME}
    )
    assert c._api_key == VALID_API_KEY


def test_connector_reads_account_name_from_config() -> None:
    c = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME}
    )
    assert c._account_name == VALID_ACCOUNT_NAME


def test_connector_no_http_client_initially() -> None:
    c = ActiveCampaignConnector()
    assert c.http_client is None


def test_connector_empty_config_defaults() -> None:
    c = ActiveCampaignConnector()
    assert c._api_key == ""
    assert c._account_name == ""


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_activecampaign_error_base() -> None:
    exc = ActiveCampaignError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_activecampaign_auth_error_is_base() -> None:
    exc = ActiveCampaignAuthError("auth fail", 401, "UNAUTHORIZED")
    assert isinstance(exc, ActiveCampaignError)
    assert exc.status_code == 401


def test_activecampaign_rate_limit_error_attrs() -> None:
    exc = ActiveCampaignRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_activecampaign_rate_limit_error_default_retry_after() -> None:
    exc = ActiveCampaignRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_activecampaign_not_found_error_message() -> None:
    exc = ActiveCampaignNotFoundError("contact", "101")
    assert "101" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_activecampaign_network_error_is_base() -> None:
    exc = ActiveCampaignNetworkError("timeout")
    assert isinstance(exc, ActiveCampaignError)


def test_activecampaign_server_error_is_base() -> None:
    exc = ActiveCampaignServerError("5xx", status_code=503)
    assert isinstance(exc, ActiveCampaignError)
    assert exc.status_code == 503


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
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="degraded",
        user_name="Admin User",
        user_email="admin@test.com",
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.user_name == "Admin User"
    assert r.user_email == "admin@test.com"


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
# 4. stable_id HELPER
# ════════════════════════════════════════════════════════════════════════


def test_stable_id_is_16_chars() -> None:
    sid = stable_id("contact", "101")
    assert len(sid) == 16


def test_stable_id_is_deterministic() -> None:
    assert stable_id("contact", "101") == stable_id("contact", "101")


def test_stable_id_differs_by_prefix() -> None:
    assert stable_id("contact", "101") != stable_id("deal", "101")


def test_stable_id_differs_by_resource_id() -> None:
    assert stable_id("contact", "101") != stable_id("contact", "102")


def test_stable_id_is_hex() -> None:
    sid = stable_id("campaign", "999")
    int(sid, 16)  # raises ValueError if not hex


def test_stable_id_activecampaign_contact_prefix() -> None:
    """Verify the canonical prefix documented in the spec."""
    import hashlib
    expected = hashlib.sha256("contact:101".encode()).hexdigest()[:16]
    assert stable_id("contact", "101") == expected


# ════════════════════════════════════════════════════════════════════════
# 5. NORMALIZERS — contacts
# ════════════════════════════════════════════════════════════════════════


def test_normalize_contact_title() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "Jane Doe" in doc.title
    assert "jane@example.com" in doc.title


def test_normalize_contact_source_id_is_stable() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("contact", "101")
    assert len(doc.source_id) == 16


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


def test_normalize_contact_metadata_ac_id() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["ac_id"] == "101"


def test_normalize_contact_content_has_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "jane@example.com" in doc.content


def test_normalize_contact_minimal_record() -> None:
    doc = normalize_contact({"id": "999"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("contact", "999")
    assert "Unknown" in doc.title


def test_normalize_contact_no_last_name() -> None:
    record = {"id": "102", "firstName": "Solo", "email": "solo@x.com"}
    doc = normalize_contact(record, CONNECTOR_ID, TENANT_ID)
    assert "Solo" in doc.title


# ════════════════════════════════════════════════════════════════════════
# 6. NORMALIZERS — deals
# ════════════════════════════════════════════════════════════════════════


def test_normalize_deal_title() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert "Enterprise Deal" in doc.title


def test_normalize_deal_source_id_is_stable() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("deal", "201")


def test_normalize_deal_metadata_object_type() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "deal"


def test_normalize_deal_metadata_status_mapped() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["status"] == "open"


def test_normalize_deal_metadata_value() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["value"] == "50000"


def test_normalize_deal_status_won() -> None:
    record = {**SAMPLE_DEAL, "status": "1"}
    doc = normalize_deal(record, CONNECTOR_ID, TENANT_ID)
    assert "won" in doc.metadata["status"]


def test_normalize_deal_status_lost() -> None:
    record = {**SAMPLE_DEAL, "status": "2"}
    doc = normalize_deal(record, CONNECTOR_ID, TENANT_ID)
    assert "lost" in doc.metadata["status"]


def test_normalize_deal_minimal_record() -> None:
    doc = normalize_deal({"id": "999"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("deal", "999")
    assert "Deal 999" in doc.title


# ════════════════════════════════════════════════════════════════════════
# 7. NORMALIZERS — campaigns
# ════════════════════════════════════════════════════════════════════════


def test_normalize_campaign_title() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert "Q2 Newsletter" in doc.title


def test_normalize_campaign_source_id_is_stable() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("campaign", "301")


def test_normalize_campaign_metadata_object_type() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "campaign"


def test_normalize_campaign_status_sent() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["status"] == "sent"


def test_normalize_campaign_status_draft() -> None:
    record = {**SAMPLE_CAMPAIGN, "status": "0"}
    doc = normalize_campaign(record, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["status"] == "draft"


def test_normalize_campaign_metadata_subject() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["subject"] == "Big news from Acme"


def test_normalize_campaign_minimal_record() -> None:
    doc = normalize_campaign({"id": "999"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("campaign", "999")
    assert "Campaign 999" in doc.title


# ════════════════════════════════════════════════════════════════════════
# 8. RETRY LOGIC
# ════════════════════════════════════════════════════════════════════════


async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


async def test_retry_retries_on_network_error() -> None:
    fn = AsyncMock(side_effect=[ActiveCampaignNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=ActiveCampaignAuthError("auth fail", 401))
    with pytest.raises(ActiveCampaignAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert fn.call_count == 1


async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=ActiveCampaignNetworkError("timeout"))
    with pytest.raises(ActiveCampaignNetworkError):
        await with_retry(fn, max_retries=2, base_delay=0)
    assert fn.call_count == 2


async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[ActiveCampaignRateLimitError("rl", retry_after=0), {"done": True}]
    )
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"done": True}
    mock_sleep.assert_called_once()


async def test_retry_with_args_and_kwargs() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", max_retries=1, base_delay=0, kwarg1="val")
    fn.assert_called_once_with("arg1", kwarg1="val")
    assert result == "result"


async def test_retry_server_error_retried() -> None:
    fn = AsyncMock(
        side_effect=[
            ActiveCampaignServerError("5xx", 500),
            ActiveCampaignServerError("5xx", 500),
            {"ok": True},
        ]
    )
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 3


# ════════════════════════════════════════════════════════════════════════
# 9. install()
# ════════════════════════════════════════════════════════════════════════


async def test_install_success() -> None:
    connector = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "ActiveCampaign" in result.message


async def test_install_missing_api_key() -> None:
    connector = ActiveCampaignConnector(
        config={"account_name": VALID_ACCOUNT_NAME},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "required" in result.message


async def test_install_missing_account_name() -> None:
    connector = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "required" in result.message


async def test_install_missing_both_credentials() -> None:
    connector = ActiveCampaignConnector(config={})
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


async def test_install_invalid_credentials() -> None:
    connector = ActiveCampaignConnector(
        config={"api_key": "bad_key", "account_name": VALID_ACCOUNT_NAME},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(
            side_effect=ActiveCampaignAuthError("Authentication failed", 401)
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


async def test_install_exception_fallback() -> None:
    connector = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


async def test_install_sets_http_client_on_success() -> None:
    connector = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        await connector.install()
    assert connector.http_client is not None


# ════════════════════════════════════════════════════════════════════════
# 10. health_check()
# ════════════════════════════════════════════════════════════════════════


async def test_health_check_healthy(authed: ActiveCampaignConnector) -> None:
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


async def test_health_check_returns_user_name(authed: ActiveCampaignConnector) -> None:
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.user_name == "Admin User"


async def test_health_check_returns_user_email(authed: ActiveCampaignConnector) -> None:
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.user_email == "admin@testaccount.com"


async def test_health_check_invalid_key(authed: ActiveCampaignConnector) -> None:
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(
            side_effect=ActiveCampaignAuthError("Invalid token", 401)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


async def test_health_check_network_error(authed: ActiveCampaignConnector) -> None:
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(
            side_effect=ActiveCampaignNetworkError("timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)


async def test_health_check_missing_credentials() -> None:
    connector = ActiveCampaignConnector(config={})
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


async def test_health_check_generic_exception(authed: ActiveCampaignConnector) -> None:
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=RuntimeError("boom"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


async def test_health_check_increments_circuit_breaker_on_failure(
    authed: ActiveCampaignConnector,
) -> None:
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(
            side_effect=ActiveCampaignNetworkError("timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures >= 1


async def test_health_check_resets_circuit_breaker_on_success(
    authed: ActiveCampaignConnector,
) -> None:
    for _ in range(3):
        authed._circuit_breaker.on_failure()
    with patch("connector.ActiveCampaignHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures == 0


# ════════════════════════════════════════════════════════════════════════
# 11. sync()
# ════════════════════════════════════════════════════════════════════════


async def test_sync_empty(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_contacts = AsyncMock(return_value=EMPTY_CONTACTS)
    authed.http_client.list_deals = AsyncMock(return_value=EMPTY_DEALS)
    authed.http_client.list_campaigns = AsyncMock(return_value=EMPTY_CAMPAIGNS)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


async def test_sync_with_data(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    authed.http_client.list_deals = AsyncMock(return_value=DEALS_PAGE)
    authed.http_client.list_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


async def test_sync_pagination(authed: ActiveCampaignConnector) -> None:
    """Two pages of contacts — offset advances until total is reached."""
    page1 = {"contacts": [SAMPLE_CONTACT], "meta": {"total": "2"}}
    page2 = {"contacts": [{**SAMPLE_CONTACT, "id": "102"}], "meta": {"total": "2"}}
    authed.http_client.list_contacts = AsyncMock(side_effect=[page1, page2])
    authed.http_client.list_deals = AsyncMock(return_value=EMPTY_DEALS)
    authed.http_client.list_campaigns = AsyncMock(return_value=EMPTY_CAMPAIGNS)
    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert authed.http_client.list_contacts.call_count == 2


async def test_sync_normalize_failure_increments_failed(
    authed: ActiveCampaignConnector,
) -> None:
    authed.http_client.list_contacts = AsyncMock(return_value=EMPTY_CONTACTS)
    authed.http_client.list_deals = AsyncMock(
        return_value={"deals": [SAMPLE_DEAL], "meta": {"total": "1"}}
    )
    authed.http_client.list_campaigns = AsyncMock(return_value=EMPTY_CAMPAIGNS)
    with patch("connector.normalize_deal", side_effect=ValueError("bad")):
        result = await authed.sync(full=True)
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


async def test_sync_status_completed_when_no_failures(
    authed: ActiveCampaignConnector,
) -> None:
    authed.http_client.list_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    authed.http_client.list_deals = AsyncMock(return_value=DEALS_PAGE)
    authed.http_client.list_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


async def test_sync_fetch_error_returns_failed(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_contacts = AsyncMock(
        side_effect=ActiveCampaignError("API gone", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED


async def test_sync_creates_http_client_if_none() -> None:
    connector = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.list_contacts = AsyncMock(return_value=EMPTY_CONTACTS)
    mock_client.list_deals = AsyncMock(return_value=EMPTY_DEALS)
    mock_client.list_campaigns = AsyncMock(return_value=EMPTY_CAMPAIGNS)
    connector._make_client = lambda: mock_client
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


async def test_sync_counts_found_correctly(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_contacts = AsyncMock(
        return_value={
            "contacts": [SAMPLE_CONTACT, SAMPLE_CONTACT],
            "meta": {"total": "2"},
        }
    )
    authed.http_client.list_deals = AsyncMock(return_value=DEALS_PAGE)
    authed.http_client.list_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    result = await authed.sync(full=True)
    assert result.documents_found == 4  # 2 contacts + 1 deal + 1 campaign


async def test_sync_with_offset_pagination_calls_correct_offsets(
    authed: ActiveCampaignConnector,
) -> None:
    """Verify offset advances per page."""
    page1 = {"contacts": [SAMPLE_CONTACT], "meta": {"total": "2"}}
    page2 = {"contacts": [{**SAMPLE_CONTACT, "id": "102"}], "meta": {"total": "2"}}
    authed.http_client.list_contacts = AsyncMock(side_effect=[page1, page2])
    authed.http_client.list_deals = AsyncMock(return_value=EMPTY_DEALS)
    authed.http_client.list_campaigns = AsyncMock(return_value=EMPTY_CAMPAIGNS)
    await authed.sync(full=True)
    calls = authed.http_client.list_contacts.call_args_list
    assert calls[0].kwargs["offset"] == 0
    assert calls[1].kwargs["offset"] == 1


# ════════════════════════════════════════════════════════════════════════
# 12. list_contacts / get_contact
# ════════════════════════════════════════════════════════════════════════


async def test_list_contacts(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    result = await authed.list_contacts(limit=10)
    assert result["contacts"][0]["id"] == "101"


async def test_list_contacts_with_offset(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    await authed.list_contacts(limit=10, offset=20)
    authed.http_client.list_contacts.assert_called_once_with(limit=10, offset=20)


async def test_get_contact(authed: ActiveCampaignConnector) -> None:
    authed.http_client.get_contact = AsyncMock(
        return_value={"contact": SAMPLE_CONTACT}
    )
    result = await authed.get_contact("101")
    assert result["contact"]["id"] == "101"


async def test_get_contact_calls_client_with_id(
    authed: ActiveCampaignConnector,
) -> None:
    authed.http_client.get_contact = AsyncMock(
        return_value={"contact": SAMPLE_CONTACT}
    )
    await authed.get_contact("101")
    authed.http_client.get_contact.assert_called_once_with("101")


# ════════════════════════════════════════════════════════════════════════
# 13. list_deals / get_deal
# ════════════════════════════════════════════════════════════════════════


async def test_list_deals(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_deals = AsyncMock(return_value=DEALS_PAGE)
    result = await authed.list_deals(limit=10)
    assert result["deals"][0]["id"] == "201"


async def test_list_deals_with_offset(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_deals = AsyncMock(return_value=DEALS_PAGE)
    await authed.list_deals(limit=5, offset=10)
    authed.http_client.list_deals.assert_called_once_with(limit=5, offset=10)


async def test_get_deal(authed: ActiveCampaignConnector) -> None:
    authed.http_client.get_deal = AsyncMock(return_value={"deal": SAMPLE_DEAL})
    result = await authed.get_deal("201")
    assert result["deal"]["id"] == "201"


# ════════════════════════════════════════════════════════════════════════
# 14. list_campaigns
# ════════════════════════════════════════════════════════════════════════


async def test_list_campaigns(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    result = await authed.list_campaigns(limit=10)
    assert result["campaigns"][0]["id"] == "301"


async def test_list_campaigns_with_offset(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_campaigns = AsyncMock(return_value=CAMPAIGNS_PAGE)
    await authed.list_campaigns(limit=5, offset=15)
    authed.http_client.list_campaigns.assert_called_once_with(limit=5, offset=15)


# ════════════════════════════════════════════════════════════════════════
# 15. list_lists
# ════════════════════════════════════════════════════════════════════════


async def test_list_lists(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_lists = AsyncMock(return_value=LISTS_PAGE)
    result = await authed.list_lists(limit=10)
    assert result["lists"][0]["name"] == "Main List"


async def test_list_lists_with_offset(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_lists = AsyncMock(return_value=LISTS_PAGE)
    await authed.list_lists(limit=5, offset=5)
    authed.http_client.list_lists.assert_called_once_with(limit=5, offset=5)


# ════════════════════════════════════════════════════════════════════════
# 16. list_automations
# ════════════════════════════════════════════════════════════════════════


async def test_list_automations(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_automations = AsyncMock(return_value=AUTOMATIONS_PAGE)
    result = await authed.list_automations(limit=10)
    assert result["automations"][0]["id"] == "401"
    assert result["automations"][0]["name"] == "Welcome Series"


async def test_list_automations_with_offset(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_automations = AsyncMock(return_value=AUTOMATIONS_PAGE)
    await authed.list_automations(limit=50, offset=100)
    authed.http_client.list_automations.assert_called_once_with(limit=50, offset=100)


async def test_list_automations_empty(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_automations = AsyncMock(
        return_value={"automations": [], "meta": {"total": "0"}}
    )
    result = await authed.list_automations()
    assert result["automations"] == []


# ════════════════════════════════════════════════════════════════════════
# 17. list_tags
# ════════════════════════════════════════════════════════════════════════


async def test_list_tags(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_tags = AsyncMock(return_value=TAGS_PAGE)
    result = await authed.list_tags(limit=10)
    assert result["tags"][0]["id"] == "501"
    assert result["tags"][0]["tag"] == "VIP Customer"


async def test_list_tags_with_offset(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_tags = AsyncMock(return_value=TAGS_PAGE)
    await authed.list_tags(limit=50, offset=50)
    authed.http_client.list_tags.assert_called_once_with(limit=50, offset=50)


async def test_list_tags_empty(authed: ActiveCampaignConnector) -> None:
    authed.http_client.list_tags = AsyncMock(
        return_value={"tags": [], "meta": {"total": "0"}}
    )
    result = await authed.list_tags()
    assert result["tags"] == []


# ════════════════════════════════════════════════════════════════════════
# 18. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


async def test_aclose_calls_http_client_aclose(authed: ActiveCampaignConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


async def test_aclose_noop_when_no_client() -> None:
    connector = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME}
    )
    await connector.aclose()
    assert connector.http_client is None


async def test_context_manager() -> None:
    connector = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME},
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
# 19. CircuitBreaker
# ════════════════════════════════════════════════════════════════════════


def test_circuit_breaker_starts_closed() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_opens_on_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(5):
        cb.on_failure()
    assert cb.state == "open"


def test_circuit_breaker_closes_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(5):
        cb.on_failure()
    cb.on_success()
    assert cb.state == "closed"
    assert cb._failures == 0


def test_circuit_breaker_is_open_property() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    assert not cb.is_open
    for _ in range(3):
        cb.on_failure()
    assert cb.is_open


def test_circuit_breaker_half_open_after_timeout() -> None:
    import time
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.01)
    cb.on_failure()
    assert cb.state == "open"
    time.sleep(0.05)
    assert cb.state == "half-open"


def test_circuit_breaker_failure_below_threshold_stays_closed() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(4):
        cb.on_failure()
    assert cb.state == "closed"


def test_circuit_breaker_custom_recovery_timeout() -> None:
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=999.0)
    cb.on_failure()
    assert cb.state == "open"
    assert cb.state == "open"  # still open — timeout not elapsed


# ════════════════════════════════════════════════════════════════════════
# 20. _ensure_client / _has_credentials
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    connector = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME}
    )
    mock_client = MagicMock()
    connector._make_client = lambda: mock_client
    client = connector._ensure_client()
    assert client is mock_client
    assert connector.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    connector = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME}
    )
    existing = MagicMock()
    connector.http_client = existing
    client = connector._ensure_client()
    assert client is existing


def test_has_credentials_true_with_both() -> None:
    c = ActiveCampaignConnector(
        config={"api_key": VALID_API_KEY, "account_name": VALID_ACCOUNT_NAME}
    )
    assert c._has_credentials() is True


def test_has_credentials_false_missing_api_key() -> None:
    c = ActiveCampaignConnector(config={"account_name": VALID_ACCOUNT_NAME})
    assert c._has_credentials() is False


def test_has_credentials_false_missing_account_name() -> None:
    c = ActiveCampaignConnector(config={"api_key": VALID_API_KEY})
    assert c._has_credentials() is False


def test_has_credentials_false_when_empty() -> None:
    c = ActiveCampaignConnector(config={})
    assert c._has_credentials() is False


# ════════════════════════════════════════════════════════════════════════
# 21. HTTP client — base URL construction from account_name
# ════════════════════════════════════════════════════════════════════════


def test_http_client_base_url_from_account_name() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="key", account_name="mycompany")
    assert client._base_url == "https://mycompany.api-activecampaign.com/api/3"


def test_http_client_different_account_names() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    c1 = ActiveCampaignHTTPClient(api_key="k", account_name="acme")
    c2 = ActiveCampaignHTTPClient(api_key="k", account_name="corp")
    assert "acme" in c1._base_url
    assert "corp" in c2._base_url
    assert c1._base_url != c2._base_url


def test_http_client_stores_api_key() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="mykey", account_name="acct")
    assert client._api_key == "mykey"


def test_http_client_stores_account_name() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="k", account_name="myaccount")
    assert client._account_name == "myaccount"


def test_http_client_headers_contain_api_token() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="superkey", account_name="acct")
    assert client._headers["Api-Token"] == "superkey"


# ════════════════════════════════════════════════════════════════════════
# 22. Error mapping in _raise_for_status
# ════════════════════════════════════════════════════════════════════════


def test_raise_for_status_401_raises_auth_error() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="k", account_name="a")
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.url = "https://example.com/api/3/users/me"
    with pytest.raises(ActiveCampaignAuthError):
        client._raise_for_status(mock_response, {})


def test_raise_for_status_403_raises_auth_error() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="k", account_name="a")
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.url = "https://example.com/api/3/contacts"
    with pytest.raises(ActiveCampaignAuthError):
        client._raise_for_status(mock_response, {})


def test_raise_for_status_404_raises_not_found() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="k", account_name="a")
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.url = "https://example.com/api/3/contacts/999"
    with pytest.raises(ActiveCampaignNotFoundError):
        client._raise_for_status(mock_response, {})


def test_raise_for_status_429_raises_rate_limit() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="k", account_name="a")
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "10"}
    mock_response.url = "https://example.com/api/3/contacts"
    exc = None
    with pytest.raises(ActiveCampaignRateLimitError) as exc_info:
        client._raise_for_status(mock_response, {})
    exc = exc_info.value
    assert exc.retry_after == 10.0


def test_raise_for_status_500_raises_server_error() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="k", account_name="a")
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.url = "https://example.com/api/3/contacts"
    with pytest.raises(ActiveCampaignServerError):
        client._raise_for_status(mock_response, {})


def test_raise_for_status_200_returns_body() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="k", account_name="a")
    mock_response = MagicMock()
    mock_response.status = 200
    result = client._raise_for_status(mock_response, {"contacts": []})
    assert result == {"contacts": []}


def test_raise_for_status_204_returns_empty_dict() -> None:
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="k", account_name="a")
    mock_response = MagicMock()
    mock_response.status = 204
    result = client._raise_for_status(mock_response, {})
    assert result == {}


def test_raise_for_status_ac_errors_array() -> None:
    """AC errors array format: {"errors": [{"title": "...", "code": "..."}]}"""
    from client.http_client import ActiveCampaignHTTPClient

    client = ActiveCampaignHTTPClient(api_key="k", account_name="a")
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.url = "https://example.com/api/3/users/me"
    body = {"errors": [{"title": "No Result found for User with id 0", "code": "user_not_found"}]}
    with pytest.raises(ActiveCampaignAuthError) as exc_info:
        client._raise_for_status(mock_response, body)
    assert "No Result found" in str(exc_info.value)
