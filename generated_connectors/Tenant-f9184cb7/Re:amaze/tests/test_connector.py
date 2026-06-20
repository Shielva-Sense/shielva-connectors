"""Unit tests for ReamazeConnector — all HTTP calls are mocked (63 tests)."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import ReamazeConnector
from exceptions import (
    ReamazeAuthError,
    ReamazeError,
    ReamazeNetworkError,
    ReamazeNotFoundError,
    ReamazeRateLimitError,
)
from helpers.utils import normalize_article, normalize_contact, normalize_conversation, with_retry
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_reamaze_test_001"
BRAND_SUBDOMAIN = "mystore"
EMAIL = "admin@mystore.com"
API_TOKEN = "test_api_token_xyz789"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_CONVERSATION: dict = {
    "slug": "abc123",
    "subject": "Order not delivered",
    "status": "open",
    "channel": {"name": "email"},
    "tags": [{"name": "shipping"}, {"name": "urgent"}],
    "created_at": "2026-06-01T10:00:00Z",
    "updated_at": "2026-06-01T12:00:00Z",
    "messages": [
        {
            "body": "My order hasn't arrived yet.",
            "author": {"email": "customer@example.com", "name": "Customer A"},
        }
    ],
}

SAMPLE_CONVERSATION_2: dict = {
    "slug": "def456",
    "subject": "Wrong item received",
    "status": "closed",
    "channel": {"name": "chat"},
    "tags": [],
    "created_at": "2026-06-02T09:00:00Z",
    "updated_at": "2026-06-02T11:00:00Z",
    "messages": [],
}

SAMPLE_CONTACT: dict = {
    "id": 101,
    "name": "Alice Buyer",
    "email": "alice@example.com",
    "phone": "+1-555-1111",
    "data": {"company": "ACME Inc."},
    "created_at": "2026-05-01T08:00:00Z",
    "updated_at": "2026-05-15T08:00:00Z",
}

SAMPLE_CONTACT_2: dict = {
    "id": 102,
    "name": "Bob Shopper",
    "email": "bob@example.com",
    "phone": "",
    "data": {},
    "created_at": "2026-05-10T10:00:00Z",
    "updated_at": "2026-05-20T10:00:00Z",
}

SAMPLE_ARTICLE: dict = {
    "slug": "how-to-return",
    "title": "How to Return an Item",
    "body": "To return an item, follow these steps...",
    "status": "published",
    "created_at": "2026-04-01T08:00:00Z",
    "updated_at": "2026-05-01T08:00:00Z",
}

SAMPLE_ARTICLE_2: dict = {
    "slug": "shipping-policy",
    "title": "Shipping Policy",
    "body": "We ship within 2-3 business days.",
    "status": "published",
    "created_at": "2026-04-10T08:00:00Z",
    "updated_at": "2026-05-10T08:00:00Z",
}

SAMPLE_PEOPLE_PAGE_1: dict = {
    "contacts": [SAMPLE_CONTACT],
    "current_page": 1,
    "total_pages": 2,
}

SAMPLE_PEOPLE_PAGE_2: dict = {
    "contacts": [SAMPLE_CONTACT_2],
    "current_page": 2,
    "total_pages": 2,
}

SAMPLE_CONVERSATIONS_PAGE: dict = {
    "conversations": [SAMPLE_CONVERSATION, SAMPLE_CONVERSATION_2],
    "current_page": 1,
    "total_pages": 1,
}

SAMPLE_ARTICLES_PAGE: dict = {
    "articles": [SAMPLE_ARTICLE, SAMPLE_ARTICLE_2],
    "current_page": 1,
    "total_pages": 1,
}

SAMPLE_REPORT_SUMMARY: dict = {
    "total_conversations": 42,
    "resolved_conversations": 30,
    "average_response_time": 120,
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_connector(
    brand_subdomain: str = BRAND_SUBDOMAIN,
    email: str = EMAIL,
    api_token: str = API_TOKEN,
) -> ReamazeConnector:
    return ReamazeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "brand_subdomain": brand_subdomain,
            "email": email,
            "api_token": api_token,
        },
    )


@pytest.fixture()
def connector() -> ReamazeConnector:
    c = make_connector()
    c._http_client = MagicMock()
    return c


# ── Exception hierarchy (5 tests) ────────────────────────────────────────────


def test_reamaze_auth_error_is_reamaze_error() -> None:
    exc = ReamazeAuthError("bad credentials", 401)
    assert isinstance(exc, ReamazeError)
    assert exc.status_code == 401
    assert "bad credentials" in str(exc)


def test_reamaze_not_found_error_message() -> None:
    exc = ReamazeNotFoundError("conversation", "abc123")
    assert "abc123" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_reamaze_rate_limit_error_retry_after() -> None:
    exc = ReamazeRateLimitError("too many requests", retry_after=60.0)
    assert exc.retry_after == 60.0
    assert exc.status_code == 429
    assert exc.code == "rate_limit"


def test_reamaze_network_error_inherits_base() -> None:
    exc = ReamazeNetworkError("timeout")
    assert isinstance(exc, ReamazeError)
    assert exc.status_code == 0


def test_reamaze_error_base_fields() -> None:
    exc = ReamazeError("something broke", status_code=500, code="server_error")
    assert exc.message == "something broke"
    assert exc.status_code == 500
    assert exc.code == "server_error"


# ── Models (5 tests) ──────────────────────────────────────────────────────────


def test_connector_health_enum_values() -> None:
    from models import ConnectorHealth
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum_values() -> None:
    from models import AuthStatus
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_enum_values() -> None:
    from models import SyncStatus
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"


def test_install_result_defaults() -> None:
    from models import InstallResult
    r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
    assert r.connector_id == ""
    assert r.message == ""


def test_connector_document_metadata_default() -> None:
    from models import ConnectorDocument
    doc = ConnectorDocument(
        source_id="abc",
        title="Test",
        content="content",
        connector_id="c1",
        tenant_id="t1",
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ── normalize_conversation (6 tests) ─────────────────────────────────────────


def test_normalize_conversation_basic() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    assert "Order not delivered" in doc.title
    assert doc.metadata["slug"] == "abc123"
    assert doc.metadata["status"] == "open"
    assert doc.metadata["channel"] == "email"
    assert "shipping" in doc.metadata["tags"]
    assert "urgent" in doc.metadata["tags"]


def test_normalize_conversation_source_id_is_sha256_prefix() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    expected = hashlib.sha256("conversation:abc123".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_conversation_messages_in_content() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    assert "My order hasn't arrived yet." in doc.content
    assert "customer@example.com" in doc.content


def test_normalize_conversation_no_messages() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION_2)
    assert "Wrong item received" in doc.content
    assert doc.metadata["status"] == "closed"


def test_normalize_conversation_empty_tags() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION_2)
    assert doc.metadata["tags"] == []


def test_normalize_conversation_subject_fallback() -> None:
    raw = {**SAMPLE_CONVERSATION, "subject": ""}
    doc = normalize_conversation(raw)
    assert "abc123" in doc.title or "Conversation" in doc.title


# ── normalize_contact (6 tests) ───────────────────────────────────────────────


def test_normalize_contact_basic() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert doc.title == "Contact: Alice Buyer"
    assert doc.metadata["email"] == "alice@example.com"
    assert doc.metadata["phone"] == "+1-555-1111"
    assert doc.metadata["contact_id"] == 101


def test_normalize_contact_source_id_is_sha256_prefix() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    expected = hashlib.sha256("contact:101".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_contact_content_includes_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert "alice@example.com" in doc.content


def test_normalize_contact_data_fields_in_content() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert "ACME Inc." in doc.content


def test_normalize_contact_no_phone() -> None:
    doc = normalize_contact(SAMPLE_CONTACT_2)
    assert doc.metadata["phone"] == ""
    assert "bob@example.com" in doc.content


def test_normalize_contact_name_fallback() -> None:
    raw = {**SAMPLE_CONTACT, "name": ""}
    doc = normalize_contact(raw)
    assert "101" in doc.title


# ── normalize_article (6 tests) ───────────────────────────────────────────────


def test_normalize_article_basic() -> None:
    doc = normalize_article(SAMPLE_ARTICLE)
    assert doc.title == "How to Return an Item"
    assert doc.metadata["slug"] == "how-to-return"
    assert doc.metadata["status"] == "published"


def test_normalize_article_source_id_is_sha256_prefix() -> None:
    doc = normalize_article(SAMPLE_ARTICLE)
    expected = hashlib.sha256("article:how-to-return".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_article_body_in_content() -> None:
    doc = normalize_article(SAMPLE_ARTICLE)
    assert "return an item" in doc.content.lower()


def test_normalize_article_created_at_in_metadata() -> None:
    doc = normalize_article(SAMPLE_ARTICLE)
    assert doc.metadata["created_at"] == "2026-04-01T08:00:00Z"


def test_normalize_article_updated_at_in_metadata() -> None:
    doc = normalize_article(SAMPLE_ARTICLE)
    assert doc.metadata["updated_at"] == "2026-05-01T08:00:00Z"


def test_normalize_article_title_fallback() -> None:
    raw = {**SAMPLE_ARTICLE, "title": "", "slug": "my-slug"}
    doc = normalize_article(raw)
    assert "my-slug" in doc.title or doc.title != ""


# ── with_retry (6 tests) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    fn = AsyncMock(side_effect=[ReamazeNetworkError("transient"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_auth_immediately() -> None:
    fn = AsyncMock(side_effect=ReamazeAuthError("Unauthorized", 401))
    with pytest.raises(ReamazeAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=ReamazeNetworkError("always fails"))
    with pytest.raises(ReamazeNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retried() -> None:
    fn = AsyncMock(
        side_effect=[
            ReamazeRateLimitError("rate limited", retry_after=0),
            {"ok": True},
        ]
    )
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    fn = AsyncMock(return_value=42)
    result = await with_retry(fn, "arg1", key="val", max_attempts=1)
    fn.assert_called_once_with("arg1", key="val")
    assert result == 42


# ── HTTP client mocked (14 tests) ─────────────────────────────────────────────


def test_http_client_uses_basic_auth() -> None:
    import aiohttp
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    auth = client._auth()
    assert isinstance(auth, aiohttp.BasicAuth)
    assert auth.login == EMAIL
    assert auth.password == API_TOKEN


def test_http_client_base_url_uses_subdomain() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": "testbrand",
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    assert client._base_url() == "https://testbrand.reamaze.com/api/v1"


@pytest.mark.asyncio
async def test_http_client_get_conversations_pagination() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    client._request = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
    result = await client.get_conversations(page=1)
    assert result["conversations"] == SAMPLE_CONVERSATIONS_PAGE["conversations"]
    assert result["total_pages"] == 1
    client._request.assert_called_once_with("GET", "/conversations", params={"page": 1})


@pytest.mark.asyncio
async def test_http_client_get_conversation_single() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    client._request = AsyncMock(return_value=SAMPLE_CONVERSATION)
    result = await client.get_conversation("abc123")
    assert result["slug"] == "abc123"
    client._request.assert_called_once_with("GET", "/conversations/abc123")


@pytest.mark.asyncio
async def test_http_client_get_people() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    client._request = AsyncMock(return_value=SAMPLE_PEOPLE_PAGE_1)
    result = await client.get_people(page=1)
    assert len(result["contacts"]) == 1
    assert result["total_pages"] == 2


@pytest.mark.asyncio
async def test_http_client_get_person() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    client._request = AsyncMock(return_value=SAMPLE_CONTACT)
    result = await client.get_person(101)
    assert result["id"] == 101
    client._request.assert_called_once_with("GET", "/people/101")


@pytest.mark.asyncio
async def test_http_client_get_articles() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    client._request = AsyncMock(return_value=SAMPLE_ARTICLES_PAGE)
    result = await client.get_articles(page=1)
    assert len(result["articles"]) == 2
    assert result["articles"][0]["slug"] == "how-to-return"


@pytest.mark.asyncio
async def test_http_client_get_report_summary() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    client._request = AsyncMock(return_value=SAMPLE_REPORT_SUMMARY)
    result = await client.get_report_summary()
    assert result["total_conversations"] == 42
    client._request.assert_called_once_with("GET", "/reports/summary")


def test_http_client_raise_for_status_401() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    with pytest.raises(ReamazeAuthError):
        client._raise_for_status(401, {"error": "Unauthorized"})


def test_http_client_raise_for_status_403() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    with pytest.raises(ReamazeAuthError):
        client._raise_for_status(403, {})


def test_http_client_raise_for_status_404() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    with pytest.raises(ReamazeNotFoundError):
        client._raise_for_status(404, {})


def test_http_client_raise_for_status_429() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    with pytest.raises(ReamazeRateLimitError):
        client._raise_for_status(429, {})


def test_http_client_raise_for_status_500() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    with pytest.raises(ReamazeNetworkError):
        client._raise_for_status(503, {"error": "Service Unavailable"})


@pytest.mark.asyncio
async def test_http_client_total_pages_pagination() -> None:
    from client.http_client import ReamazeHTTPClient
    client = ReamazeHTTPClient(config={
        "brand_subdomain": BRAND_SUBDOMAIN,
        "email": EMAIL,
        "api_token": API_TOKEN,
    })
    page1 = {**SAMPLE_PEOPLE_PAGE_1, "total_pages": 2}
    page2 = {**SAMPLE_PEOPLE_PAGE_2, "total_pages": 2}
    client._request = AsyncMock(side_effect=[page1, page2])
    r1 = await client.get_people(page=1)
    r2 = await client.get_people(page=2)
    assert r1["current_page"] == 1
    assert r2["current_page"] == 2
    assert r1["total_pages"] == 2


# ── install() (5 tests) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.get_people = AsyncMock(return_value=SAMPLE_PEOPLE_PAGE_1)
    mock_client.aclose = AsyncMock()
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "mystore.reamaze.com" in result.message


@pytest.mark.asyncio
async def test_install_missing_brand_subdomain() -> None:
    c = make_connector(brand_subdomain="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "brand_subdomain" in result.message


@pytest.mark.asyncio
async def test_install_missing_email() -> None:
    c = make_connector(email="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "email" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_token() -> None:
    c = make_connector(api_token="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_token" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.get_people = AsyncMock(
        side_effect=ReamazeAuthError("Invalid credentials", 401)
    )
    mock_client.aclose = AsyncMock()
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.get_people = AsyncMock(
        side_effect=ReamazeNetworkError("Connection refused")
    )
    mock_client.aclose = AsyncMock()
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ── health_check() (5 tests) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: ReamazeConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_people=AsyncMock(return_value=SAMPLE_PEOPLE_PAGE_1),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_credentials(connector: ReamazeConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_people=AsyncMock(side_effect=ReamazeAuthError("bad credentials", 401)),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: ReamazeConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_people=AsyncMock(side_effect=ReamazeNetworkError("timeout")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: ReamazeConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_people=AsyncMock(side_effect=Exception("unexpected")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_missing_creds() -> None:
    c = make_connector(brand_subdomain="", email="", api_token="")
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── sync() (8 tests) ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value={"conversations": [], "current_page": 1, "total_pages": 1}
    )
    connector._http_client.get_people = AsyncMock(
        return_value={"contacts": [], "current_page": 1, "total_pages": 1}
    )
    connector._http_client.get_articles = AsyncMock(
        return_value={"articles": [], "current_page": 1, "total_pages": 1}
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_conversations_one_page(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value=SAMPLE_CONVERSATIONS_PAGE
    )
    connector._http_client.get_people = AsyncMock(
        return_value={"contacts": [], "current_page": 1, "total_pages": 1}
    )
    connector._http_client.get_articles = AsyncMock(
        return_value={"articles": [], "current_page": 1, "total_pages": 1}
    )
    result = await connector.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_contacts_multiple_pages(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value={"conversations": [], "current_page": 1, "total_pages": 1}
    )
    connector._http_client.get_people = AsyncMock(
        side_effect=[SAMPLE_PEOPLE_PAGE_1, SAMPLE_PEOPLE_PAGE_2]
    )
    connector._http_client.get_articles = AsyncMock(
        return_value={"articles": [], "current_page": 1, "total_pages": 1}
    )
    result = await connector.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_articles_one_page(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value={"conversations": [], "current_page": 1, "total_pages": 1}
    )
    connector._http_client.get_people = AsyncMock(
        return_value={"contacts": [], "current_page": 1, "total_pages": 1}
    )
    connector._http_client.get_articles = AsyncMock(
        return_value=SAMPLE_ARTICLES_PAGE
    )
    result = await connector.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_conversation_api_failure(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        side_effect=ReamazeNetworkError("Server error", 500)
    )
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_contact_api_failure(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value={"conversations": [], "current_page": 1, "total_pages": 1}
    )
    connector._http_client.get_people = AsyncMock(
        side_effect=ReamazeNetworkError("Server error", 500)
    )
    result = await connector.sync()
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_article_api_failure(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value={"conversations": [], "current_page": 1, "total_pages": 1}
    )
    connector._http_client.get_people = AsyncMock(
        return_value={"contacts": [], "current_page": 1, "total_pages": 1}
    )
    connector._http_client.get_articles = AsyncMock(
        side_effect=ReamazeNetworkError("Server error", 500)
    )
    result = await connector.sync()
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_ingest_called_with_kb_id(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value=SAMPLE_CONVERSATIONS_PAGE
    )
    connector._http_client.get_people = AsyncMock(
        return_value={"contacts": [SAMPLE_CONTACT], "current_page": 1, "total_pages": 1}
    )
    connector._http_client.get_articles = AsyncMock(
        return_value={"articles": [SAMPLE_ARTICLE], "current_page": 1, "total_pages": 1}
    )
    connector._ingest_document = AsyncMock()
    result = await connector.sync(kb_id="kb_test_001")
    assert connector._ingest_document.call_count == 4
    assert result.documents_synced == 4


# ── list methods (5 tests) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_conversations_returns_page(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value=SAMPLE_CONVERSATIONS_PAGE
    )
    result = await connector.list_conversations(page=1)
    assert len(result) == 2
    assert result[0]["slug"] == "abc123"


@pytest.mark.asyncio
async def test_list_contacts_returns_page(connector: ReamazeConnector) -> None:
    connector._http_client.get_people = AsyncMock(return_value=SAMPLE_PEOPLE_PAGE_1)
    result = await connector.list_contacts(page=1)
    assert len(result) == 1
    assert result[0]["id"] == 101


@pytest.mark.asyncio
async def test_list_articles_returns_page(connector: ReamazeConnector) -> None:
    connector._http_client.get_articles = AsyncMock(return_value=SAMPLE_ARTICLES_PAGE)
    result = await connector.list_articles(page=1)
    assert len(result) == 2
    assert result[0]["slug"] == "how-to-return"


@pytest.mark.asyncio
async def test_list_conversations_empty(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value={"conversations": [], "current_page": 1, "total_pages": 1}
    )
    result = await connector.list_conversations(page=5)
    assert result == []


@pytest.mark.asyncio
async def test_list_contacts_empty(connector: ReamazeConnector) -> None:
    connector._http_client.get_people = AsyncMock(
        return_value={"contacts": [], "current_page": 1, "total_pages": 1}
    )
    result = await connector.list_contacts(page=3)
    assert result == []


# ── get_conversation / get_contact (4 tests) ──────────────────────────────────


@pytest.mark.asyncio
async def test_get_conversation_success(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversation = AsyncMock(return_value=SAMPLE_CONVERSATION)
    result = await connector.get_conversation("abc123")
    assert result["slug"] == "abc123"
    assert result["subject"] == "Order not delivered"


@pytest.mark.asyncio
async def test_get_conversation_not_found(connector: ReamazeConnector) -> None:
    connector._http_client.get_conversation = AsyncMock(
        side_effect=ReamazeNotFoundError("conversation", "xyz999")
    )
    with pytest.raises(ReamazeNotFoundError):
        await connector.get_conversation("xyz999")


@pytest.mark.asyncio
async def test_get_contact_success(connector: ReamazeConnector) -> None:
    connector._http_client.get_person = AsyncMock(return_value=SAMPLE_CONTACT)
    result = await connector.get_contact(101)
    assert result["id"] == 101
    assert result["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_get_contact_not_found(connector: ReamazeConnector) -> None:
    connector._http_client.get_person = AsyncMock(
        side_effect=ReamazeNotFoundError("contact", "9999")
    )
    with pytest.raises(ReamazeNotFoundError):
        await connector.get_contact(9999)


# ── get_report_summary (3 tests) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_report_summary_success(connector: ReamazeConnector) -> None:
    connector._http_client.get_report_summary = AsyncMock(return_value=SAMPLE_REPORT_SUMMARY)
    result = await connector.get_report_summary()
    assert result["total_conversations"] == 42
    assert result["resolved_conversations"] == 30


@pytest.mark.asyncio
async def test_get_report_summary_network_error(connector: ReamazeConnector) -> None:
    connector._http_client.get_report_summary = AsyncMock(
        side_effect=ReamazeNetworkError("timeout")
    )
    with pytest.raises(ReamazeNetworkError):
        await connector.get_report_summary()


@pytest.mark.asyncio
async def test_get_report_summary_empty(connector: ReamazeConnector) -> None:
    connector._http_client.get_report_summary = AsyncMock(return_value={})
    result = await connector.get_report_summary()
    assert isinstance(result, dict)
