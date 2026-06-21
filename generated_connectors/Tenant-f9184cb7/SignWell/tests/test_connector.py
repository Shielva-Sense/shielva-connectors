"""Unit tests for SignWellConnector — respx-mocked, zero real network I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, NormalizedDocument

from connector import SignWellConnector
from exceptions import (
    SignWellAuthError,
    SignWellBadRequestError,
    SignWellConflictError,
    SignWellError,
    SignWellNetworkError,
    SignWellNotFound,
    SignWellNotFoundError,
    SignWellRateLimitError,
    SignWellServerError,
)
from helpers.normalizer import normalize_document, normalize_template
from helpers.utils import safe_get, validate_recipients

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    SAMPLE_DOCUMENT,
    SAMPLE_TEMPLATE,
    SAMPLE_WEBHOOK,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install() — credential validation
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_authorize_returns_token_info(connector):
    token = await connector.authorize()
    assert token.access_token == TEST_API_KEY
    assert token.token_type == "ApiKey"


@pytest.mark.asyncio
async def test_authorize_raises_when_no_api_key(connector):
    connector.config["api_key"] = ""
    with pytest.raises(SignWellAuthError):
        await connector.authorize()


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_x_api_key_header_is_sent(connector):
    """Connector must send the api_key in X-Api-Key (NOT Authorization)."""
    route = respx.get(f"{BASE_URL}/me").mock(
        return_value=httpx.Response(200, json={"id": "u1"})
    )
    await connector.get_me()
    assert route.called
    sent = route.calls.last.request
    assert sent.headers.get("x-api-key") == TEST_API_KEY
    # Make sure we are NOT setting Authorization with a Bearer prefix.
    auth = sent.headers.get("authorization", "")
    assert not auth.lower().startswith("bearer ")


# ═══════════════════════════════════════════════════════════════════════════
# health_check() — GET /me
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    route = respx.get(f"{BASE_URL}/me").mock(
        return_value=httpx.Response(
            200, json={"id": "u_1", "email": "vivek@example.com"}
        )
    )
    result = await connector.health_check()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_maps_to_offline_token_expired(connector):
    respx.get(f"{BASE_URL}/me").mock(
        return_value=httpx.Response(401, json={"message": "Invalid api key"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_maps_to_unhealthy_invalid_creds(connector):
    respx.get(f"{BASE_URL}/me").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# list_documents()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_documents_with_status_filter(connector):
    route = respx.get(f"{BASE_URL}/documents").mock(
        return_value=httpx.Response(
            200, json={"documents": [SAMPLE_DOCUMENT], "next_page": None}
        )
    )
    result = await connector.list_documents(page=1, status="completed")
    assert route.called
    assert result["documents"][0]["id"] == "doc_abc123"
    sent_url = str(route.calls.last.request.url)
    assert "status=completed" in sent_url
    assert "page=1" in sent_url


# ═══════════════════════════════════════════════════════════════════════════
# get_document()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_document_success(connector):
    respx.get(f"{BASE_URL}/documents/doc_abc123").mock(
        return_value=httpx.Response(200, json=SAMPLE_DOCUMENT)
    )
    result = await connector.get_document("doc_abc123")
    assert result["id"] == "doc_abc123"
    assert result["status"] == "sent"


@respx.mock
@pytest.mark.asyncio
async def test_get_document_not_found(connector):
    respx.get(f"{BASE_URL}/documents/missing").mock(
        return_value=httpx.Response(404, json={"message": "Document not found"})
    )
    with pytest.raises(SignWellNotFoundError):
        await connector.get_document("missing")


# ═══════════════════════════════════════════════════════════════════════════
# create_document() — body shape verification
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_create_document_sends_recipients_and_files(connector):
    captured = {}

    def _capture(request):
        captured["body"] = _json.loads(request.content)
        return httpx.Response(201, json={"id": "doc_new", "status": "draft"})

    respx.post(f"{BASE_URL}/documents").mock(side_effect=_capture)

    recipients = [
        {"name": "Alice", "email": "alice@example.com", "id": "rec_1"},
    ]
    files = [{"name": "contract.pdf", "file_base64": "JVBERi0..."}]
    result = await connector.create_document(
        name="Service Agreement",
        recipients=recipients,
        files=files,
        subject="Please sign",
        test_mode=True,
        draft=False,
    )

    assert result["id"] == "doc_new"
    body = captured["body"]
    assert body["name"] == "Service Agreement"
    assert body["recipients"] == recipients
    assert body["files"] == files
    assert body["test_mode"] is True
    assert body["subject"] == "Please sign"


@respx.mock
@pytest.mark.asyncio
async def test_create_document_uses_test_mode_default(connector):
    """When `test_mode` is not passed, the install-time default is applied."""
    captured = {}

    def _capture(request):
        captured["body"] = _json.loads(request.content)
        return httpx.Response(201, json={"id": "doc_new"})

    respx.post(f"{BASE_URL}/documents").mock(side_effect=_capture)

    await connector.create_document(
        name="x",
        recipients=[{"name": "A", "email": "a@x.com"}],
        file_urls=["https://example.com/x.pdf"],
    )
    assert captured["body"]["test_mode"] is True  # default from TEST_CONFIG


@pytest.mark.asyncio
async def test_create_document_requires_recipients(connector):
    with pytest.raises(ValueError, match="recipients"):
        await connector.create_document(
            name="x", recipients=[], files=[{"name": "a"}]
        )


@pytest.mark.asyncio
async def test_create_document_requires_files_or_urls(connector):
    with pytest.raises(ValueError, match="files or file_urls"):
        await connector.create_document(
            name="x",
            recipients=[{"name": "A", "email": "a@x.com"}],
        )


@pytest.mark.asyncio
async def test_create_document_rejects_recipient_missing_email(connector):
    with pytest.raises(ValueError, match="email is required"):
        await connector.create_document(
            name="x",
            recipients=[{"name": "Bob"}],
            file_urls=["https://example.com/x.pdf"],
        )


# ═══════════════════════════════════════════════════════════════════════════
# send / cancel / archive / delete document
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_send_document_success(connector):
    route = respx.post(f"{BASE_URL}/documents/doc_abc123/send").mock(
        return_value=httpx.Response(200, json={"id": "doc_abc123", "status": "sent"})
    )
    result = await connector.send_document("doc_abc123")
    assert route.called
    assert result["status"] == "sent"


@respx.mock
@pytest.mark.asyncio
async def test_cancel_document_success(connector):
    respx.post(f"{BASE_URL}/documents/doc_abc123/cancel").mock(
        return_value=httpx.Response(
            200, json={"id": "doc_abc123", "status": "canceled"}
        )
    )
    result = await connector.cancel_document("doc_abc123")
    assert result["status"] == "canceled"


@respx.mock
@pytest.mark.asyncio
async def test_archive_document_success(connector):
    route = respx.post(f"{BASE_URL}/documents/doc_abc123/archive").mock(
        return_value=httpx.Response(
            200, json={"id": "doc_abc123", "archived": True}
        )
    )
    result = await connector.archive_document("doc_abc123")
    assert route.called
    assert result["archived"] is True


@respx.mock
@pytest.mark.asyncio
async def test_delete_document_success(connector):
    respx.delete(f"{BASE_URL}/documents/doc_abc123").mock(
        return_value=httpx.Response(200, json={"deleted": True})
    )
    result = await connector.delete_document("doc_abc123")
    assert result == {"deleted": True}


# ═══════════════════════════════════════════════════════════════════════════
# download_completed_document() — returns bytes
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_download_completed_document_returns_bytes(connector):
    pdf_bytes = b"%PDF-1.4 fake pdf bytes"
    route = respx.get(f"{BASE_URL}/documents/doc_abc123/completed_pdf").mock(
        return_value=httpx.Response(
            200, content=pdf_bytes, headers={"Content-Type": "application/pdf"}
        )
    )
    result = await connector.download_completed_document("doc_abc123")
    assert isinstance(result, bytes)
    assert result == pdf_bytes
    # Accept header should be application/pdf for the bytes download
    assert route.calls.last.request.headers["accept"] == "application/pdf"


@respx.mock
@pytest.mark.asyncio
async def test_download_document_alias_works(connector):
    pdf_bytes = b"%PDF-1.4 alias"
    respx.get(f"{BASE_URL}/documents/doc_abc123/completed_pdf").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )
    result = await connector.download_document("doc_abc123")
    assert result == pdf_bytes


# ═══════════════════════════════════════════════════════════════════════════
# Templates
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_templates_success(connector):
    respx.get(f"{BASE_URL}/templates").mock(
        return_value=httpx.Response(200, json={"templates": [SAMPLE_TEMPLATE]})
    )
    result = await connector.list_templates(page=1, q="NDA")
    assert result["templates"][0]["id"] == "tpl_xyz789"


@respx.mock
@pytest.mark.asyncio
async def test_get_template_success(connector):
    respx.get(f"{BASE_URL}/templates/tpl_xyz789").mock(
        return_value=httpx.Response(200, json=SAMPLE_TEMPLATE)
    )
    result = await connector.get_template("tpl_xyz789")
    assert result["id"] == "tpl_xyz789"


@respx.mock
@pytest.mark.asyncio
async def test_create_document_from_template_success(connector):
    captured = {}

    def _capture(request):
        captured["body"] = _json.loads(request.content)
        return httpx.Response(201, json={"id": "doc_from_tpl", "status": "draft"})

    respx.post(f"{BASE_URL}/document_templates/documents").mock(side_effect=_capture)

    recipients = [{"name": "Carol", "email": "carol@example.com"}]
    template_fields = [{"api_id": "company_name", "value": "Shielva Inc."}]
    result = await connector.create_document_from_template(
        template_id="tpl_xyz789",
        name="Customer NDA",
        recipients=recipients,
        template_fields=template_fields,
    )
    assert result["id"] == "doc_from_tpl"
    body = captured["body"]
    assert body["template_id"] == "tpl_xyz789"
    assert body["template_fields"] == template_fields
    assert body["test_mode"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Recipients & reminders
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_recipients_success(connector):
    respx.get(f"{BASE_URL}/documents/doc_abc123/recipients").mock(
        return_value=httpx.Response(
            200, json={"recipients": SAMPLE_DOCUMENT["recipients"]}
        )
    )
    result = await connector.list_recipients("doc_abc123")
    assert len(result["recipients"]) == 2


@respx.mock
@pytest.mark.asyncio
async def test_send_reminder_success(connector):
    route = respx.post(
        f"{BASE_URL}/documents/doc_abc123/recipients/rec_2/reminder"
    ).mock(return_value=httpx.Response(200, json={"sent": True}))
    result = await connector.send_reminder("doc_abc123", "rec_2")
    assert route.called
    assert result == {"sent": True}


# ═══════════════════════════════════════════════════════════════════════════
# Webhooks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_webhooks_success(connector):
    respx.get(f"{BASE_URL}/api_application/webhooks").mock(
        return_value=httpx.Response(200, json={"webhooks": [SAMPLE_WEBHOOK]})
    )
    result = await connector.list_webhooks()
    assert result["webhooks"][0]["id"] == "wh_001"


@respx.mock
@pytest.mark.asyncio
async def test_create_webhook_posts_body(connector):
    captured = {}

    def _capture(request):
        captured["body"] = _json.loads(request.content)
        return httpx.Response(201, json=SAMPLE_WEBHOOK)

    respx.post(f"{BASE_URL}/api_application/webhooks").mock(side_effect=_capture)
    result = await connector.create_webhook(
        url="https://example.com/webhooks/signwell",
        events=["document_completed"],
    )
    assert result["id"] == "wh_001"
    assert captured["body"]["url"] == "https://example.com/webhooks/signwell"
    assert captured["body"]["events"] == ["document_completed"]


@pytest.mark.asyncio
async def test_create_webhook_requires_url(connector):
    with pytest.raises(ValueError, match="url is required"):
        await connector.create_webhook(url="")


@respx.mock
@pytest.mark.asyncio
async def test_delete_webhook_success(connector):
    respx.delete(f"{BASE_URL}/api_application/webhooks/wh_001").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_webhook("wh_001")
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/documents/doc_abc123").mock(
        side_effect=[
            httpx.Response(429, json={"message": "Too Many Requests"}),
            httpx.Response(200, json={"id": "doc_abc123", "status": "sent"}),
        ]
    )
    result = await connector.get_document("doc_abc123")
    assert route.call_count == 2
    assert result["id"] == "doc_abc123"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/me").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"id": "u1"}),
        ]
    )
    result = await connector.get_me()
    assert route.call_count == 2
    assert result == {"id": "u1"}


@respx.mock
@pytest.mark.asyncio
async def test_429_honours_retry_after(connector, no_retry_sleep):
    """When the 429 response carries Retry-After we surface it on the exception."""
    respx.get(f"{BASE_URL}/me").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "wait"}),
            httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "wait"}),
            httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "wait"}),
        ]
    )
    with pytest.raises(SignWellRateLimitError) as ei:
        await connector.http_client.get_me()
    assert ei.value.retry_after_s >= 0.5


# ═══════════════════════════════════════════════════════════════════════════
# Error classification (other status codes)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_400_raises_bad_request(connector):
    respx.post(f"{BASE_URL}/documents").mock(
        return_value=httpx.Response(400, json={"message": "Invalid body"})
    )
    with pytest.raises(SignWellBadRequestError):
        await connector.create_document(
            name="x",
            recipients=[{"name": "A", "email": "a@x.com"}],
            file_urls=["https://example.com/x.pdf"],
        )


@respx.mock
@pytest.mark.asyncio
async def test_409_raises_conflict(connector):
    respx.post(f"{BASE_URL}/documents/doc_abc123/cancel").mock(
        return_value=httpx.Response(
            409, json={"message": "Cannot cancel a completed document"}
        )
    )
    with pytest.raises(SignWellConflictError):
        await connector.cancel_document("doc_abc123")


@respx.mock
@pytest.mark.asyncio
async def test_timeout_raises_network_error(connector, no_retry_sleep):
    respx.get(f"{BASE_URL}/me").mock(side_effect=httpx.TimeoutException("slow"))
    with pytest.raises(SignWellNetworkError):
        await connector.http_client.get_me()


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — tenant-scoped id contract
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_document_uses_tenant_scoped_id():
    doc = normalize_document(SAMPLE_DOCUMENT, CONNECTOR_ID, TENANT_ID)
    assert isinstance(doc, NormalizedDocument)
    assert doc.id == f"{TENANT_ID}_{SAMPLE_DOCUMENT['id']}"
    assert doc.source_id == SAMPLE_DOCUMENT["id"]
    assert doc.title == SAMPLE_DOCUMENT["name"]
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.source == "signwell"
    assert doc.metadata["kind"] == "signwell.document"
    assert doc.metadata["recipients_count"] == 2


def test_normalize_document_handles_missing_fields():
    minimal = {"id": "doc_min"}
    doc = normalize_document(minimal, CONNECTOR_ID, TENANT_ID)
    assert doc.id == f"{TENANT_ID}_doc_min"
    assert doc.title == "Untitled document"
    assert doc.metadata["recipients_count"] == 0


def test_normalize_template_uses_tenant_scoped_id():
    doc = normalize_template(SAMPLE_TEMPLATE, CONNECTOR_ID, TENANT_ID)
    assert doc.id == f"{TENANT_ID}_{SAMPLE_TEMPLATE['id']}"
    assert doc.metadata["kind"] == "signwell.template"
    assert doc.metadata["fields_count"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════

def test_safe_get_walks_nested_dicts():
    d = {"a": {"b": {"c": 42}}}
    assert safe_get(d, "a", "b", "c") == 42
    assert safe_get(d, "a", "missing", default="x") == "x"
    assert safe_get(None, "a") is None


def test_validate_recipients_accepts_valid_payload():
    validate_recipients([{"name": "A", "email": "a@x.com"}])


def test_validate_recipients_rejects_empty_list():
    with pytest.raises(ValueError, match="non-empty"):
        validate_recipients([])


def test_validate_recipients_rejects_non_dict_entry():
    with pytest.raises(ValueError, match="must be a dict"):
        validate_recipients(["alice@example.com"])  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
# Sync — pages + ingest
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_sync_iterates_documents_and_ingests(connector):
    # Page 1 → one doc, has_more True; page 2 → empty -> stop
    respx.get(f"{BASE_URL}/documents").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"documents": [{"id": SAMPLE_DOCUMENT["id"]}], "has_more": True},
            ),
            httpx.Response(200, json={"documents": []}),
        ]
    )
    respx.get(f"{BASE_URL}/documents/{SAMPLE_DOCUMENT['id']}").mock(
        return_value=httpx.Response(200, json=SAMPLE_DOCUMENT)
    )
    result = await connector.sync()
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0
    connector.ingest_document.assert_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert SignWellConnector.CONNECTOR_TYPE == "signwell"


def test_auth_type_class_attr():
    assert SignWellConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(SignWellConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in SignWellConnector.REQUIRED_CONFIG_KEYS


def test_optional_config_keys_defined():
    assert hasattr(SignWellConnector, "OPTIONAL_CONFIG_KEYS")
    for k in ("test_mode_default", "base_url", "rate_limit_per_min"):
        assert k in SignWellConnector.OPTIONAL_CONFIG_KEYS


def test_status_map_defined():
    assert SignWellConnector._STATUS_MAP[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert SignWellConnector._STATUS_MAP[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert SignWellConnector._STATUS_MAP[429] == ("DEGRADED", "CONNECTED")


def test_backcompat_signwell_not_found_alias():
    # `SignWellNotFound` is preserved for older callers — must subclass the canonical one
    assert SignWellNotFound is SignWellNotFoundError


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = SignWellConnector(
        tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = SignWellConnector(
        tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    # Both clients share base_url but each has its own HTTP client instance
    assert c1.http_client is not c2.http_client
