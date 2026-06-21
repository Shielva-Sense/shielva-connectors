"""Unit tests for DropboxSignConnector — respx-mocked, zero real I/O."""
import base64

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import DropboxSignConnector
from exceptions import (
    DropboxSignAuthError,
    DropboxSignBadRequestError,
    DropboxSignError,
    DropboxSignNotFoundError,
    DropboxSignRateLimitError,
)

from tests.conftest import (
    CONNECTOR_ID,
    DBS_BASE,
    SAMPLE_ACCOUNT,
    SAMPLE_SIGNATURE_REQUEST,
    SAMPLE_TEMPLATE_LIST,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CLIENT_ID,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert DropboxSignConnector.CONNECTOR_TYPE == "dropbox_sign"


def test_auth_type_class_attr():
    assert DropboxSignConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(DropboxSignConnector, "REQUIRED_CONFIG_KEYS")
    assert DropboxSignConnector.REQUIRED_CONFIG_KEYS == ["api_key"]


def test_status_map_classifies_known_codes():
    assert 401 in DropboxSignConnector._STATUS_MAP
    assert 403 in DropboxSignConnector._STATUS_MAP
    assert 429 in DropboxSignConnector._STATUS_MAP
    assert DropboxSignConnector._STATUS_MAP[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert DropboxSignConnector._STATUS_MAP[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert DropboxSignConnector._STATUS_MAP[429] == ("DEGRADED", "CONNECTED")


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_install_success(connector):
    respx.get(f"{DBS_BASE}/account").mock(
        return_value=httpx.Response(200, json=SAMPLE_ACCOUNT)
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    connector.api_key = ""
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@respx.mock
@pytest.mark.asyncio
async def test_install_auth_failure(connector):
    respx.get(f"{DBS_BASE}/account").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"error_msg": "Unauthorized", "error_name": "unauthorized"}},
        )
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.FAILED
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (HTTP Basic with api_key:empty)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_get_account_uses_http_basic_auth(connector):
    route = respx.get(f"{DBS_BASE}/account").mock(
        return_value=httpx.Response(200, json=SAMPLE_ACCOUNT)
    )
    await connector.get_account()
    sent_auth = route.calls.last.request.headers["Authorization"]
    assert sent_auth.startswith("Basic ")
    # base64("test-api-key-deadbeef:") — username + empty password.
    expected = "Basic " + base64.b64encode(
        f"{TEST_API_KEY}:".encode("utf-8")
    ).decode("ascii")
    assert sent_auth == expected


@respx.mock
@pytest.mark.asyncio
async def test_401_raises_auth_error(connector):
    respx.get(f"{DBS_BASE}/account").mock(
        return_value=httpx.Response(401, json={"error": {"error_msg": "bad key"}})
    )
    with pytest.raises(DropboxSignAuthError):
        await connector.get_account()


@respx.mock
@pytest.mark.asyncio
async def test_403_raises_auth_error(connector):
    respx.get(f"{DBS_BASE}/account").mock(
        return_value=httpx.Response(403, json={"error": {"error_msg": "forbidden"}})
    )
    with pytest.raises(DropboxSignAuthError):
        await connector.get_account()


@respx.mock
@pytest.mark.asyncio
async def test_400_raises_bad_request(connector):
    respx.get(f"{DBS_BASE}/signature_request/list").mock(
        return_value=httpx.Response(400, json={"error": {"error_msg": "bad params"}})
    )
    with pytest.raises(DropboxSignBadRequestError):
        await connector.list_signature_requests()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{DBS_BASE}/account").mock(
        return_value=httpx.Response(200, json=SAMPLE_ACCOUNT)
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{DBS_BASE}/account").mock(
        return_value=httpx.Response(401, json={"error": {"error_msg": "bad key"}})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — api_key surface returns the key in a TokenInfo
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_api_key_token(connector):
    token = await connector.authorize()
    assert token.access_token == TEST_API_KEY
    assert token.token_type == "api_key"
    assert token.refresh_token is None
    assert token.expires_at is None


# ═══════════════════════════════════════════════════════════════════════════
# get_account()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_get_account_returns_payload(connector):
    respx.get(f"{DBS_BASE}/account").mock(
        return_value=httpx.Response(200, json=SAMPLE_ACCOUNT)
    )
    payload = await connector.get_account()
    assert payload["account"]["account_id"] == "acc-1"


# ═══════════════════════════════════════════════════════════════════════════
# Signature requests — list / get / pagination
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_signature_requests_default_pagination(connector):
    payload = {
        "signature_requests": [SAMPLE_SIGNATURE_REQUEST["signature_request"]],
        "list_info": {"page": 1, "num_pages": 1, "num_results": 1, "page_size": 20},
    }
    route = respx.get(f"{DBS_BASE}/signature_request/list").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_signature_requests()
    assert len(result["signature_requests"]) == 1
    sent_params = dict(route.calls.last.request.url.params)
    assert sent_params["page"] == "1"
    assert sent_params["page_size"] == "20"


@respx.mock
@pytest.mark.asyncio
async def test_list_signature_requests_pagination_params(connector):
    route = respx.get(f"{DBS_BASE}/signature_request/list").mock(
        return_value=httpx.Response(200, json={"signature_requests": []})
    )
    await connector.list_signature_requests(page=3, page_size=50, query="status:pending")
    sent_params = dict(route.calls.last.request.url.params)
    assert sent_params["page"] == "3"
    assert sent_params["page_size"] == "50"
    assert sent_params["query"] == "status:pending"


@respx.mock
@pytest.mark.asyncio
async def test_get_signature_request_success(connector):
    respx.get(f"{DBS_BASE}/signature_request/sigreq-123").mock(
        return_value=httpx.Response(200, json=SAMPLE_SIGNATURE_REQUEST)
    )
    payload = await connector.get_signature_request("sigreq-123")
    assert payload["signature_request"]["signature_request_id"] == "sigreq-123"


@respx.mock
@pytest.mark.asyncio
async def test_get_signature_request_not_found(connector):
    respx.get(f"{DBS_BASE}/signature_request/missing").mock(
        return_value=httpx.Response(
            404, json={"error": {"error_msg": "Not found", "error_name": "not_found"}}
        )
    )
    with pytest.raises(DropboxSignNotFoundError):
        await connector.get_signature_request("missing")


@pytest.mark.asyncio
async def test_get_signature_request_requires_id(connector):
    with pytest.raises(ValueError):
        await connector.get_signature_request("")


# ═══════════════════════════════════════════════════════════════════════════
# send_signature_request() — form-encoded bracket notation
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_send_signature_request_with_file_urls(connector):
    route = respx.post(f"{DBS_BASE}/signature_request/send").mock(
        return_value=httpx.Response(200, json=SAMPLE_SIGNATURE_REQUEST)
    )
    signers = [
        {"name": "Alice", "email_address": "alice@example.com"},
        {"name": "Bob", "email_address": "bob@example.com"},
    ]
    result = await connector.send_signature_request(
        title="NDA",
        subject="Please sign",
        message="Thanks.",
        signers=signers,
        file_urls=["https://example.com/nda.pdf"],
        test_mode=True,
    )
    assert result["signature_request"]["signature_request_id"] == "sigreq-123"
    body = route.calls.last.request.content.decode()
    # Form-encoded bracket notation for signers
    assert "signers%5B0%5D%5Bemail_address%5D=alice%40example.com" in body
    assert "signers%5B1%5D%5Bname%5D=Bob" in body
    assert "file_url%5B0%5D=https%3A%2F%2Fexample.com%2Fnda.pdf" in body
    assert "test_mode=1" in body


@respx.mock
@pytest.mark.asyncio
async def test_send_signature_request_with_files_multipart(connector):
    route = respx.post(f"{DBS_BASE}/signature_request/send").mock(
        return_value=httpx.Response(200, json=SAMPLE_SIGNATURE_REQUEST)
    )
    signers = [{"name": "Alice", "email_address": "alice@example.com"}]
    pdf_bytes = b"%PDF-1.4 fake-pdf"
    await connector.send_signature_request(
        title="NDA",
        subject="Please sign",
        message="Thanks.",
        signers=signers,
        files=[("nda.pdf", pdf_bytes, "application/pdf")],
        test_mode=False,
    )
    sent_ct = route.calls.last.request.headers.get("content-type", "")
    assert sent_ct.startswith("multipart/form-data")


@pytest.mark.asyncio
async def test_send_signature_request_rejects_empty_signers(connector):
    with pytest.raises(ValueError, match="signers"):
        await connector.send_signature_request(
            title="t", subject="s", message="m",
            signers=[],
            file_urls=["https://example.com/f.pdf"],
        )


@pytest.mark.asyncio
async def test_send_signature_request_requires_files_or_urls(connector):
    with pytest.raises(ValueError, match="file_urls or files"):
        await connector.send_signature_request(
            title="t", subject="s", message="m",
            signers=[{"name": "A", "email_address": "a@b.com"}],
        )


@pytest.mark.asyncio
async def test_send_signature_request_uses_test_mode_default(connector):
    """When test_mode is omitted, the install-time default is used."""
    connector.test_mode_default = False
    with respx.mock:
        route = respx.post(f"{DBS_BASE}/signature_request/send").mock(
            return_value=httpx.Response(200, json=SAMPLE_SIGNATURE_REQUEST)
        )
        await connector.send_signature_request(
            title="t", subject="s", message="m",
            signers=[{"name": "A", "email_address": "a@b.com"}],
            file_urls=["https://example.com/f.pdf"],
        )
        body = route.calls.last.request.content.decode()
        assert "test_mode=0" in body


# ═══════════════════════════════════════════════════════════════════════════
# cancel / remind
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_cancel_signature_request(connector):
    respx.post(f"{DBS_BASE}/signature_request/cancel/sigreq-123").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    result = await connector.cancel_signature_request("sigreq-123")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_cancel_requires_id(connector):
    with pytest.raises(ValueError):
        await connector.cancel_signature_request("")


@respx.mock
@pytest.mark.asyncio
async def test_remind_signature_request(connector):
    route = respx.post(f"{DBS_BASE}/signature_request/remind/sigreq-123").mock(
        return_value=httpx.Response(200, json=SAMPLE_SIGNATURE_REQUEST)
    )
    await connector.remind_signature_request("sigreq-123", "alice@example.com")
    body = route.calls.last.request.content.decode()
    assert "email_address=alice%40example.com" in body


@pytest.mark.asyncio
async def test_remind_requires_email(connector):
    with pytest.raises(ValueError):
        await connector.remind_signature_request("sigreq-123", "")


@pytest.mark.asyncio
async def test_remind_requires_id(connector):
    with pytest.raises(ValueError):
        await connector.remind_signature_request("", "alice@example.com")


# ═══════════════════════════════════════════════════════════════════════════
# download_files
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_download_files_returns_bytes(connector):
    pdf_bytes = b"%PDF-1.4 fake-pdf-bytes"
    respx.get(f"{DBS_BASE}/signature_request/files/sigreq-123").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )
    payload = await connector.download_files("sigreq-123", file_type="pdf")
    assert payload == pdf_bytes


@respx.mock
@pytest.mark.asyncio
async def test_download_files_zip(connector):
    zip_bytes = b"PK\x03\x04fake-zip"
    route = respx.get(f"{DBS_BASE}/signature_request/files/sigreq-123").mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )
    payload = await connector.download_files("sigreq-123", file_type="zip")
    assert payload == zip_bytes
    assert route.calls.last.request.url.params["file_type"] == "zip"


@pytest.mark.asyncio
async def test_download_files_rejects_bad_file_type(connector):
    with pytest.raises(ValueError):
        await connector.download_files("sigreq-123", file_type="doc")


# Back-compat alias still works.
@respx.mock
@pytest.mark.asyncio
async def test_download_signature_request_alias(connector):
    pdf_bytes = b"%PDF-1.4 fake-pdf-bytes"
    respx.get(f"{DBS_BASE}/signature_request/files/sigreq-123").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )
    payload = await connector.download_signature_request("sigreq-123", file_type="pdf")
    assert payload == pdf_bytes


# ═══════════════════════════════════════════════════════════════════════════
# Templates
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_templates(connector):
    respx.get(f"{DBS_BASE}/template/list").mock(
        return_value=httpx.Response(200, json=SAMPLE_TEMPLATE_LIST)
    )
    result = await connector.list_templates()
    assert len(result["templates"]) == 1
    assert result["templates"][0]["template_id"] == "tpl-1"


@respx.mock
@pytest.mark.asyncio
async def test_get_template(connector):
    respx.get(f"{DBS_BASE}/template/tpl-1").mock(
        return_value=httpx.Response(200, json={"template": {"template_id": "tpl-1"}})
    )
    result = await connector.get_template("tpl-1")
    assert result["template"]["template_id"] == "tpl-1"


@pytest.mark.asyncio
async def test_get_template_requires_id(connector):
    with pytest.raises(ValueError):
        await connector.get_template("")


@respx.mock
@pytest.mark.asyncio
async def test_send_with_template(connector):
    route = respx.post(f"{DBS_BASE}/signature_request/send_with_template").mock(
        return_value=httpx.Response(200, json=SAMPLE_SIGNATURE_REQUEST)
    )
    signers = [{"role": "Client", "name": "Alice", "email_address": "alice@example.com"}]
    await connector.send_with_template(
        template_id="tpl-1",
        title="Contract",
        subject="Please sign",
        message="Thanks",
        signers=signers,
        test_mode=False,
    )
    body = route.calls.last.request.content.decode()
    assert "template_id=tpl-1" in body
    assert "test_mode=0" in body


@pytest.mark.asyncio
async def test_send_with_template_requires_template_id(connector):
    with pytest.raises(ValueError):
        await connector.send_with_template(
            template_id="",
            title="t",
            subject="s",
            message="m",
            signers=[{"name": "A", "email_address": "a@b.com", "role": "Client"}],
        )


# ═══════════════════════════════════════════════════════════════════════════
# Team / drafts / embedded / api app
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_team_members(connector):
    respx.get(f"{DBS_BASE}/team").mock(
        return_value=httpx.Response(200, json={"team": {"name": "Acme", "accounts": []}})
    )
    result = await connector.list_team_members()
    assert "team" in result


@respx.mock
@pytest.mark.asyncio
async def test_list_unclaimed_drafts(connector):
    respx.get(f"{DBS_BASE}/unclaimed_draft/list").mock(
        return_value=httpx.Response(200, json={"unclaimed_drafts": []})
    )
    result = await connector.list_unclaimed_drafts()
    assert result == {"unclaimed_drafts": []}


@respx.mock
@pytest.mark.asyncio
async def test_create_embedded_signature_request(connector):
    respx.post(f"{DBS_BASE}/signature_request/create_embedded").mock(
        return_value=httpx.Response(200, json=SAMPLE_SIGNATURE_REQUEST)
    )
    signers = [{"name": "Alice", "email_address": "alice@example.com"}]
    result = await connector.create_embedded_signature_request(
        client_id="api-app-1",
        title="Embedded NDA",
        signers=signers,
        file_urls=["https://example.com/nda.pdf"],
    )
    assert result["signature_request"]["signature_request_id"] == "sigreq-123"


@pytest.mark.asyncio
async def test_embedded_requires_client_id(connector):
    with pytest.raises(ValueError):
        await connector.create_embedded_signature_request(
            client_id="",
            title="t",
            signers=[{"name": "A", "email_address": "a@b.com"}],
            file_urls=["https://example.com/f.pdf"],
        )


@respx.mock
@pytest.mark.asyncio
async def test_create_api_app(connector):
    route = respx.post(f"{DBS_BASE}/api_app").mock(
        return_value=httpx.Response(
            200, json={"api_app": {"client_id": "new-app-id", "name": "Acme"}}
        )
    )
    result = await connector.create_api_app(
        name="Acme",
        domain="acme.example.com",
        callback_url="https://acme.example.com/hellosign",
    )
    assert result["api_app"]["client_id"] == "new-app-id"
    body = route.calls.last.request.content.decode()
    assert "name=Acme" in body
    # domain is sent as domains[0]
    assert "domains%5B0%5D=acme.example.com" in body


@pytest.mark.asyncio
async def test_create_api_app_requires_name_and_domain(connector):
    with pytest.raises(ValueError):
        await connector.create_api_app(name="", domain="acme.example.com")
    with pytest.raises(ValueError):
        await connector.create_api_app(name="Acme", domain="")


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 + 5xx — converge to success
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{DBS_BASE}/account").mock(
        side_effect=[
            httpx.Response(429, json={"error": {"error_msg": "Too Many Requests"}}),
            httpx.Response(200, json=SAMPLE_ACCOUNT),
        ]
    )
    payload = await connector.get_account()
    assert route.call_count == 2
    assert payload["account"]["account_id"] == "acc-1"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{DBS_BASE}/account").mock(
        side_effect=[
            httpx.Response(500, json={"error": {"error_msg": "boom"}}),
            httpx.Response(200, json=SAMPLE_ACCOUNT),
        ]
    )
    payload = await connector.get_account()
    assert route.call_count == 2
    assert payload["account"]["account_id"] == "acc-1"


@respx.mock
@pytest.mark.asyncio
async def test_rate_limit_exhausts_to_rate_limit_error(connector, no_retry_sleep):
    """A persistent 429 surfaces as DropboxSignRateLimitError after retries exhaust."""
    respx.get(f"{DBS_BASE}/account").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "2"},
            json={"error": {"error_msg": "Too Many Requests"}},
        )
    )
    with pytest.raises((DropboxSignRateLimitError, DropboxSignError)):
        await connector.get_account()


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    a = DropboxSignConnector(tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    b = DropboxSignConnector(tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert a.tenant_id != b.tenant_id
    assert a.connector_id != b.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — tenant-scoped NormalizedDocument id
# ═══════════════════════════════════════════════════════════════════════════


def test_normalizer_produces_tenant_scoped_id():
    from helpers.normalizer import normalize_signature_request

    doc = normalize_signature_request(
        SAMPLE_SIGNATURE_REQUEST,
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
    )
    assert doc.id == f"{TENANT_ID}_sigreq-123"
    assert doc.source_id == "sigreq-123"
    assert doc.title == "NDA"
    assert doc.metadata["kind"] == "dropbox_sign.signature_request"


def test_normalizer_template_tenant_scoped_id():
    from helpers.normalizer import normalize_template

    raw = {"template": {"template_id": "tpl-1", "title": "Sales Contract"}}
    doc = normalize_template(raw, tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert doc.id == f"{TENANT_ID}_tpl-1"
    assert doc.metadata["kind"] == "dropbox_sign.template"
