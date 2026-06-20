"""Unit tests for CustomerIOConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CustomerIOConnector, _normalize_newsletter
from exceptions import (
    CustomerIOAuthError,
    CustomerIOError,
    CustomerIONetworkError,
    CustomerIORateLimitError,
)
from helpers.utils import (
    CircuitBreaker,
    _stable_id,
    _stable_id_plain,
    normalize_campaign,
    normalize_customer,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_customerio_test_001"
VALID_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test_app_api_key"

# ── Sample data ─────────────────────────────────────────────────────────────

SAMPLE_ACCOUNT_RESPONSE: dict = {
    "account": {
        "name": "Acme Corp",
        "id": 12345,
        "domain": "acmecorp.com",
    }
}

SAMPLE_CUSTOMER: dict = {
    "id": "cust_abc123",
    "email": "jane@example.com",
    "created_at": 1704067200,
    "attributes": {
        "first_name": "Jane",
        "last_name": "Doe",
        "plan": "pro",
    },
}

SAMPLE_CUSTOMER_SEARCH_RESPONSE: dict = {
    "customers": [SAMPLE_CUSTOMER],
    "next": None,
}

SAMPLE_CUSTOMER_RESPONSE: dict = {
    "customer": SAMPLE_CUSTOMER,
}

SAMPLE_CAMPAIGN: dict = {
    "id": 101,
    "name": "Onboarding Series",
    "active": True,
    "msg_type": "email",
    "tags": ["onboarding", "automated"],
    "created": 1704067200,
    "updated": 1715000000,
}

SAMPLE_CAMPAIGNS_RESPONSE: dict = {
    "campaigns": [SAMPLE_CAMPAIGN],
}

SAMPLE_NEWSLETTER: dict = {
    "id": 201,
    "name": "Monthly Digest",
    "deduplicate_id": "monthly-digest-2024",
    "tags": ["newsletter"],
    "created": 1704067200,
    "updated": 1715000000,
}

SAMPLE_NEWSLETTERS_RESPONSE: dict = {
    "newsletters": [SAMPLE_NEWSLETTER],
}

SAMPLE_SEGMENTS_RESPONSE: dict = {
    "segments": [
        {
            "id": 301,
            "name": "Active Users",
            "description": "Users active in the last 30 days",
        }
    ],
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> CustomerIOConnector:
    c = CustomerIOConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"app_api_key": VALID_API_KEY},
    )
    c.http_client = MagicMock()
    return c


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = CustomerIOConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"app_api_key": VALID_API_KEY},
    )
    with patch("connector.CustomerIOHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=SAMPLE_ACCOUNT_RESPONSE)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Customer.io" in result.message
    assert "Acme Corp" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = CustomerIOConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "app_api_key is required" in result.message


@pytest.mark.asyncio
async def test_install_auth_error() -> None:
    c = CustomerIOConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"app_api_key": VALID_API_KEY},
    )
    with patch("connector.CustomerIOHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=CustomerIOAuthError("Unauthorized", 401))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_generic_exception() -> None:
    c = CustomerIOConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"app_api_key": VALID_API_KEY},
    )
    with patch("connector.CustomerIOHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=Exception("network failure"))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED
    assert "network failure" in result.message


@pytest.mark.asyncio
async def test_install_no_account_name() -> None:
    c = CustomerIOConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"app_api_key": VALID_API_KEY},
    )
    with patch("connector.CustomerIOHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value={"account": {}})
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Connected to Customer.io" in result.message


# ── health_check() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: CustomerIOConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_account=AsyncMock(return_value=SAMPLE_ACCOUNT_RESPONSE),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Acme Corp" in result.message


@pytest.mark.asyncio
async def test_health_check_missing_api_key() -> None:
    c = CustomerIOConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: CustomerIOConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_account=AsyncMock(side_effect=CustomerIOAuthError("Unauthorized", 401)),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: CustomerIOConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_account=AsyncMock(side_effect=CustomerIONetworkError("timeout")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_no_account_name(connector: CustomerIOConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_account=AsyncMock(return_value={"account": {}}),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert "Connected to Customer.io" in result.message


# ── sync() ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector: CustomerIOConnector) -> None:
    connector.http_client.search_customers = AsyncMock(
        return_value={"customers": [], "next": None}
    )
    connector.http_client.list_campaigns = AsyncMock(return_value={"campaigns": []})
    connector.http_client.list_newsletters = AsyncMock(return_value={"newsletters": []})
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_customers(connector: CustomerIOConnector) -> None:
    connector.http_client.search_customers = AsyncMock(
        return_value=SAMPLE_CUSTOMER_SEARCH_RESPONSE
    )
    connector.http_client.list_campaigns = AsyncMock(return_value={"campaigns": []})
    connector.http_client.list_newsletters = AsyncMock(return_value={"newsletters": []})
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_all_resources(connector: CustomerIOConnector) -> None:
    connector.http_client.search_customers = AsyncMock(
        return_value=SAMPLE_CUSTOMER_SEARCH_RESPONSE
    )
    connector.http_client.list_campaigns = AsyncMock(return_value=SAMPLE_CAMPAIGNS_RESPONSE)
    connector.http_client.list_newsletters = AsyncMock(return_value=SAMPLE_NEWSLETTERS_RESPONSE)
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3


@pytest.mark.asyncio
async def test_sync_customer_cursor_pagination(connector: CustomerIOConnector) -> None:
    page1 = {
        "customers": [SAMPLE_CUSTOMER],
        "next": "cursor_page2",
    }
    page2 = {
        "customers": [
            {
                "id": "cust_xyz456",
                "email": "john@example.com",
                "created_at": 1704067300,
                "attributes": {"first_name": "John", "last_name": "Smith"},
            }
        ],
        "next": None,
    }
    connector.http_client.search_customers = AsyncMock(side_effect=[page1, page2])
    connector.http_client.list_campaigns = AsyncMock(return_value={"campaigns": []})
    connector.http_client.list_newsletters = AsyncMock(return_value={"newsletters": []})
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert connector.http_client.search_customers.call_count == 2


@pytest.mark.asyncio
async def test_sync_customers_api_failure(connector: CustomerIOConnector) -> None:
    connector.http_client.search_customers = AsyncMock(
        side_effect=CustomerIOError("API error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_campaigns_failure_nonfatal(connector: CustomerIOConnector) -> None:
    connector.http_client.search_customers = AsyncMock(
        return_value=SAMPLE_CUSTOMER_SEARCH_RESPONSE
    )
    connector.http_client.list_campaigns = AsyncMock(
        side_effect=CustomerIOError("campaigns error", 500)
    )
    connector.http_client.list_newsletters = AsyncMock(return_value=SAMPLE_NEWSLETTERS_RESPONSE)
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found >= 1


@pytest.mark.asyncio
async def test_sync_with_kb_id(connector: CustomerIOConnector) -> None:
    connector.http_client.search_customers = AsyncMock(
        return_value=SAMPLE_CUSTOMER_SEARCH_RESPONSE
    )
    connector.http_client.list_campaigns = AsyncMock(return_value={"campaigns": []})
    connector.http_client.list_newsletters = AsyncMock(return_value={"newsletters": []})
    connector._ingest_document = AsyncMock()
    result = await connector.sync(full=True, kb_id="kb_test_001")
    assert result.documents_synced == 1
    connector._ingest_document.assert_called_once()


# ── list_customers() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_customers_no_cursor(connector: CustomerIOConnector) -> None:
    connector.http_client.search_customers = AsyncMock(
        return_value=SAMPLE_CUSTOMER_SEARCH_RESPONSE
    )
    result = await connector.list_customers(limit=50)
    assert "customers" in result
    assert len(result["customers"]) == 1
    connector.http_client.search_customers.assert_called_once_with(start=None, limit=50)


@pytest.mark.asyncio
async def test_list_customers_with_cursor(connector: CustomerIOConnector) -> None:
    connector.http_client.search_customers = AsyncMock(
        return_value=SAMPLE_CUSTOMER_SEARCH_RESPONSE
    )
    result = await connector.list_customers(start="cursor_abc123", limit=100)
    assert "customers" in result
    connector.http_client.search_customers.assert_called_once_with(
        start="cursor_abc123", limit=100
    )


# ── get_customer() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_customer(connector: CustomerIOConnector) -> None:
    connector.http_client.get_customer = AsyncMock(return_value=SAMPLE_CUSTOMER_RESPONSE)
    result = await connector.get_customer("cust_abc123")
    assert result["customer"]["id"] == "cust_abc123"
    connector.http_client.get_customer.assert_called_once_with("cust_abc123")


# ── list_campaigns() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_campaigns(connector: CustomerIOConnector) -> None:
    connector.http_client.list_campaigns = AsyncMock(return_value=SAMPLE_CAMPAIGNS_RESPONSE)
    result = await connector.list_campaigns(page=1, limit=50)
    assert "campaigns" in result
    assert result["campaigns"][0]["id"] == 101
    connector.http_client.list_campaigns.assert_called_once_with(page=1, limit=50)


# ── get_campaign() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_campaign(connector: CustomerIOConnector) -> None:
    single_campaign = {"campaign": SAMPLE_CAMPAIGN}
    connector.http_client.get_campaign = AsyncMock(return_value=single_campaign)
    result = await connector.get_campaign(101)
    assert result["campaign"]["id"] == 101
    connector.http_client.get_campaign.assert_called_once_with(101)


# ── list_newsletters() ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_newsletters(connector: CustomerIOConnector) -> None:
    connector.http_client.list_newsletters = AsyncMock(
        return_value=SAMPLE_NEWSLETTERS_RESPONSE
    )
    result = await connector.list_newsletters(page=1, limit=50)
    assert "newsletters" in result
    assert result["newsletters"][0]["id"] == 201
    connector.http_client.list_newsletters.assert_called_once_with(page=1, limit=50)


# ── list_segments() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_segments(connector: CustomerIOConnector) -> None:
    connector.http_client.list_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
    result = await connector.list_segments(page=1, limit=50)
    assert "segments" in result
    assert result["segments"][0]["id"] == 301
    connector.http_client.list_segments.assert_called_once_with(page=1, limit=50)


@pytest.mark.asyncio
async def test_list_segments_custom_page_size(connector: CustomerIOConnector) -> None:
    connector.http_client.list_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
    await connector.list_segments(page=2, limit=25)
    connector.http_client.list_segments.assert_called_once_with(page=2, limit=25)


# ── normalize_customer() ─────────────────────────────────────────────────────


def test_normalize_customer_full() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert "Jane Doe" in doc.title
    assert "jane@example.com" in doc.title
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["email"] == "jane@example.com"
    assert doc.metadata["first_name"] == "Jane"
    assert doc.metadata["last_name"] == "Doe"
    assert "cust_abc123" in doc.source_url
    # Stable ID: SHA-256("customer:cust_abc123")[:16]
    assert len(doc.source_id) == 16


def test_normalize_customer_wrapped_response() -> None:
    """Customer wrapped in a 'customer' key is unwrapped correctly."""
    doc = normalize_customer(SAMPLE_CUSTOMER_RESPONSE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == "jane@example.com"


def test_normalize_customer_no_email() -> None:
    resource = {
        "id": "cust_noemail",
        "created_at": 1704067200,
        "attributes": {"first_name": "Ghost"},
    }
    doc = normalize_customer(resource, CONNECTOR_ID, TENANT_ID)
    assert "Ghost" in doc.title
    assert "<" not in doc.title


def test_normalize_customer_no_name_no_email() -> None:
    resource = {"id": "cust_bare", "created_at": 0, "attributes": {}}
    doc = normalize_customer(resource, CONNECTOR_ID, TENANT_ID)
    assert "cust_bare" in doc.title
    assert len(doc.source_id) == 16


def test_normalize_customer_extra_attributes_included() -> None:
    """Up to 5 extra attributes are included in doc content."""
    resource = {
        "id": "cust_attrs",
        "email": "x@x.com",
        "created_at": 0,
        "attributes": {
            "plan": "pro",
            "country": "US",
            "company": "Acme",
        },
    }
    doc = normalize_customer(resource, CONNECTOR_ID, TENANT_ID)
    assert "plan" in doc.content or "pro" in doc.content


def test_normalize_customer_stable_id_uses_customer_prefix() -> None:
    """stable_id for customers is SHA-256('customer:' + id)[:16]."""
    import hashlib
    raw = "cust_abc123"
    expected = hashlib.sha256(f"customer:{raw}".encode()).hexdigest()[:16]
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


# ── normalize_campaign() ─────────────────────────────────────────────────────


def test_normalize_campaign_full() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert "Onboarding Series" in doc.title
    assert doc.metadata["status"] == "active"
    assert doc.metadata["campaign_id"] == 101
    assert "101" in doc.source_url
    assert len(doc.source_id) == 16


def test_normalize_campaign_inactive() -> None:
    campaign = dict(SAMPLE_CAMPAIGN)
    campaign["active"] = False
    doc = normalize_campaign(campaign, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["status"] == "inactive"


def test_normalize_campaign_minimal() -> None:
    minimal = {"id": 999, "name": "Minimal Campaign"}
    doc = normalize_campaign(minimal, CONNECTOR_ID, TENANT_ID)
    assert "Minimal Campaign" in doc.title
    assert doc.metadata["tags"] == []


def test_normalize_campaign_tags_in_content() -> None:
    doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
    assert "onboarding" in doc.content
    assert "automated" in doc.content


# ── _normalize_newsletter() ──────────────────────────────────────────────────


def test_normalize_newsletter_full() -> None:
    doc = _normalize_newsletter(SAMPLE_NEWSLETTER, CONNECTOR_ID, TENANT_ID)
    assert "Monthly Digest" in doc.title
    assert doc.metadata["newsletter_id"] == 201
    assert "201" in doc.source_url
    assert len(doc.source_id) == 16


def test_normalize_newsletter_tags_in_content() -> None:
    doc = _normalize_newsletter(SAMPLE_NEWSLETTER, CONNECTOR_ID, TENANT_ID)
    assert "newsletter" in doc.content


def test_normalize_newsletter_minimal() -> None:
    minimal = {"id": 999, "name": "Minimal Newsletter"}
    doc = _normalize_newsletter(minimal, CONNECTOR_ID, TENANT_ID)
    assert "Minimal Newsletter" in doc.title
    assert doc.metadata["deduplicate_id"] == ""
    assert doc.metadata["tags"] == []


# ── _stable_id() ─────────────────────────────────────────────────────────────


def test_stable_id_uses_customer_prefix() -> None:
    """_stable_id() prefixes with 'customer:' before hashing."""
    import hashlib
    raw = "some_id"
    expected = hashlib.sha256(f"customer:{raw}".encode()).hexdigest()[:16]
    assert _stable_id(raw) == expected


def test_stable_id_length() -> None:
    assert len(_stable_id("some_id")) == 16


def test_stable_id_deterministic() -> None:
    assert _stable_id("cust_abc123") == _stable_id("cust_abc123")


def test_stable_id_unique() -> None:
    assert _stable_id("cust_abc123") != _stable_id("cust_xyz456")


def test_stable_id_plain_length() -> None:
    assert len(_stable_id_plain("campaign_101")) == 16


def test_stable_id_plain_deterministic() -> None:
    assert _stable_id_plain("newsletter_201") == _stable_id_plain("newsletter_201")


def test_stable_id_hex_only() -> None:
    sid = _stable_id("arbitrary_input_value")
    assert all(c in "0123456789abcdef" for c in sid)


def test_stable_id_plain_differs_from_prefixed() -> None:
    """_stable_id_plain and _stable_id must differ for same raw ID."""
    raw = "same_id"
    assert _stable_id(raw) != _stable_id_plain(raw)


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
async def test_aclose_clears_client(connector: CustomerIOConnector) -> None:
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    connector.http_client = mock_client
    await connector.aclose()
    mock_client.aclose.assert_called_once()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_aclose_no_client() -> None:
    c = CustomerIOConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"app_api_key": VALID_API_KEY},
    )
    await c.aclose()  # Must not raise


# ── Context manager ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_manager() -> None:
    c = CustomerIOConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"app_api_key": VALID_API_KEY},
    )
    async with c as conn:
        assert conn is c
    assert c.http_client is None


# ── _ensure_client() ─────────────────────────────────────────────────────────


def test_ensure_client_creates_client() -> None:
    c = CustomerIOConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"app_api_key": VALID_API_KEY},
    )
    assert c.http_client is None
    client = c._ensure_client()
    assert client is not None
    assert c.http_client is client


def test_ensure_client_reuses_existing(connector: CustomerIOConnector) -> None:
    original = connector.http_client
    returned = connector._ensure_client()
    assert returned is original


# ── with_retry() behaviour ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_exhaustion_raises_last_error() -> None:
    call_count = 0

    async def flaky() -> dict:  # type: ignore[type-arg]
        nonlocal call_count
        call_count += 1
        raise CustomerIOError("transient", 503)

    from helpers.utils import with_retry

    with pytest.raises(CustomerIOError, match="transient"):
        await with_retry(flaky, max_attempts=3, base_delay=0)
    assert call_count == 3


@pytest.mark.asyncio
async def test_with_retry_auth_error_no_retry() -> None:
    call_count = 0

    async def always_auth_fail() -> dict:  # type: ignore[type-arg]
        nonlocal call_count
        call_count += 1
        raise CustomerIOAuthError("bad key", 401)

    from helpers.utils import with_retry

    with pytest.raises(CustomerIOAuthError):
        await with_retry(always_auth_fail, max_attempts=3, base_delay=0)
    assert call_count == 1


# ── HTTP client error mapping ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_401_raises_auth_error() -> None:
    from client.http_client import CustomerIOHTTPClient

    client = CustomerIOHTTPClient(app_api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.headers = {}
    mock_response.text = "Unauthorized"
    mock_response.content = b"Unauthorized"
    mock_response.json.return_value = {"meta": {"error": "Invalid App API key"}}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(CustomerIOAuthError):
            await client.get_account()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_403_raises_auth_error() -> None:
    from client.http_client import CustomerIOHTTPClient

    client = CustomerIOHTTPClient(app_api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.headers = {}
    mock_response.text = "Forbidden"
    mock_response.content = b"Forbidden"
    mock_response.json.return_value = {"meta": {"error": "Forbidden"}}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(CustomerIOAuthError):
            await client.get_account()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_404_raises_not_found() -> None:
    from client.http_client import CustomerIOHTTPClient
    from exceptions import CustomerIONotFoundError

    client = CustomerIOHTTPClient(app_api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.headers = {}
    mock_response.text = "Not Found"
    mock_response.content = b"Not Found"
    mock_response.json.return_value = {}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(CustomerIONotFoundError):
            await client.get_customer("nonexistent_id")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_429_raises_rate_limit_error() -> None:
    from client.http_client import CustomerIOHTTPClient

    client = CustomerIOHTTPClient(app_api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {"Retry-After": "10"}
    mock_response.text = "Too Many Requests"
    mock_response.content = b"Too Many Requests"
    mock_response.json.return_value = {"meta": {"error": "Rate limited"}}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(CustomerIORateLimitError) as exc_info:
            await client.search_customers()
    assert exc_info.value.retry_after == 10.0
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_500_raises_server_error() -> None:
    from client.http_client import CustomerIOHTTPClient
    from exceptions import CustomerIOServerError

    client = CustomerIOHTTPClient(app_api_key=VALID_API_KEY)
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.headers = {}
    mock_response.text = "Internal Server Error"
    mock_response.content = b"Internal Server Error"
    mock_response.json.return_value = {}

    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(CustomerIOServerError):
            await client.list_campaigns()
    await client.aclose()


# ── sync() PARTIAL and edge cases ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_newsletters_failure_nonfatal(connector: CustomerIOConnector) -> None:
    connector.http_client.search_customers = AsyncMock(
        return_value=SAMPLE_CUSTOMER_SEARCH_RESPONSE
    )
    connector.http_client.list_campaigns = AsyncMock(return_value=SAMPLE_CAMPAIGNS_RESPONSE)
    connector.http_client.list_newsletters = AsyncMock(
        side_effect=CustomerIOError("newsletters error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_synced >= 2


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(connector: CustomerIOConnector) -> None:
    def bad_customer(*_args: object, **_kwargs: object) -> None:
        raise ValueError("bad data")

    connector.http_client.search_customers = AsyncMock(
        return_value=SAMPLE_CUSTOMER_SEARCH_RESPONSE
    )
    connector.http_client.list_campaigns = AsyncMock(return_value={"campaigns": []})
    connector.http_client.list_newsletters = AsyncMock(return_value={"newsletters": []})

    with patch("connector.normalize_customer", side_effect=bad_customer):
        result = await connector.sync(full=True)

    assert result.documents_found >= 1
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_health_check_generic_exception_degraded(connector: CustomerIOConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_account=AsyncMock(side_effect=RuntimeError("unexpected")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_list_campaigns_custom_page(connector: CustomerIOConnector) -> None:
    connector.http_client.list_campaigns = AsyncMock(return_value=SAMPLE_CAMPAIGNS_RESPONSE)
    await connector.list_campaigns(page=3, limit=10)
    connector.http_client.list_campaigns.assert_called_once_with(page=3, limit=10)


@pytest.mark.asyncio
async def test_list_newsletters_custom_page(connector: CustomerIOConnector) -> None:
    connector.http_client.list_newsletters = AsyncMock(
        return_value=SAMPLE_NEWSLETTERS_RESPONSE
    )
    await connector.list_newsletters(page=2, limit=25)
    connector.http_client.list_newsletters.assert_called_once_with(page=2, limit=25)
