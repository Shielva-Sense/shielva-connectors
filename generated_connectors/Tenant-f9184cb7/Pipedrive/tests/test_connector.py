"""Unit tests for PipedriveConnector — all Pipedrive HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes
- All model enum values and dataclass fields
- Normalizer functions for deals, persons, organizations (full and minimal records)
- stable_id utility
- Retry logic (success, retry-on-error, auth-error short-circuits, rate-limit)
- install() — missing creds, success, auth error, generic exception
- health_check() — success, auth error, network error, generic exception, circuit breaker
- sync() — empty, single page, pagination, normalize failure, COMPLETED vs PARTIAL vs FAILED
- list_deals / get_deal
- list_persons / get_person
- list_organizations
- list_activities
- aclose / context manager
- CircuitBreaker — threshold, reset, half-open, is_open
- _ensure_client
- _has_credentials
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import PipedriveConnector
from exceptions import (
    PipedriveAuthError,
    PipedriveError,
    PipedriveNetworkError,
    PipedriveNotFoundError,
    PipedriveRateLimitError,
    PipedriveServerError,
)
from helpers.normalizer import normalize_deal, normalize_organization, normalize_person
from helpers.utils import CircuitBreaker, stable_id, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    DealStatus,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "tenant_pipedrive_001"
CONNECTOR_ID = "conn_pipedrive_001"
VALID_API_KEY = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_DEAL: dict = {
    "id": 1,
    "title": "Enterprise License Deal",
    "value": 75000,
    "currency": "USD",
    "status": "open",
    "stage_id": {"id": 1, "name": "Proposal"},
    "pipeline_id": {"id": 1, "name": "Sales Pipeline"},
    "owner_name": "Alice Smith",
    "person_id": {"id": 10, "name": "Bob Jones"},
    "org_id": {"id": 20, "name": "Acme Corp"},
    "add_time": "2024-01-15 09:00:00",
    "close_time": None,
    "expected_close_date": "2024-12-31",
}

SAMPLE_PERSON: dict = {
    "id": 10,
    "name": "Bob Jones",
    "email": [{"value": "bob@acme.com", "label": "work", "primary": True}],
    "phone": [{"value": "+1-555-0100", "label": "work", "primary": True}],
    "org_id": {"id": 20, "name": "Acme Corp"},
    "owner_name": "Alice Smith",
    "add_time": "2024-01-01 08:00:00",
    "update_time": "2024-06-01 10:00:00",
}

SAMPLE_ORG: dict = {
    "id": 20,
    "name": "Acme Corp",
    "address": "123 Main St, San Francisco, CA",
    "owner_name": "Alice Smith",
    "add_time": "2023-06-01 00:00:00",
    "update_time": "2024-05-01 00:00:00",
    "people_count": 5,
    "open_deals_count": 2,
}

SAMPLE_ACTIVITY: dict = {
    "id": 30,
    "subject": "Follow-up call",
    "type": "call",
    "due_date": "2024-07-01",
    "done": False,
}

def _pd_page(data: list[dict], more: bool = False) -> dict:
    """Build a Pipedrive-style paginated response."""
    return {
        "success": True,
        "data": data,
        "additional_data": {
            "pagination": {
                "start": 0,
                "limit": 100,
                "more_items_in_collection": more,
                "next_start": len(data) if more else None,
            }
        },
    }

DEALS_PAGE = _pd_page([SAMPLE_DEAL])
PERSONS_PAGE = _pd_page([SAMPLE_PERSON])
ORGS_PAGE = _pd_page([SAMPLE_ORG])
EMPTY_PAGE = _pd_page([])


# ── Connector fixture ────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> PipedriveConnector:
    c = PipedriveConnector(
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
    assert PipedriveConnector.CONNECTOR_TYPE == "pipedrive"


def test_auth_type_attr() -> None:
    assert PipedriveConnector.AUTH_TYPE == "api_key"


def test_connector_stores_tenant_id() -> None:
    c = PipedriveConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = PipedriveConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_api_key_from_config() -> None:
    c = PipedriveConnector(config={"api_key": "test-token"})
    assert c._api_key == "test-token"


def test_connector_reads_company_domain_from_config() -> None:
    c = PipedriveConnector(config={"api_key": VALID_API_KEY, "company_domain": "mycompany"})
    assert c._company_domain == "mycompany"


def test_connector_company_domain_optional() -> None:
    c = PipedriveConnector(config={"api_key": VALID_API_KEY})
    assert c._company_domain == ""


def test_connector_no_http_client_initially() -> None:
    c = PipedriveConnector()
    assert c.http_client is None


def test_connector_default_empty_config() -> None:
    c = PipedriveConnector()
    assert c._api_key == ""
    assert c.tenant_id == ""
    assert c.connector_id == ""


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_pipedrive_error_base() -> None:
    exc = PipedriveError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_pipedrive_auth_error_is_pipedrive_error() -> None:
    exc = PipedriveAuthError("auth fail", 401, "UNAUTHORIZED")
    assert isinstance(exc, PipedriveError)
    assert exc.status_code == 401


def test_pipedrive_rate_limit_error_attrs() -> None:
    exc = PipedriveRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_pipedrive_rate_limit_error_default_retry_after() -> None:
    exc = PipedriveRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_pipedrive_not_found_error_message() -> None:
    exc = PipedriveNotFoundError("deal", "42")
    assert "42" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_pipedrive_network_error_is_pipedrive_error() -> None:
    exc = PipedriveNetworkError("timeout")
    assert isinstance(exc, PipedriveError)


def test_pipedrive_server_error_is_pipedrive_error() -> None:
    exc = PipedriveServerError("5xx", status_code=503)
    assert isinstance(exc, PipedriveError)
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


def test_deal_status_enum_values() -> None:
    assert DealStatus.ALL == "all"
    assert DealStatus.OPEN == "open"
    assert DealStatus.WON == "won"
    assert DealStatus.LOST == "lost"
    assert DealStatus.DELETED == "deleted"


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
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.message == "degraded"


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
        source_url="https://app.pipedrive.com/deal/1",
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
# 4. stable_id UTILITY
# ════════════════════════════════════════════════════════════════════════


def test_stable_id_length() -> None:
    sid = stable_id("deal", "1")
    assert len(sid) == 16


def test_stable_id_deterministic() -> None:
    assert stable_id("deal", "42") == stable_id("deal", "42")


def test_stable_id_different_types() -> None:
    assert stable_id("deal", "1") != stable_id("person", "1")


def test_stable_id_different_ids() -> None:
    assert stable_id("deal", "1") != stable_id("deal", "2")


def test_stable_id_hex_chars() -> None:
    sid = stable_id("organization", "99")
    assert all(c in "0123456789abcdef" for c in sid)


# ════════════════════════════════════════════════════════════════════════
# 5. NORMALIZERS — deals
# ════════════════════════════════════════════════════════════════════════


def test_normalize_deal_title() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert "Enterprise License Deal" in doc.title


def test_normalize_deal_source_id_is_stable() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("deal", "1")


def test_normalize_deal_tenant_connector() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_deal_metadata_object_type() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "deal"


def test_normalize_deal_metadata_status() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["status"] == "open"


def test_normalize_deal_metadata_value() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["value"] == "75000"


def test_normalize_deal_source_url() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert "pipedrive.com" in doc.source_url
    assert "1" in doc.source_url


def test_normalize_deal_content_has_title() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert "Enterprise License Deal" in doc.content


def test_normalize_deal_minimal_record() -> None:
    doc = normalize_deal({"id": 99, "title": ""}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("deal", "99")
    assert "Deal 99" in doc.title


def test_normalize_deal_stage_from_dict() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["stage"] == "Proposal"


def test_normalize_deal_org_from_dict() -> None:
    doc = normalize_deal(SAMPLE_DEAL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["organization"] == "Acme Corp"


# ════════════════════════════════════════════════════════════════════════
# 6. NORMALIZERS — persons
# ════════════════════════════════════════════════════════════════════════


def test_normalize_person_title() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert "Bob Jones" in doc.title
    assert "bob@acme.com" in doc.title


def test_normalize_person_source_id_is_stable() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("person", "10")


def test_normalize_person_metadata_object_type() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "person"


def test_normalize_person_metadata_email() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == "bob@acme.com"


def test_normalize_person_metadata_phone() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["phone"] == "+1-555-0100"


def test_normalize_person_source_url() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert "pipedrive.com" in doc.source_url
    assert "10" in doc.source_url


def test_normalize_person_minimal_record() -> None:
    doc = normalize_person({"id": 99, "name": ""}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("person", "99")
    assert "Person 99" in doc.title


def test_normalize_person_no_email() -> None:
    record = {**SAMPLE_PERSON, "email": []}
    doc = normalize_person(record, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == ""


def test_normalize_person_no_phone() -> None:
    record = {**SAMPLE_PERSON, "phone": []}
    doc = normalize_person(record, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["phone"] == ""


def test_normalize_person_org_from_dict() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["organization"] == "Acme Corp"


# ════════════════════════════════════════════════════════════════════════
# 7. NORMALIZERS — organizations
# ════════════════════════════════════════════════════════════════════════


def test_normalize_org_title() -> None:
    doc = normalize_organization(SAMPLE_ORG, CONNECTOR_ID, TENANT_ID)
    assert "Acme Corp" in doc.title


def test_normalize_org_source_id_is_stable() -> None:
    doc = normalize_organization(SAMPLE_ORG, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("organization", "20")


def test_normalize_org_metadata_object_type() -> None:
    doc = normalize_organization(SAMPLE_ORG, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "organization"


def test_normalize_org_metadata_address() -> None:
    doc = normalize_organization(SAMPLE_ORG, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["address"] == "123 Main St, San Francisco, CA"


def test_normalize_org_source_url() -> None:
    doc = normalize_organization(SAMPLE_ORG, CONNECTOR_ID, TENANT_ID)
    assert "pipedrive.com" in doc.source_url
    assert "20" in doc.source_url


def test_normalize_org_minimal_record() -> None:
    doc = normalize_organization({"id": 99, "name": ""}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("organization", "99")
    assert "Organization 99" in doc.title


def test_normalize_org_people_count() -> None:
    doc = normalize_organization(SAMPLE_ORG, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["people_count"] == "5"


# ════════════════════════════════════════════════════════════════════════
# 8. RETRY LOGIC
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_retries_on_pipedrive_error() -> None:
    fn = AsyncMock(side_effect=[PipedriveNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=PipedriveAuthError("auth fail", 401))
    with pytest.raises(PipedriveAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=PipedriveNetworkError("timeout"))
    with pytest.raises(PipedriveNetworkError):
        await with_retry(fn, max_retries=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[PipedriveRateLimitError("rl", retry_after=0), {"done": True}]
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
# 9. install()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    connector = PipedriveConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.PipedriveHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            return_value={"success": True, "data": {"id": 1, "name": "Alice"}}
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Pipedrive" in result.message


@pytest.mark.asyncio
async def test_install_missing_credentials() -> None:
    connector = PipedriveConnector(config={}, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    connector = PipedriveConnector(
        config={"api_key": "bad-token"},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.PipedriveHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=PipedriveAuthError("Authentication failed", 401)
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_exception_fallback() -> None:
    connector = PipedriveConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.PipedriveHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success() -> None:
    connector = PipedriveConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.PipedriveHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            return_value={"success": True, "data": {"id": 1}}
        )
        instance.aclose = AsyncMock()
        await connector.install()
    assert connector.http_client is not None


# ════════════════════════════════════════════════════════════════════════
# 10. health_check()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(authed: PipedriveConnector) -> None:
    with patch("connector.PipedriveHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            return_value={"success": True, "data": {"id": 1}}
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_key(authed: PipedriveConnector) -> None:
    with patch("connector.PipedriveHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=PipedriveAuthError("Invalid token", 401)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: PipedriveConnector) -> None:
    with patch("connector.PipedriveHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=PipedriveNetworkError("timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    connector = PipedriveConnector(config={})
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: PipedriveConnector) -> None:
    with patch("connector.PipedriveHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(side_effect=RuntimeError("boom"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_increments_circuit_breaker_on_failure(
    authed: PipedriveConnector,
) -> None:
    with patch("connector.PipedriveHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=PipedriveNetworkError("timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures >= 1


@pytest.mark.asyncio
async def test_health_check_resets_circuit_breaker_on_success(
    authed: PipedriveConnector,
) -> None:
    for _ in range(3):
        authed._circuit_breaker.on_failure()
    with patch("connector.PipedriveHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            return_value={"success": True, "data": {"id": 1}}
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures == 0


# ════════════════════════════════════════════════════════════════════════
# 11. sync()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(authed: PipedriveConnector) -> None:
    authed.http_client.list_deals = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.list_persons = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.list_organizations = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_data(authed: PipedriveConnector) -> None:
    authed.http_client.list_deals = AsyncMock(return_value=DEALS_PAGE)
    authed.http_client.list_persons = AsyncMock(return_value=PERSONS_PAGE)
    authed.http_client.list_organizations = AsyncMock(return_value=ORGS_PAGE)
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_pagination(authed: PipedriveConnector) -> None:
    page1 = _pd_page([SAMPLE_DEAL], more=True)
    page2 = _pd_page([{**SAMPLE_DEAL, "id": 2}])
    authed.http_client.list_deals = AsyncMock(side_effect=[page1, page2])
    authed.http_client.list_persons = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.list_organizations = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert authed.http_client.list_deals.call_count == 2


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(authed: PipedriveConnector) -> None:
    bad_record: dict = {"id": None}  # Will produce a bad stable_id but won't crash
    # Force normalization to fail by passing a record with problematic nested access
    authed.http_client.list_deals = AsyncMock(
        return_value={"success": True, "data": [bad_record],
                      "additional_data": {"pagination": {"more_items_in_collection": False}}}
    )
    authed.http_client.list_persons = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.list_organizations = AsyncMock(return_value=EMPTY_PAGE)

    # Patch normalize_deal to always raise
    with patch("connector.normalize_deal", side_effect=Exception("norm fail")):
        result = await authed.sync(full=True)

    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_status_completed_when_no_failures(authed: PipedriveConnector) -> None:
    authed.http_client.list_deals = AsyncMock(return_value=DEALS_PAGE)
    authed.http_client.list_persons = AsyncMock(return_value=PERSONS_PAGE)
    authed.http_client.list_organizations = AsyncMock(return_value=ORGS_PAGE)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_fetch_error_returns_failed(authed: PipedriveConnector) -> None:
    authed.http_client.list_deals = AsyncMock(
        side_effect=PipedriveError("API gone", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    connector = PipedriveConnector(
        config={"api_key": VALID_API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.list_deals = AsyncMock(return_value=EMPTY_PAGE)
    mock_client.list_persons = AsyncMock(return_value=EMPTY_PAGE)
    mock_client.list_organizations = AsyncMock(return_value=EMPTY_PAGE)
    connector._make_client = lambda: mock_client
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_counts_found_correctly(authed: PipedriveConnector) -> None:
    authed.http_client.list_deals = AsyncMock(
        return_value=_pd_page([SAMPLE_DEAL, {**SAMPLE_DEAL, "id": 2}])
    )
    authed.http_client.list_persons = AsyncMock(return_value=PERSONS_PAGE)
    authed.http_client.list_organizations = AsyncMock(return_value=ORGS_PAGE)
    result = await authed.sync(full=True)
    assert result.documents_found == 4  # 2 deals + 1 person + 1 org


# ════════════════════════════════════════════════════════════════════════
# 12. list_deals / get_deal
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_deals(authed: PipedriveConnector) -> None:
    authed.http_client.list_deals = AsyncMock(return_value=DEALS_PAGE)
    result = await authed.list_deals(status="all", limit=10)
    assert result["data"][0]["id"] == 1


@pytest.mark.asyncio
async def test_list_deals_passes_status(authed: PipedriveConnector) -> None:
    authed.http_client.list_deals = AsyncMock(return_value=DEALS_PAGE)
    await authed.list_deals(status="open", limit=10, start=0)
    authed.http_client.list_deals.assert_called_once_with(status="open", limit=10, start=0)


@pytest.mark.asyncio
async def test_get_deal(authed: PipedriveConnector) -> None:
    authed.http_client.get_deal = AsyncMock(
        return_value={"success": True, "data": SAMPLE_DEAL}
    )
    result = await authed.get_deal(1)
    assert result["data"]["id"] == 1
    assert result["data"]["title"] == "Enterprise License Deal"


@pytest.mark.asyncio
async def test_get_deal_string_id(authed: PipedriveConnector) -> None:
    authed.http_client.get_deal = AsyncMock(
        return_value={"success": True, "data": SAMPLE_DEAL}
    )
    await authed.get_deal("1")
    authed.http_client.get_deal.assert_called_once_with("1")


# ════════════════════════════════════════════════════════════════════════
# 13. list_persons / get_person
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_persons(authed: PipedriveConnector) -> None:
    authed.http_client.list_persons = AsyncMock(return_value=PERSONS_PAGE)
    result = await authed.list_persons(limit=10)
    assert result["data"][0]["id"] == 10


@pytest.mark.asyncio
async def test_list_persons_with_start(authed: PipedriveConnector) -> None:
    authed.http_client.list_persons = AsyncMock(return_value=PERSONS_PAGE)
    await authed.list_persons(limit=5, start=50)
    authed.http_client.list_persons.assert_called_once_with(limit=5, start=50)


@pytest.mark.asyncio
async def test_get_person(authed: PipedriveConnector) -> None:
    authed.http_client.get_person = AsyncMock(
        return_value={"success": True, "data": SAMPLE_PERSON}
    )
    result = await authed.get_person(10)
    assert result["data"]["name"] == "Bob Jones"


# ════════════════════════════════════════════════════════════════════════
# 14. list_organizations
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_organizations(authed: PipedriveConnector) -> None:
    authed.http_client.list_organizations = AsyncMock(return_value=ORGS_PAGE)
    result = await authed.list_organizations(limit=10)
    assert result["data"][0]["id"] == 20
    assert result["data"][0]["name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_list_organizations_with_start(authed: PipedriveConnector) -> None:
    authed.http_client.list_organizations = AsyncMock(return_value=ORGS_PAGE)
    await authed.list_organizations(limit=10, start=100)
    authed.http_client.list_organizations.assert_called_once_with(limit=10, start=100)


# ════════════════════════════════════════════════════════════════════════
# 15. list_activities
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_activities(authed: PipedriveConnector) -> None:
    activities_page = _pd_page([SAMPLE_ACTIVITY])
    authed.http_client.list_activities = AsyncMock(return_value=activities_page)
    result = await authed.list_activities(limit=10)
    assert result["data"][0]["id"] == 30
    assert result["data"][0]["subject"] == "Follow-up call"


@pytest.mark.asyncio
async def test_list_activities_with_start(authed: PipedriveConnector) -> None:
    authed.http_client.list_activities = AsyncMock(return_value=EMPTY_PAGE)
    await authed.list_activities(limit=50, start=200)
    authed.http_client.list_activities.assert_called_once_with(limit=50, start=200)


# ════════════════════════════════════════════════════════════════════════
# 16. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: PipedriveConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    connector = PipedriveConnector(config={"api_key": VALID_API_KEY})
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    connector = PipedriveConnector(
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
# 17. CircuitBreaker
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
# 18. _ensure_client / _has_credentials
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    connector = PipedriveConnector(config={"api_key": VALID_API_KEY})
    mock_client = MagicMock()
    connector._make_client = lambda: mock_client
    client = connector._ensure_client()
    assert client is mock_client
    assert connector.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    connector = PipedriveConnector(config={"api_key": VALID_API_KEY})
    existing = MagicMock()
    connector.http_client = existing
    client = connector._ensure_client()
    assert client is existing


def test_has_credentials_true_with_api_key() -> None:
    c = PipedriveConnector(config={"api_key": "test-token"})
    assert c._has_credentials() is True


def test_has_credentials_false_when_empty() -> None:
    c = PipedriveConnector(config={})
    assert c._has_credentials() is False


def test_has_credentials_false_with_no_config() -> None:
    c = PipedriveConnector()
    assert c._has_credentials() is False


# ════════════════════════════════════════════════════════════════════════
# 19. api_token config key (install_fields key)
# ════════════════════════════════════════════════════════════════════════


def test_connector_reads_api_token_from_config() -> None:
    """ACP passes api_token (the install_field key); connector must accept it."""
    c = PipedriveConnector(config={"api_token": "tok-from-acp"})
    assert c._api_key == "tok-from-acp"


def test_connector_api_token_takes_precedence_over_api_key() -> None:
    """When both are present api_token wins (ACP canonical key)."""
    c = PipedriveConnector(config={"api_token": "tok-acp", "api_key": "tok-legacy"})
    assert c._api_key == "tok-acp"


def test_has_credentials_true_with_api_token() -> None:
    c = PipedriveConnector(config={"api_token": "some-token"})
    assert c._has_credentials() is True


# ════════════════════════════════════════════════════════════════════════
# 20. Bearer auth header
# ════════════════════════════════════════════════════════════════════════


def test_http_client_sets_bearer_auth_header() -> None:
    """HTTP client must include Authorization: Bearer <token> header."""
    from client.http_client import PipedriveHTTPClient

    client = PipedriveHTTPClient(api_key="my-secret-token")
    auth_header = client._client.headers.get("authorization", "")
    assert auth_header == "Bearer my-secret-token"
    # Ensure no api_token query param is baked into defaults
    assert "api_token" not in str(client._client.base_url)


def test_http_client_bearer_header_with_empty_key() -> None:
    """Bearer header is set even with an empty key (will 401 at the API level)."""
    from client.http_client import PipedriveHTTPClient

    client = PipedriveHTTPClient(api_key="")
    auth_header = client._client.headers.get("authorization", "")
    assert auth_header == "Bearer "


# ════════════════════════════════════════════════════════════════════════
# 21. list_pipelines / list_stages
# ════════════════════════════════════════════════════════════════════════

SAMPLE_PIPELINE = {"id": 1, "name": "Sales Pipeline", "active": True, "order_nr": 0}
SAMPLE_STAGE = {"id": 1, "name": "Proposal", "pipeline_id": 1, "order_nr": 0}
PIPELINES_RESPONSE = {"success": True, "data": [SAMPLE_PIPELINE]}
STAGES_RESPONSE = {"success": True, "data": [SAMPLE_STAGE]}


@pytest.mark.asyncio
async def test_list_pipelines_returns_data(authed: PipedriveConnector) -> None:
    authed.http_client.list_pipelines = AsyncMock(return_value=PIPELINES_RESPONSE)
    result = await authed.list_pipelines()
    assert result["data"][0]["id"] == 1
    assert result["data"][0]["name"] == "Sales Pipeline"


@pytest.mark.asyncio
async def test_list_pipelines_calls_client(authed: PipedriveConnector) -> None:
    authed.http_client.list_pipelines = AsyncMock(return_value=PIPELINES_RESPONSE)
    await authed.list_pipelines()
    authed.http_client.list_pipelines.assert_called_once()


@pytest.mark.asyncio
async def test_list_stages_returns_data(authed: PipedriveConnector) -> None:
    authed.http_client.list_stages = AsyncMock(return_value=STAGES_RESPONSE)
    result = await authed.list_stages()
    assert result["data"][0]["id"] == 1
    assert result["data"][0]["name"] == "Proposal"


@pytest.mark.asyncio
async def test_list_stages_calls_client(authed: PipedriveConnector) -> None:
    authed.http_client.list_stages = AsyncMock(return_value=STAGES_RESPONSE)
    await authed.list_stages()
    authed.http_client.list_stages.assert_called_once()


@pytest.mark.asyncio
async def test_list_pipelines_creates_client_if_none() -> None:
    """list_pipelines must lazy-create the HTTP client when none exists."""
    connector = PipedriveConnector(config={"api_key": VALID_API_KEY})
    mock_client = MagicMock()
    mock_client.list_pipelines = AsyncMock(return_value=PIPELINES_RESPONSE)
    connector._make_client = lambda: mock_client
    result = await connector.list_pipelines()
    assert result["data"][0]["id"] == 1


@pytest.mark.asyncio
async def test_list_stages_creates_client_if_none() -> None:
    """list_stages must lazy-create the HTTP client when none exists."""
    connector = PipedriveConnector(config={"api_key": VALID_API_KEY})
    mock_client = MagicMock()
    mock_client.list_stages = AsyncMock(return_value=STAGES_RESPONSE)
    connector._make_client = lambda: mock_client
    result = await connector.list_stages()
    assert result["data"][0]["id"] == 1
