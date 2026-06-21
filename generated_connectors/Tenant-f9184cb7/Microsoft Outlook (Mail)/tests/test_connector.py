"""respx-mocked unit tests for OutlookMailConnector — zero real network I/O."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from tests.conftest import SAMPLE_MESSAGE, TEST_CONFIG

GRAPH = TEST_CONFIG["base_url"]
TOKEN_URL = TEST_CONFIG["token_url"]


# ── install() ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING


@pytest.mark.asyncio
async def test_install_missing_credentials(connector):
    connector.client_id = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ── authorize() ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_authorize_exchanges_code_for_tokens(connector):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "Mail.Read Mail.Send offline_access",
            },
        )
    )
    token = await connector.authorize("auth-code-xyz")
    assert token.access_token == "new-access"
    assert token.refresh_token == "new-refresh"
    assert "Mail.Send" in token.scopes


# ── health_check() ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_health_check_ok(authed):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "u1", "mail": "me@example.com"})
    )
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_token_expired_after_failed_refresh(authed):
    # /me always returns 401; refresh endpoint also returns 401 → surfaces auth error.
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(401, json={"error": {"message": "invalid token"}})
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_grant"})
    )
    result = await authed.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ── list_messages with $filter / $search ──────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_list_messages_with_filter(authed):
    route = respx.get(f"{GRAPH}/me/mailFolders/inbox/messages").mock(
        return_value=httpx.Response(200, json={"value": [SAMPLE_MESSAGE]})
    )
    result = await authed.list_messages(folder="inbox", top=10,
                                        filter="isRead eq false")
    assert route.called
    sent_params = dict(route.calls.last.request.url.params)
    assert sent_params.get("$filter") == "isRead eq false"
    assert sent_params.get("$top") == "10"
    assert result["value"][0]["id"] == "AAMkAGI2"


@pytest.mark.asyncio
@respx.mock
async def test_list_messages_with_search(authed):
    route = respx.get(f"{GRAPH}/me/mailFolders/inbox/messages").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    await authed.list_messages(search="invoice")
    sent_params = dict(route.calls.last.request.url.params)
    # Graph requires quoted strings for $search.
    assert sent_params.get("$search") == '"invoice"'


# ── send_mail / reply_message / move_message ──────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_send_mail_builds_graph_payload(authed):
    route = respx.post(f"{GRAPH}/me/sendMail").mock(
        return_value=httpx.Response(202)
    )
    result = await authed.send_mail(
        to=["a@example.com"], subject="hi", body="<p>body</p>",
        cc=["c@example.com"],
    )
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["message"]["subject"] == "hi"
    assert body["message"]["body"]["contentType"] == "Html"
    assert body["message"]["toRecipients"][0]["emailAddress"]["address"] == "a@example.com"
    assert body["message"]["ccRecipients"][0]["emailAddress"]["address"] == "c@example.com"
    assert body["saveToSentItems"] is True
    # 202 Accepted → empty body → connector returns {}
    assert result == {}


@pytest.mark.asyncio
@respx.mock
async def test_reply_message(authed):
    route = respx.post(f"{GRAPH}/me/messages/AAMkAGI2/reply").mock(
        return_value=httpx.Response(202)
    )
    await authed.reply_message("AAMkAGI2", "thanks!")
    body = json.loads(route.calls.last.request.content)
    assert body == {"comment": "thanks!"}


@pytest.mark.asyncio
@respx.mock
async def test_move_message_targets_destination_folder(authed):
    route = respx.post(f"{GRAPH}/me/messages/AAMkAGI2/move").mock(
        return_value=httpx.Response(
            201, json={"id": "AAMkAGI2", "parentFolderId": "archive-id"},
        )
    )
    result = await authed.move_message("AAMkAGI2", "archive-id")
    body = json.loads(route.calls.last.request.content)
    assert body == {"destinationId": "archive-id"}
    assert result["parentFolderId"] == "archive-id"


# ── refresh-on-401 round trip ──────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_refresh_on_401_then_retries_with_new_token(authed):
    """First /me returns 401, refresh succeeds, second /me succeeds."""
    state = {"count": 0}

    def me_handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        # Second call must carry the new bearer token.
        assert request.headers["Authorization"] == "Bearer refreshed-access"
        return httpx.Response(200, json={"id": "u1"})

    respx.get(f"{GRAPH}/me").mock(side_effect=me_handler)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed-access",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "Mail.Read",
            },
        )
    )

    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert state["count"] == 2  # one 401 + one success


# ── retry-on-429 honours Retry-After ──────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_honours_retry_after(authed, monkeypatch):
    """First call 429 with Retry-After=0, second call succeeds."""
    # Skip the actual sleep so the test stays fast.
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"},
                                  json={"error": {"message": "throttled"}})
        return httpx.Response(200, json={"id": "u1"})

    respx.get(f"{GRAPH}/me").mock(side_effect=handler)

    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert state["count"] == 2
    # Either the in-client retry or the with_retry wrapper slept for ~1 s.
    assert any(abs(s - 1.0) < 0.01 for s in sleeps)


# ── delete_message / mark_as_read / search_messages ───────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_delete_message_returns_empty_on_204(authed):
    respx.delete(f"{GRAPH}/me/messages/AAMkAGI2").mock(
        return_value=httpx.Response(204)
    )
    result = await authed.delete_message("AAMkAGI2")
    assert result == {}


@pytest.mark.asyncio
@respx.mock
async def test_mark_as_read_patches_is_read_flag(authed):
    route = respx.patch(f"{GRAPH}/me/messages/AAMkAGI2").mock(
        return_value=httpx.Response(
            200, json={"id": "AAMkAGI2", "isRead": True},
        )
    )
    result = await authed.mark_as_read("AAMkAGI2", is_read=True)
    body = json.loads(route.calls.last.request.content)
    assert body == {"isRead": True}
    assert result["isRead"] is True


@pytest.mark.asyncio
@respx.mock
async def test_search_messages_quotes_query(authed):
    route = respx.get(f"{GRAPH}/me/messages").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    await authed.search_messages("invoice 2026")
    sent_params = dict(route.calls.last.request.url.params)
    assert sent_params.get("$search") == '"invoice 2026"'
