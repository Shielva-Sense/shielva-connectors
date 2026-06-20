"""Unit tests for KlaviyoConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import KlaviyoConnector, _extract_cursor, _normalize_list
from exceptions import KlaviyoAuthError, KlaviyoError, KlaviyoNetworkError, KlaviyoRateLimitError
from helpers.utils import CircuitBreaker, _stable_id, normalize_campaign, normalize_profile
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_klaviyo_test_001"
VALID_API_KEY = "pk_abc123def456ghi789jkl012mno345"

# ── Sample data ─────────────────────────────────────────────────────────────

SAMPLE_PROFILE_RESOURCE: dict = {
    "type": "profile",
    "id": "profile_abc123",
    "attributes": {
        "email": "jane@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "phone_number": "+15551234567",
        "created": "2024-01-15T10:00:00+00:00",
        "updated": "2024-06-01T09:00:00+00:00",
        "location": {
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
        },
    },
}

SAMPLE_PROFILE_RESPONSE: dict = {
    "data": SAMPLE_PROFILE_RESOURCE,
}

SAMPLE_PROFILE_LIST_RESPONSE: dict = {
    "data": [SAMPLE_PROFILE_RESOURCE],
    "links": {"self": "https://a.klaviyo.com/api/profiles?page[size]=100", "next": None},
}

SAMPLE_CAMPAIGN_RESOURCE: dict = {
    "type": "campaign",
    "id": "campaign_xyz789",
    "attributes": {
        "name": "Summer Sale 2024",
        "status": "sent",
        "scheduled_at": "2024-07-01T12:00:00+00:00",
        "created_at": "2024-06-25T08:00:00+00:00",
        "updated_at": "2024-07-01T12:00:00+00:00",
    },
}

SAMPLE_CAMPAIGN_LIST_RESPONSE: dict = {
    "data": [SAMPLE_CAMPAIGN_RESOURCE],
    "links": {"self": "https://a.klaviyo.com/api/campaigns", "next": None},
}

SAMPLE_LIST_RESOURCE: dict = {
    "type": "list",
    "id": "list_abc456",
    "attributes": {
        "name": "Newsletter Subscribers",
        "created": "2024-01-01T00:00:00+00:00",
        "updated": "2024-06-01T00:00:00+00:00",
    },
}

SAMPLE_LISTS_RESPONSE: dict = {
    "data": [SAMPLE_LIST_RESOURCE],
    "links": {"self": "https://a.klaviyo.com/api/lists", "next": None},
}

SAMPLE_ACCOUNTS_RESPONSE: dict = {
    "data": [
        {
            "type": "account",
            "id": "acct_test_001",
            "attributes": {
                "contact_information": {
                    "organization_name": "Acme Corp",
                },
            },
        }
    ]
}

SAMPLE_SEGMENTS_RESPONSE: dict = {
    "data": [
        {
            "type": "segment",
            "id": "seg_001",
            "attributes": {
                "name": "Active Buyers",
                "created": "2024-02-01T00:00:00+00:00",
            },
        }
    ],
    "links": {"next": None},
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> KlaviyoConnector:
    c = KlaviyoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": VALID_API_KEY},
    )
    c.http_client = MagicMock()
    return c


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = KlaviyoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={"api_key": VALID_API_KEY})
    with patch("connector.KlaviyoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Klaviyo" in result.message
    assert "Acme Corp" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = KlaviyoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key is required" in result.message


@pytest.mark.asyncio
async def test_install_invalid_key_format() -> None:
    c = KlaviyoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={"api_key": "sk_invalid_key"})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "pk_" in result.message


@pytest.mark.asyncio
async def test_install_auth_error() -> None:
    c = KlaviyoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={"api_key": VALID_API_KEY})
    with patch("connector.KlaviyoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_accounts = AsyncMock(side_effect=KlaviyoAuthError("Unauthorized", 401))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_generic_exception() -> None:
    c = KlaviyoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={"api_key": VALID_API_KEY})
    with patch("connector.KlaviyoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_accounts = AsyncMock(side_effect=Exception("network failure"))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED
    assert "network failure" in result.message


# ── health_check() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: KlaviyoConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_accounts=AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Acme Corp" in result.message


@pytest.mark.asyncio
async def test_health_check_missing_api_key() -> None:
    c = KlaviyoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: KlaviyoConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_accounts=AsyncMock(side_effect=KlaviyoAuthError("Unauthorized", 401)),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: KlaviyoConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_accounts=AsyncMock(side_effect=KlaviyoNetworkError("timeout")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_no_account_name(connector: KlaviyoConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_accounts=AsyncMock(return_value={"data": []}),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert "Connected to Klaviyo" in result.message


# ── sync() ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector: KlaviyoConnector) -> None:
    connector.http_client.list_profiles = AsyncMock(return_value={"data": [], "links": {"next": None}})
    connector.http_client.list_campaigns = AsyncMock(return_value={"data": [], "links": {"next": None}})
    connector.http_client.list_lists = AsyncMock(return_value={"data": [], "links": {"next": None}})
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_profiles(connector: KlaviyoConnector) -> None:
    connector.http_client.list_profiles = AsyncMock(return_value=SAMPLE_PROFILE_LIST_RESPONSE)
    connector.http_client.list_campaigns = AsyncMock(return_value={"data": [], "links": {"next": None}})
    connector.http_client.list_lists = AsyncMock(return_value={"data": [], "links": {"next": None}})
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_all_resources(connector: KlaviyoConnector) -> None:
    connector.http_client.list_profiles = AsyncMock(return_value=SAMPLE_PROFILE_LIST_RESPONSE)
    connector.http_client.list_campaigns = AsyncMock(return_value=SAMPLE_CAMPAIGN_LIST_RESPONSE)
    connector.http_client.list_lists = AsyncMock(return_value=SAMPLE_LISTS_RESPONSE)
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3


@pytest.mark.asyncio
async def test_sync_profile_pagination(connector: KlaviyoConnector) -> None:
    page1 = {
        "data": [SAMPLE_PROFILE_RESOURCE],
        "links": {"next": "https://a.klaviyo.com/api/profiles?page[cursor]=cursor_page2"},
    }
    page2 = {
        "data": [
            {
                "type": "profile",
                "id": "profile_xyz456",
                "attributes": {
                    "email": "john@example.com",
                    "first_name": "John",
                    "last_name": "Smith",
                    "created": "2024-02-01T10:00:00+00:00",
                },
            }
        ],
        "links": {"next": None},
    }
    connector.http_client.list_profiles = AsyncMock(side_effect=[page1, page2])
    connector.http_client.list_campaigns = AsyncMock(return_value={"data": [], "links": {"next": None}})
    connector.http_client.list_lists = AsyncMock(return_value={"data": [], "links": {"next": None}})
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert connector.http_client.list_profiles.call_count == 2


@pytest.mark.asyncio
async def test_sync_profiles_api_failure(connector: KlaviyoConnector) -> None:
    connector.http_client.list_profiles = AsyncMock(
        side_effect=KlaviyoError("API error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_campaigns_failure_nonfatal(connector: KlaviyoConnector) -> None:
    connector.http_client.list_profiles = AsyncMock(return_value=SAMPLE_PROFILE_LIST_RESPONSE)
    connector.http_client.list_campaigns = AsyncMock(side_effect=KlaviyoError("campaigns error", 500))
    connector.http_client.list_lists = AsyncMock(return_value=SAMPLE_LISTS_RESPONSE)
    result = await connector.sync(full=True)
    # Profiles + lists should still be synced; campaigns failure is non-fatal
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found >= 1


@pytest.mark.asyncio
async def test_sync_with_kb_id(connector: KlaviyoConnector) -> None:
    connector.http_client.list_profiles = AsyncMock(return_value=SAMPLE_PROFILE_LIST_RESPONSE)
    connector.http_client.list_campaigns = AsyncMock(return_value={"data": [], "links": {"next": None}})
    connector.http_client.list_lists = AsyncMock(return_value={"data": [], "links": {"next": None}})
    connector._ingest_document = AsyncMock()
    result = await connector.sync(full=True, kb_id="kb_test_001")
    assert result.documents_synced == 1
    connector._ingest_document.assert_called_once()


# ── list_profiles() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_profiles_no_cursor(connector: KlaviyoConnector) -> None:
    connector.http_client.list_profiles = AsyncMock(return_value=SAMPLE_PROFILE_LIST_RESPONSE)
    result = await connector.list_profiles(page_size=50)
    assert "data" in result
    assert len(result["data"]) == 1
    connector.http_client.list_profiles.assert_called_once_with(page_size=50, cursor=None)


@pytest.mark.asyncio
async def test_list_profiles_with_cursor(connector: KlaviyoConnector) -> None:
    connector.http_client.list_profiles = AsyncMock(return_value=SAMPLE_PROFILE_LIST_RESPONSE)
    result = await connector.list_profiles(page_size=100, cursor="cursor_abc123")
    assert "data" in result
    connector.http_client.list_profiles.assert_called_once_with(page_size=100, cursor="cursor_abc123")


# ── get_profile() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_profile(connector: KlaviyoConnector) -> None:
    connector.http_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE_RESPONSE)
    result = await connector.get_profile("profile_abc123")
    assert result["data"]["id"] == "profile_abc123"
    connector.http_client.get_profile.assert_called_once_with("profile_abc123")


# ── list_lists() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_lists(connector: KlaviyoConnector) -> None:
    connector.http_client.list_lists = AsyncMock(return_value=SAMPLE_LISTS_RESPONSE)
    result = await connector.list_lists(page_size=50)
    assert "data" in result
    assert result["data"][0]["id"] == "list_abc456"
    connector.http_client.list_lists.assert_called_once_with(page_size=50)


# ── get_list() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_list(connector: KlaviyoConnector) -> None:
    single_list = {"data": SAMPLE_LIST_RESOURCE}
    connector.http_client.get_list = AsyncMock(return_value=single_list)
    result = await connector.get_list("list_abc456")
    assert result["data"]["id"] == "list_abc456"
    connector.http_client.get_list.assert_called_once_with("list_abc456")


# ── list_campaigns() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_campaigns(connector: KlaviyoConnector) -> None:
    connector.http_client.list_campaigns = AsyncMock(return_value=SAMPLE_CAMPAIGN_LIST_RESPONSE)
    result = await connector.list_campaigns(page_size=100)
    assert "data" in result
    assert result["data"][0]["id"] == "campaign_xyz789"
    connector.http_client.list_campaigns.assert_called_once_with(page_size=100)


# ── list_segments() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_segments(connector: KlaviyoConnector) -> None:
    connector.http_client.list_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
    result = await connector.list_segments(page_size=100)
    assert "data" in result
    assert result["data"][0]["id"] == "seg_001"
    connector.http_client.list_segments.assert_called_once_with(page_size=100)


# ── normalize_profile() ──────────────────────────────────────────────────────


def test_normalize_profile_full() -> None:
    doc = normalize_profile(SAMPLE_PROFILE_RESOURCE, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Klaviyo profile: Jane Doe <jane@example.com>"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["email"] == "jane@example.com"
    assert doc.metadata["first_name"] == "Jane"
    assert doc.metadata["last_name"] == "Doe"
    assert "San Francisco" in doc.content
    assert "profile_abc123" in doc.source_url
    # Stable ID check: SHA-256("profile_abc123")[:16]
    assert len(doc.source_id) == 16


def test_normalize_profile_wrapped_response() -> None:
    doc = normalize_profile(SAMPLE_PROFILE_RESPONSE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == "jane@example.com"


def test_normalize_profile_minimal() -> None:
    minimal = {
        "type": "profile",
        "id": "profile_min001",
        "attributes": {"email": "min@test.com"},
    }
    doc = normalize_profile(minimal, CONNECTOR_ID, TENANT_ID)
    assert "min@test.com" in doc.title
    assert doc.metadata["phone_number"] == ""


def test_normalize_profile_no_email() -> None:
    no_email = {
        "type": "profile",
        "id": "profile_noemail",
        "attributes": {"first_name": "Ghost"},
    }
    doc = normalize_profile(no_email, CONNECTOR_ID, TENANT_ID)
    assert "Ghost" in doc.title
    assert "<" not in doc.title  # no email angle brackets


# ── normalize_campaign() ─────────────────────────────────────────────────────


def test_normalize_campaign_full() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN_RESOURCE, CONNECTOR_ID, TENANT_ID)
    assert "Summer Sale 2024" in doc.title
    assert doc.metadata["status"] == "sent"
    assert doc.metadata["campaign_id"] == "campaign_xyz789"
    assert "campaign_xyz789" in doc.source_url
    assert len(doc.source_id) == 16


def test_normalize_campaign_wrapped() -> None:
    wrapped = {"data": SAMPLE_CAMPAIGN_RESOURCE}
    doc = normalize_campaign(wrapped, CONNECTOR_ID, TENANT_ID)
    assert "Summer Sale 2024" in doc.title


def test_normalize_campaign_minimal() -> None:
    minimal = {
        "type": "campaign",
        "id": "campaign_min",
        "attributes": {"name": "Minimal Campaign", "status": "draft"},
    }
    doc = normalize_campaign(minimal, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["status"] == "draft"
    assert doc.metadata["scheduled_at"] == ""


# ── _normalize_list() ────────────────────────────────────────────────────────


def test_normalize_list() -> None:
    doc = _normalize_list(SAMPLE_LIST_RESOURCE, CONNECTOR_ID, TENANT_ID)
    assert "Newsletter Subscribers" in doc.title
    assert doc.metadata["list_id"] == "list_abc456"
    assert "list_abc456" in doc.source_url
    assert len(doc.source_id) == 16


# ── _stable_id() ─────────────────────────────────────────────────────────────


def test_stable_id_length() -> None:
    sid = _stable_id("some_id_value")
    assert len(sid) == 16


def test_stable_id_deterministic() -> None:
    assert _stable_id("profile_abc123") == _stable_id("profile_abc123")


def test_stable_id_unique() -> None:
    assert _stable_id("profile_abc123") != _stable_id("profile_xyz456")


# ── _extract_cursor() ────────────────────────────────────────────────────────


def test_extract_cursor_valid() -> None:
    url = "https://a.klaviyo.com/api/profiles?page[size]=100&page[cursor]=WzE2MjQ4ODYyMDBd"
    cursor = _extract_cursor(url)
    assert cursor == "WzE2MjQ4ODYyMDBd"


def test_extract_cursor_no_cursor() -> None:
    url = "https://a.klaviyo.com/api/profiles?page[size]=100"
    assert _extract_cursor(url) == ""


def test_extract_cursor_empty_string() -> None:
    assert _extract_cursor("") == ""


# ── CircuitBreaker ───────────────────────────────────────────────────────────


def test_circuit_breaker_initial_state() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_opens_at_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    cb.on_failure()
    cb.on_failure()
    assert cb.state == "closed"
    cb.on_failure()
    assert cb.state == "open"
    assert cb.is_open


def test_circuit_breaker_closes_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    for _ in range(3):
        cb.on_failure()
    assert cb.is_open
    cb.on_success()
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_default_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(4):
        cb.on_failure()
    assert cb.state == "closed"
    cb.on_failure()
    assert cb.state == "open"


# ── aclose() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_clears_client(connector: KlaviyoConnector) -> None:
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    connector.http_client = mock_client
    await connector.aclose()
    mock_client.aclose.assert_called_once()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_aclose_no_client() -> None:
    c = KlaviyoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={"api_key": VALID_API_KEY})
    # Should not raise even with no client
    await c.aclose()


# ── Context manager ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_manager() -> None:
    c = KlaviyoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={"api_key": VALID_API_KEY})
    async with c as conn:
        assert conn is c
    # http_client may be None if never used (no _make_client call)
    assert c.http_client is None


# ── _ensure_client() ─────────────────────────────────────────────────────────


def test_ensure_client_creates_client() -> None:
    c = KlaviyoConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={"api_key": VALID_API_KEY})
    assert c.http_client is None
    client = c._ensure_client()
    assert client is not None
    assert c.http_client is client


def test_ensure_client_reuses_existing(connector: KlaviyoConnector) -> None:
    original = connector.http_client
    returned = connector._ensure_client()
    assert returned is original


# ── _stable_id() additional ───────────────────────────────────────────────────


def test_stable_id_hex_only() -> None:
    """Output must contain only lowercase hex digits."""
    sid = _stable_id("arbitrary_input_value")
    assert all(c in "0123456789abcdef" for c in sid)


def test_stable_id_exactly_16_chars() -> None:
    """16 characters — not 15, not 17."""
    assert len(_stable_id("x")) == 16
    assert len(_stable_id("a" * 200)) == 16


def test_stable_id_determinism_across_calls() -> None:
    """Multiple calls with the same input always yield the same result."""
    key = "profile_determinism_test"
    results = [_stable_id(key) for _ in range(5)]
    assert len(set(results)) == 1


def test_stable_id_different_inputs_differ() -> None:
    """Two distinct inputs must not collide (for these test values)."""
    assert _stable_id("campaign_aaa") != _stable_id("campaign_bbb")
    assert _stable_id("list_001") != _stable_id("list_002")


# ── normalize_profile() edge cases ───────────────────────────────────────────


def test_normalize_profile_missing_email_no_angle_brackets() -> None:
    """When email is absent the title must not contain < or >."""
    resource = {
        "type": "profile",
        "id": "p_no_email",
        "attributes": {"first_name": "Alice", "last_name": "Wonder"},
    }
    doc = normalize_profile(resource, CONNECTOR_ID, TENANT_ID)
    assert "<" not in doc.title
    assert ">" not in doc.title
    assert "Alice Wonder" in doc.title


def test_normalize_profile_missing_first_and_last_name() -> None:
    """When both name fields are absent the profile falls back to 'Unknown'."""
    resource = {
        "type": "profile",
        "id": "p_no_name",
        "attributes": {"email": "noname@example.com"},
    }
    doc = normalize_profile(resource, CONNECTOR_ID, TENANT_ID)
    assert "Unknown" in doc.title
    assert "noname@example.com" in doc.title


def test_normalize_profile_all_optional_fields_absent() -> None:
    """Profile with only an id and no attributes still produces a valid document."""
    resource = {"type": "profile", "id": "p_bare", "attributes": {}}
    doc = normalize_profile(resource, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("p_bare")
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["email"] == ""
    assert doc.metadata["phone_number"] == ""


def test_normalize_profile_only_last_name() -> None:
    """A profile with only a last name does not produce a leading space in title."""
    resource = {
        "type": "profile",
        "id": "p_last_only",
        "attributes": {"last_name": "Smith", "email": "s@x.com"},
    }
    doc = normalize_profile(resource, CONNECTOR_ID, TENANT_ID)
    assert "Smith" in doc.title
    assert doc.title == doc.title.strip()


# ── normalize_campaign() edge cases ──────────────────────────────────────────


def test_normalize_campaign_missing_subject_uses_default_name() -> None:
    """A campaign with no 'name' attribute falls back to 'Unnamed Campaign'."""
    resource = {
        "type": "campaign",
        "id": "camp_no_name",
        "attributes": {"status": "draft"},
    }
    doc = normalize_campaign(resource, CONNECTOR_ID, TENANT_ID)
    assert "Unnamed Campaign" in doc.title


def test_normalize_campaign_missing_send_time_is_empty_string() -> None:
    """When scheduled_at and send_time are both absent, scheduled_at metadata is ''."""
    resource = {
        "type": "campaign",
        "id": "camp_no_time",
        "attributes": {"name": "No Time Campaign", "status": "draft"},
    }
    doc = normalize_campaign(resource, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["scheduled_at"] == ""


def test_normalize_campaign_send_time_fallback_to_send_time_field() -> None:
    """When scheduled_at absent but send_time present, send_time is used."""
    resource = {
        "type": "campaign",
        "id": "camp_send_time",
        "attributes": {
            "name": "Fallback Campaign",
            "status": "sent",
            "send_time": "2024-08-01T10:00:00+00:00",
        },
    }
    doc = normalize_campaign(resource, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["scheduled_at"] == "2024-08-01T10:00:00+00:00"


# ── with_retry() behaviour ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_exhaustion_raises_last_error() -> None:
    """After max_attempts the last KlaviyoError is re-raised."""
    call_count = 0

    async def flaky() -> dict:  # type: ignore[type-arg]
        nonlocal call_count
        call_count += 1
        raise KlaviyoError("transient", 503)

    from helpers.utils import with_retry

    with pytest.raises(KlaviyoError, match="transient"):
        await with_retry(flaky, max_attempts=3, base_delay=0)
    assert call_count == 3


@pytest.mark.asyncio
async def test_with_retry_auth_error_no_retry() -> None:
    """KlaviyoAuthError is re-raised immediately without retrying."""
    call_count = 0

    async def always_auth_fail() -> dict:  # type: ignore[type-arg]
        nonlocal call_count
        call_count += 1
        raise KlaviyoAuthError("bad key", 401)

    from helpers.utils import with_retry

    with pytest.raises(KlaviyoAuthError):
        await with_retry(always_auth_fail, max_attempts=3, base_delay=0)
    assert call_count == 1


# ── _extract_cursor() edge cases ─────────────────────────────────────────────


def test_extract_cursor_no_cursor_param_returns_empty() -> None:
    """URL with query params but no page[cursor] returns ''."""
    url = "https://a.klaviyo.com/api/profiles?page[size]=100&filter=foo"
    assert _extract_cursor(url) == ""


def test_extract_cursor_malformed_url_returns_empty() -> None:
    """A URL that cannot be meaningfully parsed returns '' rather than raising."""
    # urlparse is lenient; an empty query produces no cursor
    assert _extract_cursor("not_a_real_url") == ""


def test_extract_cursor_cursor_present_with_special_chars() -> None:
    """Cursor values that contain encoded characters are returned as-is."""
    url = "https://a.klaviyo.com/api/profiles?page[cursor]=abc%3D%3D&page[size]=100"
    cursor = _extract_cursor(url)
    # The value should be the decoded/raw query param value
    assert cursor != ""
    assert "abc" in cursor


# ── list_lists() errors ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_lists_network_error(connector: KlaviyoConnector) -> None:
    """A KlaviyoNetworkError from list_lists bubbles up to the caller."""
    connector.http_client.list_lists = AsyncMock(side_effect=KlaviyoNetworkError("timeout"))
    with pytest.raises(KlaviyoNetworkError):
        await connector.list_lists()


@pytest.mark.asyncio
async def test_list_lists_returns_correct_page_size(connector: KlaviyoConnector) -> None:
    """list_lists passes the requested page_size through to the HTTP client."""
    connector.http_client.list_lists = AsyncMock(return_value=SAMPLE_LISTS_RESPONSE)
    await connector.list_lists(page_size=25)
    connector.http_client.list_lists.assert_called_once_with(page_size=25)


# ── list_segments() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_segments_custom_page_size(connector: KlaviyoConnector) -> None:
    """list_segments passes the requested page_size through to the HTTP client."""
    connector.http_client.list_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
    await connector.list_segments(page_size=50)
    connector.http_client.list_segments.assert_called_once_with(page_size=50)


@pytest.mark.asyncio
async def test_list_segments_returns_segment_data(connector: KlaviyoConnector) -> None:
    """list_segments result includes the segment item from the mock response."""
    connector.http_client.list_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
    result = await connector.list_segments()
    assert result["data"][0]["attributes"]["name"] == "Active Buyers"


# ── install() key format ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_key_without_pk_prefix_returns_invalid_credentials() -> None:
    """An API key that does not start with 'pk_' is rejected before any HTTP call."""
    c = KlaviyoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": "live_abc123"},  # no pk_ prefix
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "pk_" in result.message


@pytest.mark.asyncio
async def test_install_key_with_sk_prefix_returns_invalid_credentials() -> None:
    """Secret keys (sk_) are also invalid — only pk_ is accepted."""
    c = KlaviyoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": "sk_live_123456789"},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ── health_check() with empty accounts ───────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_empty_accounts_list_still_healthy(connector: KlaviyoConnector) -> None:
    """An accounts response with an empty data list is still HEALTHY (API key valid)."""
    connector._make_client = lambda: MagicMock(
        get_accounts=AsyncMock(return_value={"data": []}),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Connected to Klaviyo" in result.message


@pytest.mark.asyncio
async def test_health_check_generic_exception_degraded(connector: KlaviyoConnector) -> None:
    """An unexpected exception sets health to DEGRADED (not OFFLINE)."""
    connector._make_client = lambda: MagicMock(
        get_accounts=AsyncMock(side_effect=RuntimeError("unexpected")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ── sync() PARTIAL result ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_campaigns_fail_profiles_lists_succeed_partial(connector: KlaviyoConnector) -> None:
    """Profiles + lists succeed; campaigns raise — status is COMPLETED (campaigns non-fatal)."""
    connector.http_client.list_profiles = AsyncMock(return_value=SAMPLE_PROFILE_LIST_RESPONSE)
    connector.http_client.list_campaigns = AsyncMock(
        side_effect=KlaviyoError("campaigns unavailable", 503)
    )
    connector.http_client.list_lists = AsyncMock(return_value=SAMPLE_LISTS_RESPONSE)
    result = await connector.sync(full=True)
    # Profiles (1) + lists (1) synced; campaigns skipped as non-fatal
    assert result.documents_synced >= 2
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_all_resources_fail_individually_returns_failed_count(
    connector: KlaviyoConnector,
) -> None:
    """When normalize_profile throws for every item, documents_failed accumulates."""

    def bad_profile(*_args: object, **_kwargs: object) -> None:
        raise ValueError("bad data")

    connector.http_client.list_profiles = AsyncMock(return_value=SAMPLE_PROFILE_LIST_RESPONSE)
    connector.http_client.list_campaigns = AsyncMock(return_value={"data": [], "links": {"next": None}})
    connector.http_client.list_lists = AsyncMock(return_value={"data": [], "links": {"next": None}})

    with patch("connector.normalize_profile", side_effect=bad_profile):
        result = await connector.sync(full=True)

    assert result.documents_found >= 1
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


# ── HTTP client _raise_for_status equivalents ────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_401_raises_auth_error() -> None:
    """A 401 response from the HTTP layer raises KlaviyoAuthError."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.headers = {}
    mock_response.text = "Unauthorized"
    mock_response.content = b"Unauthorized"
    mock_response.json.return_value = {"errors": [{"detail": "Invalid API key", "code": "not_authenticated"}]}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(KlaviyoAuthError):
            await client.get_accounts()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_403_raises_auth_error() -> None:
    """A 403 response raises KlaviyoAuthError (Forbidden)."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.headers = {}
    mock_response.text = "Forbidden"
    mock_response.content = b"Forbidden"
    mock_response.json.return_value = {"errors": [{"detail": "Forbidden", "code": "forbidden"}]}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(KlaviyoAuthError):
            await client.get_accounts()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_404_raises_not_found_error() -> None:
    """A 404 response raises KlaviyoNotFoundError."""
    from client.http_client import KlaviyoHTTPClient
    from exceptions import KlaviyoNotFoundError

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.headers = {}
    mock_response.text = "Not Found"
    mock_response.content = b"Not Found"
    mock_response.json.return_value = {}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(KlaviyoNotFoundError):
            await client.get_profile("nonexistent_id")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_429_raises_rate_limit_error() -> None:
    """A 429 response raises KlaviyoRateLimitError."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {"Retry-After": "10"}
    mock_response.text = "Too Many Requests"
    mock_response.content = b"Too Many Requests"
    mock_response.json.return_value = {"errors": [{"detail": "Rate limited"}]}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(KlaviyoRateLimitError) as exc_info:
            await client.list_profiles()
    assert exc_info.value.retry_after == 10.0
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_500_raises_server_error() -> None:
    """A 5xx response raises KlaviyoServerError."""
    from client.http_client import KlaviyoHTTPClient
    from exceptions import KlaviyoServerError

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.headers = {}
    mock_response.text = "Internal Server Error"
    mock_response.content = b"Internal Server Error"
    mock_response.json.return_value = {"errors": [{"detail": "Internal error"}]}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(KlaviyoServerError):
            await client.list_campaigns()
    await client.aclose()


# ── Auth header verification (Klaviyo-API-Key, NOT Bearer) ───────────────────


def test_http_client_auth_header_is_klaviyo_api_key() -> None:
    """The HTTP client must use 'Klaviyo-API-Key' auth header — NOT 'Bearer'."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    headers = dict(client._client.headers)
    auth_header = headers.get("authorization", "")
    assert auth_header.startswith("Klaviyo-API-Key "), (
        f"Expected 'Klaviyo-API-Key ...' but got '{auth_header}'"
    )
    assert "Bearer" not in auth_header
    assert VALID_API_KEY in auth_header


def test_http_client_auth_header_not_bearer() -> None:
    """Ensure Bearer token pattern is absent from auth header."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key="pk_testkey123")
    headers = dict(client._client.headers)
    auth_header = headers.get("authorization", "")
    assert "Bearer" not in auth_header
    assert "Klaviyo-API-Key pk_testkey123" == auth_header


# ── Revision header verification ─────────────────────────────────────────────


def test_http_client_revision_header_present() -> None:
    """Every request must carry 'revision: 2024-02-15' as required by Klaviyo."""
    from client.http_client import KlaviyoHTTPClient, KLAVIYO_REVISION

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    headers = dict(client._client.headers)
    assert "revision" in headers, "revision header must be set on the httpx client"
    assert headers["revision"] == KLAVIYO_REVISION


def test_http_client_revision_header_value() -> None:
    """Revision must be exactly '2024-02-15'."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    headers = dict(client._client.headers)
    assert headers.get("revision") == "2024-02-15"


# ── list_flows() ──────────────────────────────────────────────────────────────

SAMPLE_FLOWS_RESPONSE: dict = {
    "data": [
        {
            "type": "flow",
            "id": "flow_001",
            "attributes": {
                "name": "Welcome Series",
                "status": "live",
                "created": "2024-01-10T00:00:00+00:00",
                "updated": "2024-05-01T00:00:00+00:00",
            },
        }
    ],
    "links": {"next": None},
}


@pytest.mark.asyncio
async def test_list_flows_returns_data(connector: KlaviyoConnector) -> None:
    """list_flows returns a JSON:API response with a data list."""
    connector.http_client.list_flows = AsyncMock(return_value=SAMPLE_FLOWS_RESPONSE)
    result = await connector.list_flows()
    assert "data" in result
    assert result["data"][0]["id"] == "flow_001"
    connector.http_client.list_flows.assert_called_once_with(page_cursor=None)


@pytest.mark.asyncio
async def test_list_flows_with_cursor(connector: KlaviyoConnector) -> None:
    """list_flows passes the cursor through to the HTTP client."""
    connector.http_client.list_flows = AsyncMock(return_value=SAMPLE_FLOWS_RESPONSE)
    await connector.list_flows(page_cursor="cursor_flow_page2")
    connector.http_client.list_flows.assert_called_once_with(page_cursor="cursor_flow_page2")


@pytest.mark.asyncio
async def test_http_client_list_flows_no_cursor() -> None:
    """KlaviyoHTTPClient.list_flows sends GET /flows without cursor when None."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"data":[],"links":{"next":null}}'
    mock_response.json.return_value = {"data": [], "links": {"next": None}}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)) as mock_req:
        await client.list_flows()
        call_kwargs = mock_req.call_args
        params = call_kwargs.kwargs.get("params") or (call_kwargs.args[2] if len(call_kwargs.args) > 2 else {})
        assert "page[cursor]" not in params

    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_list_flows_with_cursor() -> None:
    """KlaviyoHTTPClient.list_flows includes page[cursor] when cursor provided."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"data":[]}'
    mock_response.json.return_value = {"data": []}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)) as mock_req:
        await client.list_flows(page_cursor="abc123")
        call_kwargs = mock_req.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert params.get("page[cursor]") == "abc123"

    await client.aclose()


# ── list_metrics() ────────────────────────────────────────────────────────────

SAMPLE_METRICS_RESPONSE: dict = {
    "data": [
        {
            "type": "metric",
            "id": "metric_001",
            "attributes": {
                "name": "Placed Order",
                "created": "2024-01-05T00:00:00+00:00",
                "updated": "2024-05-01T00:00:00+00:00",
                "integration": {"name": "Shopify", "category": "Commerce"},
            },
        }
    ],
    "links": {"next": None},
}


@pytest.mark.asyncio
async def test_list_metrics_returns_data(connector: KlaviyoConnector) -> None:
    """list_metrics returns a JSON:API response with a data list."""
    connector.http_client.list_metrics = AsyncMock(return_value=SAMPLE_METRICS_RESPONSE)
    result = await connector.list_metrics()
    assert "data" in result
    assert result["data"][0]["id"] == "metric_001"
    connector.http_client.list_metrics.assert_called_once_with(page_cursor=None)


@pytest.mark.asyncio
async def test_list_metrics_with_cursor(connector: KlaviyoConnector) -> None:
    """list_metrics passes the cursor through to the HTTP client."""
    connector.http_client.list_metrics = AsyncMock(return_value=SAMPLE_METRICS_RESPONSE)
    await connector.list_metrics(page_cursor="cursor_metrics_page2")
    connector.http_client.list_metrics.assert_called_once_with(page_cursor="cursor_metrics_page2")


@pytest.mark.asyncio
async def test_http_client_list_metrics_no_cursor() -> None:
    """KlaviyoHTTPClient.list_metrics sends GET /metrics without cursor param when None."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"data":[]}'
    mock_response.json.return_value = {"data": []}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)) as mock_req:
        await client.list_metrics()
        call_kwargs = mock_req.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert "page[cursor]" not in params

    await client.aclose()


# ── get_account() alias ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_get_account_alias() -> None:
    """get_account() is an alias for get_accounts() and returns the same response."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"data":[{"id":"acct_001"}]}'
    mock_response.json.return_value = {"data": [{"id": "acct_001"}]}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        result = await client.get_account()
    assert result["data"][0]["id"] == "acct_001"
    await client.aclose()


# ── links.next cursor pagination ─────────────────────────────────────────────


def test_extract_cursor_from_links_next_url() -> None:
    """Cursor extracted from a realistic Klaviyo links.next URL."""
    links_next = "https://a.klaviyo.com/api/profiles?page[size]=100&page[cursor]=WzE2MjQ4ODYyMDBd"
    cursor = _extract_cursor(links_next)
    assert cursor == "WzE2MjQ4ODYyMDBd"


def test_extract_cursor_none_links_next() -> None:
    """links.next of None must be guarded before calling _extract_cursor."""
    # In connector.py, sync() checks `if not next_cursor` before calling _extract_cursor
    # Confirm _extract_cursor("") returns "" so guard is meaningful
    assert _extract_cursor("") == ""


@pytest.mark.asyncio
async def test_sync_cursor_pagination_follows_links_next(connector: KlaviyoConnector) -> None:
    """sync() follows links.next across pages by extracting the cursor."""
    page1 = {
        "data": [SAMPLE_PROFILE_RESOURCE],
        "links": {"next": "https://a.klaviyo.com/api/profiles?page[cursor]=page2cursor"},
    }
    page2 = {
        "data": [
            {
                "type": "profile",
                "id": "profile_page2",
                "attributes": {"email": "page2@example.com", "first_name": "Page2"},
            }
        ],
        "links": {"next": None},
    }
    connector.http_client.list_profiles = AsyncMock(side_effect=[page1, page2])
    connector.http_client.list_campaigns = AsyncMock(return_value={"data": [], "links": {"next": None}})
    connector.http_client.list_lists = AsyncMock(return_value={"data": [], "links": {"next": None}})

    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2
    # Verify second call used the extracted cursor
    second_call_kwargs = connector.http_client.list_profiles.call_args_list[1]
    assert second_call_kwargs.kwargs.get("cursor") == "page2cursor"


# ── 4xx other error mapping ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_other_4xx_raises_klaviyo_error() -> None:
    """An unrecognised 4xx (e.g. 422) raises the base KlaviyoError."""
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.headers = {}
    mock_response.text = "Unprocessable Entity"
    mock_response.content = b"Unprocessable Entity"
    mock_response.json.return_value = {"errors": [{"detail": "Invalid filter"}]}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(KlaviyoError) as exc_info:
            await client.list_profiles()
    assert exc_info.value.status_code == 422
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_network_error_raises_network_exception() -> None:
    """A network-level error (TimeoutException) raises KlaviyoNetworkError."""
    import httpx
    from client.http_client import KlaviyoHTTPClient

    client = KlaviyoHTTPClient(api_key=VALID_API_KEY)
    with patch.object(
        client._client,
        "request",
        new=AsyncMock(side_effect=httpx.TimeoutException("timed out")),
    ):
        with pytest.raises(KlaviyoNetworkError):
            await client.get_accounts()
    await client.aclose()
