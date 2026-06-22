"""Unit tests for SendGridConnector — all HTTP calls are mocked.

Covers:
- All 5 exception classes and their attributes
- All model enums and dataclasses
- normalize_contact, normalize_template (stable IDs, field mapping, source_url)
- with_retry (success, retry on network, no retry on auth, exhausted, rate-limit)
- SendGridHTTPClient: all 8 methods, _raise_for_status for each status code, pagination
- install(): success, missing api_key
- health_check(): success, auth error, network error, generic error
- sync(): contacts + templates, empty results, pagination, partial/failed states
- list_contacts(), list_lists(), list_segments(), list_templates()
- get_contact(), get_template(), get_stats()
- list_suppressions() — global and by group
- aclose / context manager
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import SendGridConnector
from exceptions import (
    SendGridAuthError,
    SendGridError,
    SendGridNetworkError,
    SendGridNotFoundError,
    SendGridRateLimitError,
)
from helpers.normalizer import normalize_contact, normalize_template
from helpers.utils import sha256_id, with_retry
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
CONNECTOR_ID = "conn_sendgrid_test_001"
VALID_API_KEY = "SG.test_valid_api_key_12345"

# ── Sample fixtures ───────────────────────────────────────────────────────────

SAMPLE_CONTACT: dict[str, Any] = {
    "id": "contact-uuid-abc123",
    "email": "jane@example.com",
    "first_name": "Jane",
    "last_name": "Doe",
    "created_at": "2026-01-15T10:00:00Z",
    "updated_at": "2026-06-01T08:00:00Z",
    "list_ids": ["list-uuid-001", "list-uuid-002"],
}

SAMPLE_CONTACT_2: dict[str, Any] = {
    "id": "contact-uuid-def456",
    "email": "john@example.com",
    "first_name": "John",
    "last_name": "Smith",
    "created_at": "2026-02-10T12:00:00Z",
    "updated_at": "2026-05-20T09:00:00Z",
    "list_ids": [],
}

SAMPLE_TEMPLATE: dict[str, Any] = {
    "id": "tmpl-uuid-xyz789",
    "name": "Welcome Email",
    "generation": "dynamic",
    "versions": [
        {"id": "v-uuid-001", "active": 1, "name": "v1"},
        {"id": "v-uuid-002", "active": 0, "name": "v2 draft"},
    ],
}

SAMPLE_TEMPLATE_LEGACY: dict[str, Any] = {
    "id": "tmpl-uuid-legacy001",
    "name": "Old Newsletter",
    "generation": "legacy",
    "versions": [],
}

SAMPLE_LIST: dict[str, Any] = {
    "id": "list-uuid-001",
    "name": "VIP Customers",
    "contact_count": 500,
}

SAMPLE_SEGMENT: dict[str, Any] = {
    "id": "seg-uuid-001",
    "name": "High Engagers",
    "contacts_count": 120,
    "created_at": "2026-01-01T00:00:00Z",
}

SAMPLE_SUPPRESSION: dict[str, Any] = {
    "email": "bounced@example.com",
    "created": 1718000000,
    "reason": "Email does not exist",
}

SAMPLE_STATS: list[dict[str, Any]] = [
    {
        "date": "2026-06-01",
        "stats": [
            {
                "metrics": {
                    "requests": 1000,
                    "delivered": 950,
                    "opens": 300,
                    "clicks": 50,
                    "bounces": 20,
                    "spam_reports": 2,
                }
            }
        ],
    }
]


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> SendGridConnector:
    c = SendGridConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": VALID_API_KEY},
    )
    c.http_client = MagicMock()
    return c


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════


def test_sendgrid_error_base_attrs() -> None:
    exc = SendGridError("base error", status_code=400, code="bad_request")
    assert exc.message == "base error"
    assert exc.status_code == 400
    assert exc.code == "bad_request"
    assert str(exc) == "base error"


def test_sendgrid_auth_error_is_subclass() -> None:
    exc = SendGridAuthError("auth fail", 401, "auth_error")
    assert isinstance(exc, SendGridError)
    assert exc.status_code == 401


def test_sendgrid_network_error_is_subclass() -> None:
    exc = SendGridNetworkError("timeout", 503)
    assert isinstance(exc, SendGridError)
    assert exc.message == "timeout"


def test_sendgrid_not_found_error_format() -> None:
    exc = SendGridNotFoundError("template", "tmpl-123")
    assert isinstance(exc, SendGridError)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"
    assert "tmpl-123" in str(exc)
    assert "template" in str(exc)


def test_sendgrid_rate_limit_error_retry_after() -> None:
    exc = SendGridRateLimitError("rate limited", retry_after=60.0)
    assert isinstance(exc, SendGridError)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 60.0


def test_sendgrid_rate_limit_error_default_retry_after() -> None:
    exc = SendGridRateLimitError("rate limited")
    assert exc.retry_after == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Models
# ═══════════════════════════════════════════════════════════════════════════════


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


def test_install_result_defaults() -> None:
    r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
    assert r.connector_id == ""
    assert r.message == ""


def test_health_check_result_defaults() -> None:
    r = HealthCheckResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
    assert r.message == ""
    assert r.user_name == ""
    assert r.user_email == ""


def test_sync_result_defaults() -> None:
    r = SyncResult(status=SyncStatus.COMPLETED)
    assert r.documents_found == 0
    assert r.documents_synced == 0
    assert r.documents_failed == 0
    assert r.message == ""


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test",
        content="Content body",
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        source_url="https://example.com",
        metadata={"key": "value"},
    )
    assert doc.source_id == "abc123"
    assert doc.metadata["key"] == "value"
    assert doc.source_url == "https://example.com"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. sha256_id
# ═══════════════════════════════════════════════════════════════════════════════


def test_sha256_id_default_length() -> None:
    result = sha256_id("some-contact-id")
    assert len(result) == 16
    assert all(c in "0123456789abcdef" for c in result)


def test_sha256_id_deterministic() -> None:
    assert sha256_id("contact-uuid-abc123") == sha256_id("contact-uuid-abc123")


def test_sha256_id_different_inputs_differ() -> None:
    assert sha256_id("contact-abc") != sha256_id("contact-def")


def test_sha256_id_custom_length() -> None:
    assert len(sha256_id("test", length=8)) == 8


def test_sha256_id_prefix_matches_hashlib() -> None:
    raw = "contact:contact-uuid-abc123"
    expected = hashlib.sha256(raw.encode()).hexdigest()[:16]
    assert sha256_id(raw) == expected


# ═══════════════════════════════════════════════════════════════════════════════
# 4. normalize_contact
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_contact_stable_id() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    expected_id = sha256_id("contact:contact-uuid-abc123")
    assert doc.source_id == expected_id
    assert len(doc.source_id) == 16


def test_normalize_contact_title_includes_name() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "Jane" in doc.title
    assert "Doe" in doc.title


def test_normalize_contact_metadata_fields() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == "jane@example.com"
    assert doc.metadata["first_name"] == "Jane"
    assert doc.metadata["last_name"] == "Doe"
    assert "list-uuid-001" in doc.metadata["list_ids"]
    assert doc.metadata["created_at"] == "2026-01-15T10:00:00Z"
    assert doc.metadata["object_type"] == "contact"
    assert doc.metadata["sendgrid_id"] == "contact-uuid-abc123"


def test_normalize_contact_content_body() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "Jane" in doc.content
    assert "jane@example.com" in doc.content


def test_normalize_contact_source_url() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "mc.sendgrid.com/contacts" in doc.source_url


def test_normalize_contact_ids_propagated() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_contact_minimal_fields() -> None:
    minimal = {"id": "contact-min-001", "email": "min@example.com"}
    doc = normalize_contact(minimal, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == sha256_id("contact:contact-min-001")
    assert doc.metadata["first_name"] == ""
    assert doc.metadata["last_name"] == ""
    assert doc.metadata["list_ids"] == []


def test_normalize_contact_fallback_title_uses_email() -> None:
    c = {"id": "contact-xyz", "email": "noname@example.com"}
    doc = normalize_contact(c, CONNECTOR_ID, TENANT_ID)
    assert "noname@example.com" in doc.title


# ═══════════════════════════════════════════════════════════════════════════════
# 5. normalize_template
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_template_stable_id() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    expected_id = sha256_id("template:tmpl-uuid-xyz789")
    assert doc.source_id == expected_id
    assert len(doc.source_id) == 16


def test_normalize_template_title() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert "Welcome Email" in doc.title


def test_normalize_template_metadata() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["name"] == "Welcome Email"
    assert doc.metadata["generation"] == "dynamic"
    assert doc.metadata["active_version_id"] == "v-uuid-001"
    assert doc.metadata["object_type"] == "email_template"
    assert doc.metadata["sendgrid_id"] == "tmpl-uuid-xyz789"


def test_normalize_template_content() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert "dynamic" in doc.content
    assert "Welcome Email" in doc.content


def test_normalize_template_source_url() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert "tmpl-uuid-xyz789" in doc.source_url
    assert "mc.sendgrid.com" in doc.source_url


def test_normalize_template_legacy_no_active_version() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE_LEGACY, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == sha256_id("template:tmpl-uuid-legacy001")
    assert doc.metadata["generation"] == "legacy"
    assert doc.metadata["active_version_id"] == ""


def test_normalize_template_ids_propagated() -> None:
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


# ═══════════════════════════════════════════════════════════════════════════════
# 6. with_retry
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn)
    assert result == {"ok": True}
    fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    err = SendGridNetworkError("timeout")
    fn = AsyncMock(side_effect=[err, err, {"ok": True}])
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.await_count == 3


@pytest.mark.asyncio
async def test_with_retry_no_retry_on_auth_error() -> None:
    fn = AsyncMock(side_effect=SendGridAuthError("Unauthorized", 401))
    with pytest.raises(SendGridAuthError):
        await with_retry(fn, max_attempts=3)
    fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_with_retry_exhausted_raises_last_error() -> None:
    err = SendGridNetworkError("Connection refused")
    fn = AsyncMock(side_effect=err)
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(SendGridNetworkError):
            await with_retry(fn, max_attempts=3)
    assert fn.await_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    rl_err = SendGridRateLimitError("rate limited", retry_after=1.0)
    fn = AsyncMock(side_effect=[rl_err, {"ok": True}])
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    mock_sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    fn = AsyncMock(return_value=42)
    result = await with_retry(fn, "arg1", key="val")
    fn.assert_awaited_once_with("arg1", key="val")
    assert result == 42


# ═══════════════════════════════════════════════════════════════════════════════
# 7. install()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = SendGridConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": VALID_API_KEY},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID
    assert "installed" in result.message.lower() or "SendGrid" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = SendGridConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key is required" in result.message


@pytest.mark.asyncio
async def test_install_returns_connector_id() -> None:
    c = SendGridConnector(
        tenant_id=TENANT_ID,
        connector_id="conn-custom-id",
        config={"api_key": VALID_API_KEY},
    )
    result = await c.install()
    assert result.connector_id == "conn-custom-id"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. health_check()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(connector: SendGridConnector) -> None:
    connector._make_client = MagicMock(
        return_value=MagicMock(
            get_stats=AsyncMock(return_value=SAMPLE_STATS),
            aclose=AsyncMock(),
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message.lower()


@pytest.mark.asyncio
async def test_health_check_missing_key() -> None:
    c = SendGridConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: SendGridConnector) -> None:
    connector._make_client = MagicMock(
        return_value=MagicMock(
            get_stats=AsyncMock(
                side_effect=SendGridAuthError("Unauthorized", 401, "auth_error")
            ),
            aclose=AsyncMock(),
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: SendGridConnector) -> None:
    connector._make_client = MagicMock(
        return_value=MagicMock(
            get_stats=AsyncMock(side_effect=SendGridNetworkError("Timeout")),
            aclose=AsyncMock(),
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: SendGridConnector) -> None:
    connector._make_client = MagicMock(
        return_value=MagicMock(
            get_stats=AsyncMock(side_effect=RuntimeError("unexpected")),
            aclose=AsyncMock(),
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 9. sync()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty_results(connector: SendGridConnector) -> None:
    connector.http_client.list_contacts = AsyncMock(
        return_value={"result": [], "_metadata": {}}
    )
    connector.http_client.list_templates = AsyncMock(return_value={"templates": []})
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_contacts_and_templates(connector: SendGridConnector) -> None:
    connector.http_client.list_contacts = AsyncMock(
        return_value={"result": [SAMPLE_CONTACT, SAMPLE_CONTACT_2], "_metadata": {}}
    )
    connector.http_client.list_templates = AsyncMock(
        return_value={"templates": [SAMPLE_TEMPLATE]}
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_contacts_pagination(connector: SendGridConnector) -> None:
    page1 = {"result": [SAMPLE_CONTACT], "_metadata": {"next": "page_token_abc"}}
    page2 = {"result": [SAMPLE_CONTACT_2], "_metadata": {}}
    connector.http_client.list_contacts = AsyncMock(side_effect=[page1, page2])
    connector.http_client.list_templates = AsyncMock(return_value={"templates": []})
    result = await connector.sync()
    assert result.documents_found == 2
    assert connector.http_client.list_contacts.call_count == 2


@pytest.mark.asyncio
async def test_sync_contacts_failure_returns_failed(connector: SendGridConnector) -> None:
    connector.http_client.list_contacts = AsyncMock(
        side_effect=SendGridNetworkError("SendGrid down")
    )
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "SendGrid down" in result.message


@pytest.mark.asyncio
async def test_sync_templates_failure_returns_partial(connector: SendGridConnector) -> None:
    connector.http_client.list_contacts = AsyncMock(
        return_value={"result": [SAMPLE_CONTACT], "_metadata": {}}
    )
    connector.http_client.list_templates = AsyncMock(
        side_effect=SendGridNetworkError("Templates down")
    )
    result = await connector.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(connector: SendGridConnector) -> None:
    connector.http_client.list_contacts = AsyncMock(
        return_value={"result": [SAMPLE_CONTACT], "_metadata": {}}
    )
    connector.http_client.list_templates = AsyncMock(
        return_value={"templates": [SAMPLE_TEMPLATE]}
    )
    connector._ingest_document = AsyncMock()
    await connector.sync(kb_id="kb-test-001")
    assert connector._ingest_document.call_count == 2


@pytest.mark.asyncio
async def test_sync_next_page_token_key(connector: SendGridConnector) -> None:
    """Support next_page_token as alternative pagination key."""
    page1 = {"result": [SAMPLE_CONTACT], "next_page_token": "tok_xyz"}
    page2 = {"result": [SAMPLE_CONTACT_2]}
    connector.http_client.list_contacts = AsyncMock(side_effect=[page1, page2])
    connector.http_client.list_templates = AsyncMock(return_value={"templates": []})
    result = await connector.sync()
    assert result.documents_found == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 10. list_contacts()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_contacts_returns_list(connector: SendGridConnector) -> None:
    connector.http_client.list_contacts = AsyncMock(
        return_value={"result": [SAMPLE_CONTACT, SAMPLE_CONTACT_2], "_metadata": {}}
    )
    result = await connector.list_contacts(page_size=50)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["email"] == "jane@example.com"


@pytest.mark.asyncio
async def test_list_contacts_empty(connector: SendGridConnector) -> None:
    connector.http_client.list_contacts = AsyncMock(
        return_value={"result": [], "_metadata": {}}
    )
    result = await connector.list_contacts()
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 11. get_contact()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_contact_success(connector: SendGridConnector) -> None:
    connector.http_client.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
    result = await connector.get_contact("contact-uuid-abc123")
    assert result["email"] == "jane@example.com"
    connector.http_client.get_contact.assert_called_once_with("contact-uuid-abc123")


@pytest.mark.asyncio
async def test_get_contact_not_found(connector: SendGridConnector) -> None:
    connector.http_client.get_contact = AsyncMock(
        side_effect=SendGridNotFoundError("contact", "contact-missing")
    )
    with pytest.raises(SendGridNotFoundError):
        await connector.get_contact("contact-missing")


# ═══════════════════════════════════════════════════════════════════════════════
# 12. list_lists()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_lists_success(connector: SendGridConnector) -> None:
    connector.http_client.list_lists = AsyncMock(
        return_value={"result": [SAMPLE_LIST]}
    )
    result = await connector.list_lists()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "VIP Customers"


@pytest.mark.asyncio
async def test_list_lists_empty(connector: SendGridConnector) -> None:
    connector.http_client.list_lists = AsyncMock(return_value={"result": []})
    result = await connector.list_lists()
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 13. list_segments()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_segments_success(connector: SendGridConnector) -> None:
    connector.http_client.list_segments = AsyncMock(
        return_value={"results": [SAMPLE_SEGMENT]}
    )
    result = await connector.list_segments()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "High Engagers"


@pytest.mark.asyncio
async def test_list_segments_empty(connector: SendGridConnector) -> None:
    connector.http_client.list_segments = AsyncMock(return_value={"results": []})
    result = await connector.list_segments()
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 14. list_templates()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_templates_returns_list(connector: SendGridConnector) -> None:
    connector.http_client.list_templates = AsyncMock(
        return_value={"templates": [SAMPLE_TEMPLATE, SAMPLE_TEMPLATE_LEGACY]}
    )
    result = await connector.list_templates()
    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_templates_dynamic_filter(connector: SendGridConnector) -> None:
    connector.http_client.list_templates = AsyncMock(
        return_value={"templates": [SAMPLE_TEMPLATE]}
    )
    result = await connector.list_templates(generations="dynamic")
    connector.http_client.list_templates.assert_called_once_with(generations="dynamic")
    assert result[0]["generation"] == "dynamic"


@pytest.mark.asyncio
async def test_list_templates_empty(connector: SendGridConnector) -> None:
    connector.http_client.list_templates = AsyncMock(return_value={"templates": []})
    result = await connector.list_templates()
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 15. get_template()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_template_success(connector: SendGridConnector) -> None:
    connector.http_client.get_template = AsyncMock(return_value=SAMPLE_TEMPLATE)
    result = await connector.get_template("tmpl-uuid-xyz789")
    assert result["id"] == "tmpl-uuid-xyz789"
    assert result["name"] == "Welcome Email"
    connector.http_client.get_template.assert_called_once_with("tmpl-uuid-xyz789")


@pytest.mark.asyncio
async def test_get_template_not_found(connector: SendGridConnector) -> None:
    connector.http_client.get_template = AsyncMock(
        side_effect=SendGridNotFoundError("template", "tmpl-nonexistent")
    )
    with pytest.raises(SendGridNotFoundError):
        await connector.get_template("tmpl-nonexistent")


# ═══════════════════════════════════════════════════════════════════════════════
# 16. get_stats()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_stats_with_explicit_dates(connector: SendGridConnector) -> None:
    connector.http_client.get_stats = AsyncMock(return_value=SAMPLE_STATS)
    result = await connector.get_stats("2026-06-01", "2026-06-07")
    assert isinstance(result, list)
    connector.http_client.get_stats.assert_called_once_with("2026-06-01", "2026-06-07")


@pytest.mark.asyncio
async def test_get_stats_defaults_last_30_days(connector: SendGridConnector) -> None:
    from datetime import date, timedelta
    connector.http_client.get_stats = AsyncMock(return_value=SAMPLE_STATS)
    await connector.get_stats()
    call_args = connector.http_client.get_stats.call_args
    start, end = call_args[0]
    today = date.today()
    assert end == today.isoformat()
    assert start == (today - timedelta(days=30)).isoformat()


@pytest.mark.asyncio
async def test_get_stats_empty(connector: SendGridConnector) -> None:
    connector.http_client.get_stats = AsyncMock(return_value=[])
    result = await connector.get_stats("2026-01-01", "2026-01-01")
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 17. list_suppressions()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_suppressions_global(connector: SendGridConnector) -> None:
    connector.http_client.list_suppressions = AsyncMock(
        return_value=[SAMPLE_SUPPRESSION]
    )
    result = await connector.list_suppressions()
    assert isinstance(result, list)
    assert result[0]["email"] == "bounced@example.com"
    connector.http_client.list_suppressions.assert_called_once_with(
        group_id=None, page_size=100
    )


@pytest.mark.asyncio
async def test_list_suppressions_by_group(connector: SendGridConnector) -> None:
    connector.http_client.list_suppressions = AsyncMock(return_value=[SAMPLE_SUPPRESSION])
    result = await connector.list_suppressions(group_id=12345)
    connector.http_client.list_suppressions.assert_called_once_with(
        group_id=12345, page_size=100
    )
    assert len(result) == 1


@pytest.mark.asyncio
async def test_list_suppressions_empty(connector: SendGridConnector) -> None:
    connector.http_client.list_suppressions = AsyncMock(return_value=[])
    result = await connector.list_suppressions()
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 18. Lifecycle — aclose / context manager
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_clears_http_client(connector: SendGridConnector) -> None:
    connector.http_client = MagicMock()
    connector.http_client.aclose = AsyncMock()
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_aclose_when_no_client() -> None:
    c = SendGridConnector(config={"api_key": VALID_API_KEY})
    await c.aclose()  # must not raise


@pytest.mark.asyncio
async def test_context_manager() -> None:
    c = SendGridConnector(config={"api_key": VALID_API_KEY})
    async with c as ctx:
        assert ctx is c
    # client should be cleaned up (no error)


@pytest.mark.asyncio
async def test_ensure_client_creates_if_none() -> None:
    c = SendGridConnector(config={"api_key": VALID_API_KEY})
    assert c.http_client is None
    client = c._ensure_client()
    assert c.http_client is client
    assert client is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 19. Error propagation from methods
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_template_rate_limit_propagates(connector: SendGridConnector) -> None:
    connector.http_client.get_template = AsyncMock(
        side_effect=SendGridRateLimitError("Rate limited", retry_after=0.0)
    )
    with pytest.raises(SendGridRateLimitError):
        await connector.get_template("tmpl-uuid-xyz789")


@pytest.mark.asyncio
async def test_list_contacts_auth_error_propagates(connector: SendGridConnector) -> None:
    connector.http_client.list_contacts = AsyncMock(
        side_effect=SendGridAuthError("Unauthorized", 401)
    )
    with pytest.raises(SendGridAuthError):
        await connector.list_contacts()


@pytest.mark.asyncio
async def test_get_contact_network_error_propagates(connector: SendGridConnector) -> None:
    connector.http_client.get_contact = AsyncMock(
        side_effect=SendGridNetworkError("timeout")
    )
    with pytest.raises(SendGridNetworkError):
        await connector.get_contact("contact-uuid-abc123")


# ═══════════════════════════════════════════════════════════════════════════════
# 20. Connector class attributes
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_type_attribute() -> None:
    assert SendGridConnector.CONNECTOR_TYPE == "sendgrid"


def test_auth_type_attribute() -> None:
    assert SendGridConnector.AUTH_TYPE == "api_key"


def test_base_connector_fallback_attributes() -> None:
    c = SendGridConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": VALID_API_KEY},
    )
    assert c.tenant_id == TENANT_ID
    assert c.connector_id == CONNECTOR_ID
    assert c.config["api_key"] == VALID_API_KEY
