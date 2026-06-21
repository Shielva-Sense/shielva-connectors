"""Unit tests for AdobeSignConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import AdobeSignConnector
from exceptions import (
    AdobeSignAuthError,
    AdobeSignBadRequestError,
    AdobeSignConflictError,
    AdobeSignError,
    AdobeSignNotFoundError,
    AdobeSignRateLimitError,
)

from tests.conftest import (
    API_BASE,
    CONNECTOR_ID,
    OAUTH_HOST,
    TENANT_ID,
    TEST_ACCESS_TOKEN,
    TEST_AGREEMENT_ID,
    TEST_CLIENT_ID,
    TEST_CLIENT_SECRET,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_client_id(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_client_secret(connector):
    connector.config.pop("client_secret", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# OAuth URL builder
# ═══════════════════════════════════════════════════════════════════════════

def test_get_oauth_url_contains_required_params(connector):
    url = connector.get_oauth_url("https://example.com/cb", state="xyz")
    assert url.startswith(f"{OAUTH_HOST}/public/oauth/v2?")
    assert "response_type=code" in url
    assert f"client_id={TEST_CLIENT_ID}" in url
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fcb" in url
    assert "state=xyz" in url


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — code exchange + shard discovery
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorize_exchanges_code_and_discovers_shard(connector):
    token_route = respx.post(f"{OAUTH_HOST}/oauth/v2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "scope": "user_read agreement_read",
                "token_type": "Bearer",
            },
        )
    )
    # /baseUris served on the default NA1 base; discovery pivots to EU1.
    base_uris_route = respx.get(f"{API_BASE}/baseUris").mock(
        return_value=httpx.Response(
            200,
            json={
                "apiAccessPoint": "https://api.eu1.adobesign.com/",
                "webAccessPoint": "https://secure.eu1.adobesign.com/",
            },
        )
    )
    token_info = await connector.authorize(
        auth_code="auth-code-123",
        state="https://example.com/cb",
    )
    assert token_route.called
    sent_token_body = dict(httpx.QueryParams(token_route.calls[0].request.content.decode()))
    assert sent_token_body["grant_type"] == "authorization_code"
    assert sent_token_body["code"] == "auth-code-123"
    assert sent_token_body["client_id"] == TEST_CLIENT_ID
    assert sent_token_body["client_secret"] == TEST_CLIENT_SECRET

    assert base_uris_route.called
    assert token_info.access_token == "new-access"
    assert token_info.refresh_token == "new-refresh"
    # Shard pivot
    assert connector.api_base_url == "https://api.eu1.adobesign.com/api/rest/v6"
    assert connector.http_client.base_url == "https://api.eu1.adobesign.com/api/rest/v6"


@respx.mock
@pytest.mark.asyncio
async def test_authorize_missing_code_raises(connector):
    with pytest.raises(AdobeSignAuthError):
        await connector.authorize(auth_code="")


@respx.mock
@pytest.mark.asyncio
async def test_authorize_token_endpoint_401_raises(connector):
    respx.post(f"{OAUTH_HOST}/oauth/v2/token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_grant"})
    )
    with pytest.raises(AdobeSignAuthError):
        await connector.authorize(auth_code="bad-code")


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer) + health_check
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer(connector):
    """Connector must send the access token as 'Bearer <token>'."""
    route = respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"id": "u-1", "email": "me@ex.com"})
    )
    await connector.health_check()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_ACCESS_TOKEN}"


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"id": "u-1"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(401, json={"code": "INVALID_ACCESS_TOKEN"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Agreements
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_create_agreement_posts_body(connector):
    route = respx.post(f"{API_BASE}/agreements").mock(
        return_value=httpx.Response(201, json={"id": TEST_AGREEMENT_ID})
    )
    payload = {
        "fileInfos": [{"libraryDocumentId": "lib-1"}],
        "participantSetsInfo": [
            {"memberInfos": [{"email": "signer@ex.com"}], "order": 1, "role": "SIGNER"}
        ],
        "name": "NDA",
        "signatureType": "ESIGN",
        "state": "IN_PROCESS",
    }
    result = await connector.create_agreement(payload)
    assert route.called
    sent_body = json.loads(route.calls[0].request.content.decode())
    assert sent_body == payload
    assert result["id"] == TEST_AGREEMENT_ID


@respx.mock
@pytest.mark.asyncio
async def test_get_agreement_success(connector):
    respx.get(f"{API_BASE}/agreements/{TEST_AGREEMENT_ID}").mock(
        return_value=httpx.Response(
            200, json={"id": TEST_AGREEMENT_ID, "status": "OUT_FOR_SIGNATURE"}
        )
    )
    result = await connector.get_agreement(TEST_AGREEMENT_ID)
    assert result["id"] == TEST_AGREEMENT_ID
    assert result["status"] == "OUT_FOR_SIGNATURE"


@respx.mock
@pytest.mark.asyncio
async def test_get_agreement_not_found(connector):
    respx.get(f"{API_BASE}/agreements/missing").mock(
        return_value=httpx.Response(404, json={"code": "RESOURCE_NOT_FOUND"})
    )
    with pytest.raises(AdobeSignNotFoundError):
        await connector.get_agreement("missing")


@respx.mock
@pytest.mark.asyncio
async def test_list_agreements_passes_pagination(connector):
    route = respx.get(f"{API_BASE}/agreements").mock(
        return_value=httpx.Response(
            200,
            json={"userAgreementList": [{"id": "a1", "name": "X"}]},
        )
    )
    result = await connector.list_agreements(page_size=25, cursor="cur-abc")
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("pageSize") == "25"
    assert qs.get("cursor") == "cur-abc"
    assert result["userAgreementList"][0]["id"] == "a1"


@respx.mock
@pytest.mark.asyncio
async def test_send_reminder_posts_body(connector):
    route = respx.post(
        f"{API_BASE}/agreements/{TEST_AGREEMENT_ID}/reminders"
    ).mock(return_value=httpx.Response(201, json={"id": "rem-1"}))
    await connector.send_reminder(
        TEST_AGREEMENT_ID,
        participant_emails=["pid-1", "pid-2"],
        note="Please sign",
    )
    sent_body = json.loads(route.calls[0].request.content.decode())
    assert sent_body["recipientParticipantIds"] == ["pid-1", "pid-2"]
    assert sent_body["status"] == "ACTIVE"
    assert sent_body["note"] == "Please sign"


@respx.mock
@pytest.mark.asyncio
async def test_cancel_agreement_puts_cancellation_state(connector):
    route = respx.put(f"{API_BASE}/agreements/{TEST_AGREEMENT_ID}/state").mock(
        return_value=httpx.Response(200, json={})
    )
    await connector.cancel_agreement(
        TEST_AGREEMENT_ID,
        comment="No longer needed",
        notify_signer=False,
    )
    sent_body = json.loads(route.calls[0].request.content.decode())
    assert sent_body["state"] == "CANCELLED"
    assert sent_body["agreementCancellationInfo"]["comment"] == "No longer needed"
    assert sent_body["agreementCancellationInfo"]["notifyOthers"] is False


@respx.mock
@pytest.mark.asyncio
async def test_download_agreement_returns_bytes(connector):
    pdf_bytes = b"%PDF-1.7 fake pdf content"
    respx.get(
        f"{API_BASE}/agreements/{TEST_AGREEMENT_ID}/combinedDocument"
    ).mock(
        return_value=httpx.Response(
            200,
            content=pdf_bytes,
            headers={"Content-Type": "application/pdf"},
        )
    )
    result = await connector.download_agreement(TEST_AGREEMENT_ID)
    assert result == pdf_bytes


# ═══════════════════════════════════════════════════════════════════════════
# Library documents / Users / Workflows / Webhooks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_library_documents(connector):
    respx.get(f"{API_BASE}/libraryDocuments").mock(
        return_value=httpx.Response(
            200, json={"libraryDocumentList": [{"id": "lib-1", "name": "Template"}]}
        )
    )
    result = await connector.list_library_documents()
    assert result["libraryDocumentList"][0]["id"] == "lib-1"


@respx.mock
@pytest.mark.asyncio
async def test_get_library_document(connector):
    respx.get(f"{API_BASE}/libraryDocuments/lib-1").mock(
        return_value=httpx.Response(200, json={"id": "lib-1", "name": "Template"})
    )
    result = await connector.get_library_document("lib-1")
    assert result["id"] == "lib-1"


@respx.mock
@pytest.mark.asyncio
async def test_list_users(connector):
    respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(
            200, json={"userInfoList": [{"id": "u-1", "email": "u@ex.com"}]}
        )
    )
    result = await connector.list_users()
    assert result["userInfoList"][0]["id"] == "u-1"


@respx.mock
@pytest.mark.asyncio
async def test_get_user(connector):
    respx.get(f"{API_BASE}/users/u-1").mock(
        return_value=httpx.Response(200, json={"id": "u-1", "email": "u@ex.com"})
    )
    result = await connector.get_user("u-1")
    assert result["id"] == "u-1"


@respx.mock
@pytest.mark.asyncio
async def test_list_workflows(connector):
    respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(
            200, json={"userWorkflowList": [{"id": "wf-1", "name": "Onboarding"}]}
        )
    )
    result = await connector.list_workflows()
    assert result["userWorkflowList"][0]["id"] == "wf-1"


@respx.mock
@pytest.mark.asyncio
async def test_list_webhooks(connector):
    respx.get(f"{API_BASE}/webhooks").mock(
        return_value=httpx.Response(
            200, json={"userWebhookList": [{"id": "wh-1"}]}
        )
    )
    result = await connector.list_webhooks()
    assert result["userWebhookList"][0]["id"] == "wh-1"


@respx.mock
@pytest.mark.asyncio
async def test_create_webhook_posts_body(connector):
    route = respx.post(f"{API_BASE}/webhooks").mock(
        return_value=httpx.Response(201, json={"id": "wh-new"})
    )
    payload = {
        "name": "MyHook",
        "scope": "ACCOUNT",
        "state": "ACTIVE",
        "webhookSubscriptionEvents": ["AGREEMENT_ALL"],
        "webhookUrlInfo": {"url": "https://example.com/hook"},
    }
    result = await connector.create_webhook(payload)
    sent_body = json.loads(route.calls[0].request.content.decode())
    assert sent_body == payload
    assert result["id"] == "wh-new"


@respx.mock
@pytest.mark.asyncio
async def test_get_base_uris(connector):
    respx.get(f"{API_BASE}/baseUris").mock(
        return_value=httpx.Response(
            200,
            json={
                "apiAccessPoint": "https://api.na1.adobesign.com/",
                "webAccessPoint": "https://secure.na1.adobesign.com/",
            },
        )
    )
    result = await connector.get_base_uris()
    assert result["apiAccessPoint"] == "https://api.na1.adobesign.com/"


# ═══════════════════════════════════════════════════════════════════════════
# Error classification
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_400_raises_bad_request(connector):
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(400, json={"code": "INVALID_PARAMETER"})
    )
    with pytest.raises(AdobeSignBadRequestError):
        await connector.http_client.get_me()


@respx.mock
@pytest.mark.asyncio
async def test_409_raises_conflict(connector):
    respx.put(f"{API_BASE}/agreements/{TEST_AGREEMENT_ID}/state").mock(
        return_value=httpx.Response(409, json={"code": "INVALID_AGREEMENT_STATE"})
    )
    with pytest.raises(AdobeSignConflictError):
        await connector.cancel_agreement(TEST_AGREEMENT_ID)


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{API_BASE}/users/me").mock(
        side_effect=[
            httpx.Response(429, json={"code": "RATE_LIMITED"}),
            httpx.Response(200, json={"id": "after-retry"}),
        ]
    )
    result = await connector.http_client.get_me()
    assert route.call_count == 2
    assert result["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{API_BASE}/agreements").mock(
        side_effect=[
            httpx.Response(500, json={"code": "INTERNAL"}),
            httpx.Response(200, json={"userAgreementList": []}),
        ]
    )
    result = await connector.list_agreements()
    assert route.call_count == 2
    assert result == {"userAgreementList": []}


@respx.mock
@pytest.mark.asyncio
async def test_429_exhausted_raises_rate_limit(connector, no_retry_sleep):
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(429, json={"code": "RATE_LIMITED"})
    )
    with pytest.raises(AdobeSignRateLimitError):
        await connector.http_client.get_me()


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert AdobeSignConnector.CONNECTOR_TYPE == "adobe_sign"


def test_auth_type_class_attr():
    assert AdobeSignConnector.AUTH_TYPE == "oauth2_code"


def test_required_config_keys_defined():
    assert hasattr(AdobeSignConnector, "REQUIRED_CONFIG_KEYS")
    assert "client_id" in AdobeSignConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in AdobeSignConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert 401 in AdobeSignConnector._STATUS_MAP
    assert 403 in AdobeSignConnector._STATUS_MAP
    assert 429 in AdobeSignConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = AdobeSignConnector(
        tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = AdobeSignConnector(
        tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — agreement payload → NormalizedDocument
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_agreement_id_format():
    from helpers.normalizer import normalize_agreement

    raw = {
        "id": "AG-123",
        "name": "NDA",
        "status": "OUT_FOR_SIGNATURE",
        "createdDate": "2026-06-21T10:00:00Z",
        "participantSetsInfo": [
            {"memberInfos": [{"email": "signer@ex.com"}], "role": "SIGNER"}
        ],
    }
    doc = normalize_agreement(raw, connector_id="conn-1", tenant_id="t-A")
    assert doc.id == "conn-1_AG-123"
    assert doc.source_id == "AG-123"
    assert doc.title == "NDA"
    assert "signer@ex.com" in doc.metadata["participants"]
    assert doc.metadata["kind"] == "adobe_sign.agreement"


def test_normalize_agreements_page_handles_userAgreementList():
    from helpers.normalizer import normalize_agreements_page

    page = {
        "userAgreementList": [
            {"id": "a1", "name": "A1", "status": "SIGNED"},
            {"id": "a2", "name": "A2", "status": "OUT_FOR_SIGNATURE"},
        ]
    }
    docs = normalize_agreements_page(page, connector_id="conn-1", tenant_id="t-A")
    assert len(docs) == 2
    assert docs[0].source_id == "a1"
    assert docs[1].source_id == "a2"


def test_normalize_library_document():
    from helpers.normalizer import normalize_library_document

    raw = {
        "id": "lib-9",
        "name": "Standard NDA",
        "scope": "ACCOUNT",
        "templateTypes": ["DOCUMENT"],
    }
    doc = normalize_library_document(raw, connector_id="conn-1", tenant_id="t-A")
    assert doc.id == "conn-1_lib-9"
    assert doc.title == "Standard NDA"
    assert doc.metadata["scope"] == "ACCOUNT"


# ═══════════════════════════════════════════════════════════════════════════
# Helpers — URL math
# ═══════════════════════════════════════════════════════════════════════════

def test_api_base_url_from_access_point_appends_v6_path():
    from helpers.utils import api_base_url_from_access_point

    assert (
        api_base_url_from_access_point("https://api.eu1.adobesign.com/")
        == "https://api.eu1.adobesign.com/api/rest/v6"
    )
    assert (
        api_base_url_from_access_point("https://api.na1.adobesign.com")
        == "https://api.na1.adobesign.com/api/rest/v6"
    )
    assert api_base_url_from_access_point("") == ""


def test_build_oauth_authorize_url_includes_all_params():
    from helpers.utils import build_oauth_authorize_url

    url = build_oauth_authorize_url(
        oauth_host="https://secure.na1.adobesign.com",
        client_id="cid",
        redirect_uri="https://cb.example.com",
        scopes="user_read agreement_read",
        state="STATE-1",
    )
    assert url.startswith("https://secure.na1.adobesign.com/public/oauth/v2?")
    assert "client_id=cid" in url
    assert "state=STATE-1" in url
