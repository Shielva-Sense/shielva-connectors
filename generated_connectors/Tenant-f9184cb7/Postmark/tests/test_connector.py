"""Unit tests for ``PostmarkConnector`` — respx-mocked, zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, NormalizedDocument

from connector import PostmarkConnector
from exceptions import (
    PostmarkAuthError,
    PostmarkBadRequestError,
    PostmarkInactiveRecipient,
    PostmarkNotFoundError,
    PostmarkRateLimitError,
)
from tests.conftest import (
    CONNECTOR_ID,
    POSTMARK_BASE,
    TENANT_ID,
    TEST_ACCOUNT_TOKEN,
    TEST_CONFIG,
    TEST_SERVER_TOKEN,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_install_success(connector):
    respx.get(f"{POSTMARK_BASE}/server").mock(
        return_value=httpx.Response(200, json={"ID": 1, "Name": "test"}),
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
@respx.mock
async def test_install_missing_server_token():
    c = PostmarkConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"base_url": POSTMARK_BASE},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_error_surfaces_missing_credentials(connector):
    """A 401 at install time must downgrade auth_status to MISSING_CREDENTIALS."""
    respx.get(f"{POSTMARK_BASE}/server").mock(
        return_value=httpx.Response(
            401, json={"ErrorCode": 10, "Message": "Invalid token"},
        )
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
@respx.mock
async def test_install_network_error_surfaces_degraded(connector):
    """Postmark unreachable at install → DEGRADED but installed."""
    respx.get(f"{POSTMARK_BASE}/server").mock(
        side_effect=httpx.ConnectError("dns broken"),
    )
    result = await connector.install()
    # Either DEGRADED or OFFLINE — both indicate "we know there is a network problem"
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape + auth-error path
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_uses_server_token(connector):
    """Server-scoped calls must send ``X-Postmark-Server-Token`` (not account)."""
    route = respx.get(f"{POSTMARK_BASE}/server").mock(
        return_value=httpx.Response(200, json={"ID": 1}),
    )
    await connector.get_server()
    assert route.called
    sent = route.calls.last.request
    assert sent.headers.get("X-Postmark-Server-Token") == TEST_SERVER_TOKEN
    assert "X-Postmark-Account-Token" not in sent.headers


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_postmark_auth_error(connector):
    respx.get(f"{POSTMARK_BASE}/server").mock(
        return_value=httpx.Response(401, json={"Message": "Invalid token"}),
    )
    with pytest.raises(PostmarkAuthError):
        await connector.get_server()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{POSTMARK_BASE}/server").mock(
        return_value=httpx.Response(200, json={"ID": 1, "Name": "test"}),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error(connector):
    respx.get(f"{POSTMARK_BASE}/server").mock(
        return_value=httpx.Response(
            401, json={"ErrorCode": 10, "Message": "Invalid token"},
        )
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_403_surfaces_invalid_credentials(connector):
    respx.get(f"{POSTMARK_BASE}/server").mock(
        return_value=httpx.Response(403, json={"Message": "Forbidden"}),
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — no-op api_key flow
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_token_info_with_api_key_type(connector):
    """``authorize()`` must surface a TokenInfo for ABI compatibility — not raise."""
    tok = await connector.authorize(auth_code="", state="")
    assert tok.token_type == "api_key"
    assert tok.access_token == TEST_SERVER_TOKEN


# ═══════════════════════════════════════════════════════════════════════════
# send_email()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_send_email_success(connector):
    route = respx.post(f"{POSTMARK_BASE}/email").mock(
        return_value=httpx.Response(
            200,
            json={
                "To": "user@example.com",
                "SubmittedAt": "2026-06-21T10:00:00Z",
                "MessageID": "abc-123",
                "ErrorCode": 0,
                "Message": "OK",
            },
        )
    )
    result = await connector.send_email(
        from_email="no-reply@example.com",
        to="user@example.com",
        subject="Hello",
        text_body="World",
    )
    assert result["MessageID"] == "abc-123"
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["X-Postmark-Server-Token"] == TEST_CONFIG["server_token"]
    body = _json.loads(sent.content.decode())
    assert body["From"] == "no-reply@example.com"
    assert body["To"] == "user@example.com"
    assert body["TextBody"] == "World"
    assert body["MessageStream"] == "outbound"


@pytest.mark.asyncio
async def test_send_email_requires_a_body(connector):
    with pytest.raises(ValueError, match="html_body or text_body"):
        await connector.send_email(
            from_email="no-reply@example.com",
            to="user@example.com",
            subject="Hello",
        )


@pytest.mark.asyncio
@respx.mock
async def test_send_email_inactive_recipient_typed(connector):
    """Postmark's 422 + ErrorCode 406 must surface as PostmarkInactiveRecipient."""
    respx.post(f"{POSTMARK_BASE}/email").mock(
        return_value=httpx.Response(
            422,
            json={
                "ErrorCode": 406,
                "Message": "You tried to send to recipient(s) that have been marked as inactive.",
            },
        )
    )
    with pytest.raises(PostmarkInactiveRecipient) as excinfo:
        await connector.send_email(
            from_email="no-reply@example.com",
            to="bounced@example.com",
            subject="Hello",
            text_body="World",
        )
    assert excinfo.value.response_body.get("ErrorCode") == 406


@pytest.mark.asyncio
@respx.mock
async def test_send_email_uses_default_from_when_omitted(connector):
    """If ``from_email`` is falsy the connector substitutes ``default_from_email``."""
    route = respx.post(f"{POSTMARK_BASE}/email").mock(
        return_value=httpx.Response(200, json={"MessageID": "m1"}),
    )
    await connector.send_email(
        from_email="",
        to="user@example.com",
        subject="Hi",
        text_body="hello",
    )
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["From"] == TEST_CONFIG["default_from_email"]


# ═══════════════════════════════════════════════════════════════════════════
# send_email_batch()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_send_email_batch_success(connector):
    route = respx.post(f"{POSTMARK_BASE}/email/batch").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"To": "a@x.com", "MessageID": "m1", "ErrorCode": 0, "Message": "OK"},
                {"To": "b@x.com", "MessageID": "m2", "ErrorCode": 0, "Message": "OK"},
            ],
        )
    )
    payload = [
        {"From": "no-reply@example.com", "To": "a@x.com", "Subject": "Hi", "TextBody": "yo"},
        {"From": "no-reply@example.com", "To": "b@x.com", "Subject": "Hi", "TextBody": "yo"},
    ]
    result = await connector.send_email_batch(payload)
    assert len(result) == 2
    assert result[0]["MessageID"] == "m1"
    # Verify the wire payload was the raw list (not wrapped).
    sent_body = _json.loads(route.calls.last.request.content.decode())
    assert sent_body == payload


# ═══════════════════════════════════════════════════════════════════════════
# send_email_with_template()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_send_email_with_template_by_alias(connector):
    route = respx.post(f"{POSTMARK_BASE}/email/withTemplate").mock(
        return_value=httpx.Response(
            200,
            json={
                "MessageID": "t1",
                "SubmittedAt": "2026-06-21T10:00:00Z",
                "ErrorCode": 0,
                "Message": "OK",
            },
        )
    )
    result = await connector.send_email_with_template(
        template_alias="welcome",
        from_email="no-reply@example.com",
        to="user@example.com",
        template_model={"name": "Vivek"},
    )
    assert result["MessageID"] == "t1"
    body = route.calls.last.request.content.decode()
    assert "TemplateAlias" in body and "welcome" in body


@pytest.mark.asyncio
@respx.mock
async def test_send_email_with_template_by_id(connector):
    route = respx.post(f"{POSTMARK_BASE}/email/withTemplate").mock(
        return_value=httpx.Response(200, json={"MessageID": "t2"}),
    )
    await connector.send_email_with_template(
        template_id=12345,
        from_email="no-reply@example.com",
        to="user@example.com",
        template_model={"x": 1},
    )
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["TemplateId"] == 12345
    assert "TemplateAlias" not in body


@pytest.mark.asyncio
async def test_send_email_with_template_requires_exactly_one_selector(connector):
    with pytest.raises(ValueError, match="template_id or template_alias"):
        await connector.send_email_with_template(
            template_id=1, template_alias="welcome", to="x@example.com",
        )
    with pytest.raises(ValueError, match="template_id or template_alias"):
        await connector.send_email_with_template(to="x@example.com")


# ═══════════════════════════════════════════════════════════════════════════
# list_messages() / get_message_details()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_messages_with_filters(connector):
    route = respx.get(f"{POSTMARK_BASE}/messages/outbound").mock(
        return_value=httpx.Response(
            200, json={"TotalCount": 1, "Messages": [{"MessageID": "m1"}]},
        )
    )
    result = await connector.list_messages(
        count=10, offset=0,
        recipient="user@example.com",
        tag="welcome", status="Sent",
    )
    assert result["TotalCount"] == 1
    req = route.calls.last.request
    assert req.url.params.get("count") == "10"
    assert req.url.params.get("recipient") == "user@example.com"
    assert req.url.params.get("tag") == "welcome"
    assert req.url.params.get("status") == "Sent"


@pytest.mark.asyncio
@respx.mock
async def test_get_message_details_success(connector):
    mid = "abc-123"
    respx.get(f"{POSTMARK_BASE}/messages/outbound/{mid}/details").mock(
        return_value=httpx.Response(
            200,
            json={
                "MessageID": mid,
                "Subject": "Welcome",
                "From": "no-reply@x.com",
                "To": [{"Email": "user@example.com"}],
                "SubmittedAt": "2026-06-21T09:30:00Z",
                "Status": "Sent",
                "MessageStream": "outbound",
            },
        )
    )
    result = await connector.get_message_details(mid)
    assert result["Subject"] == "Welcome"


@pytest.mark.asyncio
@respx.mock
async def test_get_message_returns_normalized_document(connector):
    mid = "abc-123"
    respx.get(f"{POSTMARK_BASE}/messages/outbound/{mid}/details").mock(
        return_value=httpx.Response(
            200,
            json={
                "MessageID": mid,
                "Subject": "Welcome",
                "From": "no-reply@x.com",
                "To": [{"Email": "user@example.com"}],
                "TextBody": "Hello there",
                "SubmittedAt": "2026-06-21T09:30:00Z",
            },
        )
    )
    doc = await connector.get_message(mid)
    assert isinstance(doc, NormalizedDocument)
    assert doc.source_id == mid
    # Tenant-scoped id — guards against cross-tenant ID collisions.
    assert doc.id == f"{TENANT_ID}_{mid}"
    assert doc.title == "Welcome"
    assert "Hello there" in doc.content


@pytest.mark.asyncio
@respx.mock
async def test_list_inbound_messages_with_filters(connector):
    route = respx.get(f"{POSTMARK_BASE}/messages/inbound").mock(
        return_value=httpx.Response(
            200, json={"TotalCount": 1, "InboundMessages": [{"MessageID": "i1"}]},
        )
    )
    result = await connector.list_inbound_messages(
        count=5, recipient="inbox@example.com", subject="ping",
    )
    assert result["TotalCount"] == 1
    params = route.calls.last.request.url.params
    assert params.get("recipient") == "inbox@example.com"
    assert params.get("subject") == "ping"


# ═══════════════════════════════════════════════════════════════════════════
# list_bounces / get_bounce / activate_bounce
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_bounces_with_inactive_filter(connector):
    route = respx.get(f"{POSTMARK_BASE}/bounces").mock(
        return_value=httpx.Response(
            200, json={"TotalCount": 1, "Bounces": [{"ID": 99, "Email": "x@y.com"}]},
        )
    )
    result = await connector.list_bounces(count=20, inactive=True, type="HardBounce")
    assert result["TotalCount"] == 1
    params = route.calls.last.request.url.params
    assert params.get("inactive") == "true"
    assert params.get("type") == "HardBounce"


@pytest.mark.asyncio
@respx.mock
async def test_get_bounce_404_raises_not_found(connector):
    respx.get(f"{POSTMARK_BASE}/bounces/99").mock(
        return_value=httpx.Response(404, json={"Message": "no bounce"}),
    )
    with pytest.raises(PostmarkNotFoundError):
        await connector.get_bounce(99)


@pytest.mark.asyncio
@respx.mock
async def test_activate_bounce(connector):
    respx.put(f"{POSTMARK_BASE}/bounces/99/activate").mock(
        return_value=httpx.Response(200, json={"Bounce": {"ID": 99, "Inactive": False}}),
    )
    result = await connector.activate_bounce(99)
    assert result["Bounce"]["Inactive"] is False


# ═══════════════════════════════════════════════════════════════════════════
# list_templates / get_template / create_template
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_templates(connector):
    respx.get(f"{POSTMARK_BASE}/templates").mock(
        return_value=httpx.Response(
            200,
            json={"TotalCount": 1, "Templates": [{"TemplateId": 7, "Alias": "welcome"}]},
        )
    )
    result = await connector.list_templates(count=50, offset=0)
    assert result["Templates"][0]["Alias"] == "welcome"


@pytest.mark.asyncio
@respx.mock
async def test_get_template(connector):
    respx.get(f"{POSTMARK_BASE}/templates/welcome").mock(
        return_value=httpx.Response(
            200, json={"TemplateId": 7, "Alias": "welcome", "Subject": "Hi"},
        )
    )
    result = await connector.get_template("welcome")
    assert result["Alias"] == "welcome"


@pytest.mark.asyncio
@respx.mock
async def test_create_template_posts_payload(connector):
    route = respx.post(f"{POSTMARK_BASE}/templates").mock(
        return_value=httpx.Response(200, json={"TemplateId": 42, "Alias": "thanks"}),
    )
    result = await connector.create_template(
        name="thanks",
        subject="Thanks!",
        html_body="<p>Thank you</p>",
        alias="thanks",
    )
    assert result["TemplateId"] == 42
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["Name"] == "thanks"
    assert body["Subject"] == "Thanks!"
    assert body["HtmlBody"] == "<p>Thank you</p>"
    assert body["Alias"] == "thanks"


@pytest.mark.asyncio
async def test_create_template_requires_body(connector):
    with pytest.raises(ValueError, match="html_body or text_body"):
        await connector.create_template(name="x", subject="x")


# ═══════════════════════════════════════════════════════════════════════════
# get_stats_overview
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_stats_overview_passes_filters(connector):
    route = respx.get(f"{POSTMARK_BASE}/stats/outbound").mock(
        return_value=httpx.Response(200, json={"Sent": 1234, "Bounced": 12}),
    )
    result = await connector.get_stats_overview(
        tag="welcome", from_date="2026-06-01", to_date="2026-06-21",
    )
    assert result["Sent"] == 1234
    params = route.calls.last.request.url.params
    assert params.get("tag") == "welcome"
    assert params.get("fromdate") == "2026-06-01"
    assert params.get("todate") == "2026-06-21"


# ═══════════════════════════════════════════════════════════════════════════
# Account-token-gated endpoints
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_servers_uses_account_token_header(connector):
    route = respx.get(f"{POSTMARK_BASE}/servers").mock(
        return_value=httpx.Response(200, json={"TotalCount": 0, "Servers": []}),
    )
    await connector.list_servers()
    headers = route.calls.last.request.headers
    assert headers["X-Postmark-Account-Token"] == TEST_ACCOUNT_TOKEN
    assert "X-Postmark-Server-Token" not in headers


@pytest.mark.asyncio
async def test_list_servers_without_account_token_raises():
    c = PostmarkConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"server_token": "s", "base_url": POSTMARK_BASE},
    )
    with pytest.raises(PostmarkAuthError):
        await c.list_servers()


@pytest.mark.asyncio
@respx.mock
async def test_list_domains_uses_account_token(connector):
    route = respx.get(f"{POSTMARK_BASE}/domains").mock(
        return_value=httpx.Response(200, json={"TotalCount": 0, "Domains": []}),
    )
    await connector.list_domains()
    headers = route.calls.last.request.headers
    assert headers["X-Postmark-Account-Token"] == TEST_ACCOUNT_TOKEN


@pytest.mark.asyncio
async def test_list_domains_without_account_token_raises():
    c = PostmarkConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"server_token": "s", "base_url": POSTMARK_BASE},
    )
    with pytest.raises(PostmarkAuthError):
        await c.list_domains()


# ═══════════════════════════════════════════════════════════════════════════
# Retry behaviour — 429 + 5xx
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_succeed(connector):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{POSTMARK_BASE}/server").mock(
        side_effect=[
            httpx.Response(429, json={"ErrorCode": 0, "Message": "Rate limit"}),
            httpx.Response(200, json={"ID": 1, "Name": "test"}),
        ]
    )
    result = await connector.get_server()
    assert result["ID"] == 1
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500_then_succeed(connector):
    route = respx.get(f"{POSTMARK_BASE}/server").mock(
        side_effect=[
            httpx.Response(500, json={"Message": "boom"}),
            httpx.Response(200, json={"ID": 1}),
        ]
    )
    result = await connector.get_server()
    assert result["ID"] == 1
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_429_with_retry_after_eventually_surfaces(connector):
    """If 429 persists, the typed RateLimit exception is raised after retries."""
    respx.get(f"{POSTMARK_BASE}/server").mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "1"}, json={"Message": "slow down"},
        )
    )
    with pytest.raises(PostmarkRateLimitError):
        await connector.get_server()


@pytest.mark.asyncio
@respx.mock
async def test_422_bad_request_typed(connector):
    """422 with a non-special ErrorCode → PostmarkBadRequestError."""
    respx.post(f"{POSTMARK_BASE}/email").mock(
        return_value=httpx.Response(
            422, json={"ErrorCode": 300, "Message": "Invalid 'To'"},
        )
    )
    with pytest.raises(PostmarkBadRequestError):
        await connector.send_email(
            from_email="x@y.com", to="bad", subject="x", text_body="x",
        )


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_sync_walks_outbound_and_normalizes(connector):
    """sync() must list outbound messages, fetch details for each, and ingest."""
    respx.get(f"{POSTMARK_BASE}/messages/outbound").mock(
        return_value=httpx.Response(
            200,
            json={"TotalCount": 2, "Messages": [{"MessageID": "m1"}, {"MessageID": "m2"}]},
        )
    )
    respx.get(f"{POSTMARK_BASE}/messages/outbound/m1/details").mock(
        return_value=httpx.Response(
            200,
            json={"MessageID": "m1", "Subject": "one", "TextBody": "1"},
        )
    )
    respx.get(f"{POSTMARK_BASE}/messages/outbound/m2/details").mock(
        return_value=httpx.Response(
            200,
            json={"MessageID": "m2", "Subject": "two", "TextBody": "2"},
        )
    )
    result = await connector.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_without_server_token_fails_fast():
    c = PostmarkConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID,
        config={"base_url": POSTMARK_BASE},
    )
    result = await c.sync()
    assert result.documents_synced == 0
    assert "server_token" in (result.message or "")


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity / multi-tenant
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type():
    assert PostmarkConnector.CONNECTOR_TYPE == "postmark"


def test_auth_type():
    assert PostmarkConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    """Only server_token is required at install time."""
    assert PostmarkConnector.REQUIRED_CONFIG_KEYS == ["server_token"]


def test_status_map_class_attr():
    """OCP — health-classification table lives on the class."""
    assert 401 in PostmarkConnector._STATUS_MAP
    assert 403 in PostmarkConnector._STATUS_MAP
    assert 429 in PostmarkConnector._STATUS_MAP


def test_different_tenants_are_isolated():
    a = PostmarkConnector(tenant_id="tA", connector_id="c1", config=dict(TEST_CONFIG))
    b = PostmarkConnector(tenant_id="tB", connector_id="c2", config=dict(TEST_CONFIG))
    assert a.tenant_id != b.tenant_id
    assert a.connector_id != b.connector_id
