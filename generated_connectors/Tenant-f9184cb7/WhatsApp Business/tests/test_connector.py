"""Unit tests for WhatsAppConnector — all Meta HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import WhatsAppConnector
from exceptions import (
    WhatsAppAuthError,
    WhatsAppError,
    WhatsAppNetworkError,
    WhatsAppNotFoundError,
    WhatsAppRateLimitError,
)
from helpers.utils import normalize_template, with_retry
from models import AuthStatus, ConnectorHealth, ConnectorDocument, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_whatsapp_test_001"
VALID_TOKEN = "EAAtest1234567890abcdefghijklmnopqrstuvwxyz"
PHONE_NUMBER_ID = "1234567890"
WABA_ID = "9876543210"

# ── Shared fixtures & sample data ─────────────────────────────────────────────

VALID_CONFIG = {
    "phone_number_id": PHONE_NUMBER_ID,
    "access_token": VALID_TOKEN,
    "waba_id": WABA_ID,
}

SAMPLE_PHONE_NUMBER: dict = {
    "id": PHONE_NUMBER_ID,
    "display_phone_number": "+1 555-000-1234",
    "verified_name": "Acme Corp",
    "quality_rating": "GREEN",
    "status": "CONNECTED",
}

SAMPLE_TEMPLATE: dict = {
    "id": "tpl_001",
    "name": "order_confirmation",
    "status": "APPROVED",
    "category": "TRANSACTIONAL",
    "language": "en_US",
    "components": [
        {"type": "HEADER", "format": "TEXT", "text": "Order Confirmed"},
        {"type": "BODY", "text": "Hi {{1}}, your order {{2}} has been placed."},
        {"type": "FOOTER", "text": "Thank you for shopping with us."},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Track Order"}]},
    ],
}

SAMPLE_TEMPLATE_2: dict = {
    "id": "tpl_002",
    "name": "shipping_update",
    "status": "APPROVED",
    "category": "TRANSACTIONAL",
    "language": "en_US",
    "components": [
        {"type": "BODY", "text": "Your order has shipped!"},
    ],
}

SAMPLE_WABA: dict = {
    "id": WABA_ID,
    "name": "Acme Corp WABA",
    "currency": "USD",
    "timezone_id": "1",
    "message_template_namespace": "acme_ns_001",
}


@pytest.fixture()
def connector() -> WhatsAppConnector:
    c = WhatsAppConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=VALID_CONFIG,
    )
    c.http_client = MagicMock()
    return c


@pytest.fixture()
def no_creds_connector() -> WhatsAppConnector:
    return WhatsAppConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    conn = WhatsAppConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=VALID_CONFIG,
    )
    with patch("connector.WhatsAppHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_phone_number = AsyncMock(return_value=SAMPLE_PHONE_NUMBER)
        instance.aclose = AsyncMock()
        result = await conn.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Acme Corp" in result.message
    assert "+1 555-000-1234" in result.message


@pytest.mark.asyncio
async def test_install_missing_all_credentials() -> None:
    conn = WhatsAppConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await conn.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "phone_number_id" in result.message
    assert "access_token" in result.message
    assert "waba_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_access_token() -> None:
    conn = WhatsAppConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"phone_number_id": PHONE_NUMBER_ID, "waba_id": WABA_ID},
    )
    result = await conn.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_missing_phone_number_id() -> None:
    conn = WhatsAppConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"access_token": VALID_TOKEN, "waba_id": WABA_ID},
    )
    result = await conn.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "phone_number_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_waba_id() -> None:
    conn = WhatsAppConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"phone_number_id": PHONE_NUMBER_ID, "access_token": VALID_TOKEN},
    )
    result = await conn.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "waba_id" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    conn = WhatsAppConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=VALID_CONFIG,
    )
    with patch("connector.WhatsAppHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_phone_number = AsyncMock(
            side_effect=WhatsAppAuthError("Invalid or expired access token", 401, 190)
        )
        instance.aclose = AsyncMock()
        result = await conn.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid access token" in result.message


@pytest.mark.asyncio
async def test_install_generic_exception() -> None:
    conn = WhatsAppConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=VALID_CONFIG,
    )
    with patch("connector.WhatsAppHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_phone_number = AsyncMock(side_effect=Exception("unexpected error"))
        instance.aclose = AsyncMock()
        result = await conn.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED
    assert "unexpected error" in result.message


@pytest.mark.asyncio
async def test_install_sets_connector_id_from_config(connector: WhatsAppConnector) -> None:
    with patch("connector.WhatsAppHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_phone_number = AsyncMock(return_value=SAMPLE_PHONE_NUMBER)
        instance.aclose = AsyncMock()
        conn = WhatsAppConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=VALID_CONFIG,
        )
        result = await conn.install()
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_falls_back_to_phone_number_id_when_no_connector_id() -> None:
    conn = WhatsAppConnector(
        tenant_id=TENANT_ID,
        connector_id="",
        config=VALID_CONFIG,
    )
    with patch("connector.WhatsAppHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_phone_number = AsyncMock(return_value=SAMPLE_PHONE_NUMBER)
        instance.aclose = AsyncMock()
        result = await conn.install()
    assert result.connector_id == PHONE_NUMBER_ID


# ═══════════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(connector: WhatsAppConnector) -> None:
    with patch("connector.WhatsAppHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_phone_number = AsyncMock(return_value=SAMPLE_PHONE_NUMBER)
        instance.aclose = AsyncMock()
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message
    assert "+1 555-000-1234" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_token(connector: WhatsAppConnector) -> None:
    with patch("connector.WhatsAppHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_phone_number = AsyncMock(
            side_effect=WhatsAppAuthError("Invalid token", 401, 190)
        )
        instance.aclose = AsyncMock()
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: WhatsAppConnector) -> None:
    with patch("connector.WhatsAppHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_phone_number = AsyncMock(
            side_effect=WhatsAppNetworkError("connection refused")
        )
        instance.aclose = AsyncMock()
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_exception(connector: WhatsAppConnector) -> None:
    with patch("connector.WhatsAppHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_phone_number = AsyncMock(side_effect=Exception("boom"))
        instance.aclose = AsyncMock()
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials(no_creds_connector: WhatsAppConnector) -> None:
    result = await no_creds_connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(connector: WhatsAppConnector) -> None:
    connector.http_client.list_templates = AsyncMock(
        return_value={"data": [], "paging": {}}
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_single_page(connector: WhatsAppConnector) -> None:
    page = {
        "data": [SAMPLE_TEMPLATE, SAMPLE_TEMPLATE_2],
        "paging": {"cursors": {}},
    }
    connector.http_client.list_templates = AsyncMock(return_value=page)
    result = await connector.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_pagination(connector: WhatsAppConnector) -> None:
    page1 = {
        "data": [SAMPLE_TEMPLATE],
        "paging": {"cursors": {"after": "cursor_abc"}},
    }
    page2 = {
        "data": [SAMPLE_TEMPLATE_2],
        "paging": {"cursors": {}},
    }
    connector.http_client.list_templates = AsyncMock(side_effect=[page1, page2])
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert connector.http_client.list_templates.call_count == 2


@pytest.mark.asyncio
async def test_sync_partial_failure(connector: WhatsAppConnector) -> None:
    bad_template: dict = {}  # will fail normalize_template (no id, name, etc.)
    page = {
        "data": [bad_template, SAMPLE_TEMPLATE],
        "paging": {},
    }
    connector.http_client.list_templates = AsyncMock(return_value=page)
    with patch("connector.normalize_template", side_effect=[Exception("bad"), normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)]):
        result = await connector.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1
    assert result.documents_synced >= 1


@pytest.mark.asyncio
async def test_sync_api_error_returns_failed(connector: WhatsAppConnector) -> None:
    connector.http_client.list_templates = AsyncMock(
        side_effect=WhatsAppError("API error", 400, 100)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "API error" in result.message


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(connector: WhatsAppConnector) -> None:
    page = {
        "data": [SAMPLE_TEMPLATE],
        "paging": {},
    }
    connector.http_client.list_templates = AsyncMock(return_value=page)
    ingest_calls: list[tuple] = []

    async def fake_ingest(doc: ConnectorDocument, kb_id: str) -> None:
        ingest_calls.append((doc, kb_id))

    connector._ingest_document = fake_ingest
    result = await connector.sync(full=True, kb_id="kb_123")
    assert len(ingest_calls) == 1
    assert ingest_calls[0][1] == "kb_123"
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_accepts_since_param(connector: WhatsAppConnector) -> None:
    """since param is accepted (Meta doesn't filter by it, but interface must not break)."""
    from datetime import datetime, timezone
    page = {"data": [], "paging": {}}
    connector.http_client.list_templates = AsyncMock(return_value=page)
    result = await connector.sync(full=False, since=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result.status == SyncStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# list_templates()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_templates_single_page(connector: WhatsAppConnector) -> None:
    page = {
        "data": [SAMPLE_TEMPLATE, SAMPLE_TEMPLATE_2],
        "paging": {"cursors": {}},
    }
    connector.http_client.list_templates = AsyncMock(return_value=page)
    results = await connector.list_templates(limit=20)
    assert len(results) == 2
    assert results[0]["id"] == "tpl_001"
    assert results[1]["id"] == "tpl_002"


@pytest.mark.asyncio
async def test_list_templates_follows_cursor(connector: WhatsAppConnector) -> None:
    page1 = {
        "data": [SAMPLE_TEMPLATE],
        "paging": {"cursors": {"after": "next_cursor"}},
    }
    page2 = {
        "data": [SAMPLE_TEMPLATE_2],
        "paging": {"cursors": {}},
    }
    connector.http_client.list_templates = AsyncMock(side_effect=[page1, page2])
    results = await connector.list_templates(limit=1)
    assert len(results) == 2
    assert connector.http_client.list_templates.call_count == 2


@pytest.mark.asyncio
async def test_list_templates_empty(connector: WhatsAppConnector) -> None:
    connector.http_client.list_templates = AsyncMock(
        return_value={"data": [], "paging": {}}
    )
    results = await connector.list_templates()
    assert results == []


@pytest.mark.asyncio
async def test_list_templates_three_pages(connector: WhatsAppConnector) -> None:
    page1 = {"data": [SAMPLE_TEMPLATE], "paging": {"cursors": {"after": "c1"}}}
    page2 = {"data": [SAMPLE_TEMPLATE_2], "paging": {"cursors": {"after": "c2"}}}
    page3 = {"data": [{"id": "tpl_003", "name": "promo"}], "paging": {"cursors": {}}}
    connector.http_client.list_templates = AsyncMock(side_effect=[page1, page2, page3])
    results = await connector.list_templates(limit=1)
    assert len(results) == 3
    assert connector.http_client.list_templates.call_count == 3


# ═══════════════════════════════════════════════════════════════════════════════
# get_template()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_template_returns_data(connector: WhatsAppConnector) -> None:
    connector.http_client.get_template = AsyncMock(return_value=SAMPLE_TEMPLATE)
    result = await connector.get_template("tpl_001")
    assert result["id"] == "tpl_001"
    assert result["name"] == "order_confirmation"
    connector.http_client.get_template.assert_called_once_with(VALID_TOKEN, "tpl_001")


@pytest.mark.asyncio
async def test_get_template_not_found(connector: WhatsAppConnector) -> None:
    connector.http_client.get_template = AsyncMock(
        side_effect=WhatsAppNotFoundError("template", "tpl_999")
    )
    with pytest.raises(WhatsAppNotFoundError):
        await connector.get_template("tpl_999")


@pytest.mark.asyncio
async def test_get_template_auth_error(connector: WhatsAppConnector) -> None:
    connector.http_client.get_template = AsyncMock(
        side_effect=WhatsAppAuthError("Invalid token", 401, 190)
    )
    with pytest.raises(WhatsAppAuthError):
        await connector.get_template("tpl_001")


# ═══════════════════════════════════════════════════════════════════════════════
# list_phone_numbers()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_phone_numbers(connector: WhatsAppConnector) -> None:
    connector.http_client.list_phone_numbers = AsyncMock(
        return_value={"data": [SAMPLE_PHONE_NUMBER]}
    )
    results = await connector.list_phone_numbers()
    assert len(results) == 1
    assert results[0]["display_phone_number"] == "+1 555-000-1234"
    assert results[0]["verified_name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_list_phone_numbers_empty(connector: WhatsAppConnector) -> None:
    connector.http_client.list_phone_numbers = AsyncMock(return_value={"data": []})
    results = await connector.list_phone_numbers()
    assert results == []


@pytest.mark.asyncio
async def test_list_phone_numbers_multiple(connector: WhatsAppConnector) -> None:
    numbers = [
        SAMPLE_PHONE_NUMBER,
        {**SAMPLE_PHONE_NUMBER, "id": "1111111111", "display_phone_number": "+44 7700 900123"},
    ]
    connector.http_client.list_phone_numbers = AsyncMock(return_value={"data": numbers})
    results = await connector.list_phone_numbers()
    assert len(results) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# get_waba()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_waba(connector: WhatsAppConnector) -> None:
    connector.http_client.get_waba = AsyncMock(return_value=SAMPLE_WABA)
    result = await connector.get_waba()
    assert result["id"] == WABA_ID
    assert result["name"] == "Acme Corp WABA"
    assert result["currency"] == "USD"
    assert result["message_template_namespace"] == "acme_ns_001"


@pytest.mark.asyncio
async def test_get_waba_auth_error(connector: WhatsAppConnector) -> None:
    connector.http_client.get_waba = AsyncMock(
        side_effect=WhatsAppAuthError("token expired", 401, 190)
    )
    with pytest.raises(WhatsAppAuthError):
        await connector.get_waba()


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_template()
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_template_basic_fields() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert isinstance(doc, ConnectorDocument)
    assert doc.title == "Template: order_confirmation (en_US)"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert doc.metadata["template_id"] == "tpl_001"
    assert doc.metadata["name"] == "order_confirmation"
    assert doc.metadata["status"] == "APPROVED"
    assert doc.metadata["category"] == "TRANSACTIONAL"
    assert doc.metadata["language"] == "en_US"
    assert doc.metadata["waba_id"] == WABA_ID


def test_normalize_template_source_id_is_16_chars() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert len(doc.source_id) == 16


def test_normalize_template_source_id_is_stable() -> None:
    doc1 = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)
    doc2 = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_template_different_templates_different_ids() -> None:
    doc1 = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)
    doc2 = normalize_template(SAMPLE_TEMPLATE_2, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_template_content_includes_header() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert "HEADER: Order Confirmed" in doc.content


def test_normalize_template_content_includes_body() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert "BODY:" in doc.content
    assert "order" in doc.content.lower()


def test_normalize_template_content_includes_footer() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert "FOOTER:" in doc.content
    assert "Thank you" in doc.content


def test_normalize_template_content_includes_buttons() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert "BUTTON" in doc.content
    assert "Track Order" in doc.content


def test_normalize_template_source_url_contains_waba_id() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert WABA_ID in doc.source_url
    assert "business.facebook.com" in doc.source_url


def test_normalize_template_no_components_returns_fallback() -> None:
    template_no_components = {**SAMPLE_TEMPLATE, "components": []}
    doc = normalize_template(template_no_components, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert "(no components)" in doc.content


def test_normalize_template_media_header() -> None:
    template_with_image = {
        **SAMPLE_TEMPLATE,
        "components": [{"type": "HEADER", "format": "IMAGE"}],
    }
    doc = normalize_template(template_with_image, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert "HEADER: [IMAGE]" in doc.content


def test_normalize_template_missing_optional_fields() -> None:
    minimal = {"id": "tpl_min"}
    doc = normalize_template(minimal, CONNECTOR_ID, TENANT_ID, WABA_ID)
    assert doc.title == "Template: unknown ()"
    assert doc.metadata["status"] == ""
    assert doc.metadata["category"] == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════════════════


def test_whatsapp_error_base() -> None:
    exc = WhatsAppError("test error", status_code=400, code=100)
    assert exc.message == "test error"
    assert exc.status_code == 400
    assert exc.code == 100
    assert str(exc) == "test error"


def test_whatsapp_auth_error_is_subclass() -> None:
    exc = WhatsAppAuthError("invalid token", 401, 190)
    assert isinstance(exc, WhatsAppError)
    assert exc.status_code == 401
    assert exc.code == 190


def test_whatsapp_network_error_is_subclass() -> None:
    exc = WhatsAppNetworkError("timeout")
    assert isinstance(exc, WhatsAppError)


def test_whatsapp_rate_limit_error() -> None:
    exc = WhatsAppRateLimitError("rate limited", retry_after=30.0)
    assert isinstance(exc, WhatsAppError)
    assert exc.status_code == 429
    assert exc.retry_after == 30.0


def test_whatsapp_rate_limit_error_default_retry_after() -> None:
    exc = WhatsAppRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_whatsapp_not_found_error() -> None:
    exc = WhatsAppNotFoundError("template", "tpl_999")
    assert isinstance(exc, WhatsAppError)
    assert "tpl_999" in str(exc)
    assert exc.resource == "template"
    assert exc.resource_id == "tpl_999"


def test_whatsapp_not_found_error_status_code() -> None:
    exc = WhatsAppNotFoundError("phone", "123")
    assert exc.status_code == 400
    assert exc.code == 100


# ═══════════════════════════════════════════════════════════════════════════════
# with_retry()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await with_retry(fn, max_attempts=3)
    assert result == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_raises_auth_error_immediately() -> None:
    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise WhatsAppAuthError("invalid token", 401, 190)

    with pytest.raises(WhatsAppAuthError):
        await with_retry(fn, max_attempts=3)
    assert calls == 1  # no retry on auth errors


@pytest.mark.asyncio
async def test_with_retry_retries_on_whatsapp_error() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise WhatsAppNetworkError("timeout")
        return "success"

    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == "success"
    assert calls == 3


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise WhatsAppNetworkError("always fails")

    with pytest.raises(WhatsAppNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert calls == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    calls = 0
    slept: list[float] = []

    import asyncio as _asyncio
    original_sleep = _asyncio.sleep

    async def fake_sleep(secs: float) -> None:
        slept.append(secs)

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WhatsAppRateLimitError("rate limited", retry_after=5.0)
        return "ok"

    with patch("helpers.utils.asyncio.sleep", fake_sleep):
        result = await with_retry(fn, max_attempts=3, base_delay=1.0)
    assert result == "ok"
    assert slept[0] == 5.0  # honours Retry-After


# ═══════════════════════════════════════════════════════════════════════════════
# Connector lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_clears_http_client(connector: WhatsAppConnector) -> None:
    connector.http_client.aclose = AsyncMock()
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_aclose_safe_when_no_client() -> None:
    conn = WhatsAppConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    conn.http_client = None
    await conn.aclose()  # must not raise


@pytest.mark.asyncio
async def test_context_manager() -> None:
    conn = WhatsAppConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    conn.http_client = MagicMock()
    conn.http_client.aclose = AsyncMock()
    async with conn as c:
        assert c is conn
    assert conn.http_client is None


def test_ensure_client_creates_client_when_none() -> None:
    conn = WhatsAppConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    assert conn.http_client is None
    with patch("connector.WhatsAppHTTPClient") as MockClient:
        _ = conn._ensure_client()
        MockClient.assert_called_once()


def test_missing_fields_all_empty() -> None:
    conn = WhatsAppConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    missing = conn._missing_fields()
    assert set(missing) == {"phone_number_id", "access_token", "waba_id"}


def test_missing_fields_none_when_all_set() -> None:
    conn = WhatsAppConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    assert conn._missing_fields() == []
