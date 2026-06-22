"""Respx-mocked unit tests for TelegramConnector — zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import TelegramConnector
from exceptions import (
    TelegramAuthError,
    TelegramBadRequestError,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRateLimitError,
    TelegramServerError,
)
from helpers.utils import RETRY_DELAY_S
from tests.conftest import (
    BOT_TOKEN,
    CONNECTOR_ID,
    SAMPLE_BOT,
    SAMPLE_MESSAGE,
    TELEGRAM_BASE,
    TENANT_ID,
    TEST_CONFIG,
    WEBHOOK_SECRET,
)

API_BASE = f"{TELEGRAM_BASE}/bot{BOT_TOKEN}"


def _ok(result):
    return {"ok": True, "result": result}


def _err(description: str, error_code: int = 400, parameters=None):
    body = {"ok": False, "description": description, "error_code": error_code}
    if parameters is not None:
        body["parameters"] = parameters
    return body


# ═══════════════════════════════════════════════════════════════════════════
# install() — config validation + getMe probe + optional webhook
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_install_success(connector):
    respx.get(f"{API_BASE}/getMe").mock(
        return_value=httpx.Response(200, json=_ok(SAMPLE_BOT)),
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_bot_token():
    c = TelegramConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"base_url": TELEGRAM_BASE},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_error(connector):
    respx.get(f"{API_BASE}/getMe").mock(
        return_value=httpx.Response(
            401, json=_err("Unauthorized", error_code=401),
        ),
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
@respx.mock
async def test_install_registers_webhook_when_url_set():
    cfg = dict(TEST_CONFIG)
    cfg["webhook_url"] = "https://shielva.example.com/hook"
    cfg["webhook_secret_token"] = WEBHOOK_SECRET
    c = TelegramConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg,
    )
    # stub side-effect methods so install() doesn't reach real storage.
    from unittest.mock import AsyncMock
    c.save_config = AsyncMock()

    respx.get(f"{API_BASE}/getMe").mock(
        return_value=httpx.Response(200, json=_ok(SAMPLE_BOT)),
    )
    set_webhook_route = respx.post(f"{API_BASE}/setWebhook").mock(
        return_value=httpx.Response(200, json=_ok(True)),
    )
    result = await c.install()
    assert set_webhook_route.called
    payload = _json.loads(set_webhook_route.calls.last.request.content)
    assert payload["url"] == "https://shielva.example.com/hook"
    assert payload["secret_token"] == WEBHOOK_SECRET
    assert result.health == ConnectorHealth.HEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Telegram URL convention: bot token in path, no Authorization header
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_bot_token_in_url_path_no_auth_header(connector):
    """Connector MUST embed bot_token in the URL path and send NO Authorization header."""
    route = respx.get(f"{API_BASE}/getMe").mock(
        return_value=httpx.Response(200, json=_ok(SAMPLE_BOT)),
    )
    await connector.get_me()
    assert route.called
    sent = route.calls.last.request
    # Token in path:
    assert f"/bot{BOT_TOKEN}/getMe" in str(sent.url)
    # No Authorization header.
    assert sent.headers.get("authorization") is None


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — no-op for api_key
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_api_key_token_info(connector):
    info = await connector.authorize("", "")
    assert info.access_token == BOT_TOKEN
    assert info.token_type == "api_key"
    assert info.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{API_BASE}/getMe").mock(
        return_value=httpx.Response(200, json=_ok(SAMPLE_BOT)),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error(connector, no_retry_sleep):
    respx.get(f"{API_BASE}/getMe").mock(
        return_value=httpx.Response(401, json=_err("Unauthorized", error_code=401)),
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# get_me() — envelope unwrap
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_me_unwraps_envelope(connector):
    respx.get(f"{API_BASE}/getMe").mock(
        return_value=httpx.Response(200, json=_ok(SAMPLE_BOT)),
    )
    result = await connector.get_me()
    assert "ok" not in result
    assert result["id"] == 7777
    assert result["username"] == "shielva_test_bot"


# ═══════════════════════════════════════════════════════════════════════════
# send_message() — envelope unwrap + body shape
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_send_message_unwraps_envelope(connector):
    route = respx.post(f"{API_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json=_ok(SAMPLE_MESSAGE)),
    )
    result = await connector.send_message(chat_id=-100200300, text="Hi")
    assert route.called
    sent = route.calls.last.request
    payload = _json.loads(sent.content)
    assert payload["chat_id"] == -100200300
    assert payload["text"] == "Hi"
    assert payload["parse_mode"] == "HTML"
    assert result["message_id"] == 42
    assert "ok" not in result


# ═══════════════════════════════════════════════════════════════════════════
# edit_message() + delete_message() + send_photo() + send_document()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_edit_message_success(connector):
    edited = {**SAMPLE_MESSAGE, "text": "Edited"}
    respx.post(f"{API_BASE}/editMessageText").mock(
        return_value=httpx.Response(200, json=_ok(edited)),
    )
    result = await connector.edit_message(
        chat_id=-100200300, message_id=42, text="Edited",
    )
    assert result["text"] == "Edited"


@pytest.mark.asyncio
@respx.mock
async def test_delete_message_returns_bool(connector):
    respx.post(f"{API_BASE}/deleteMessage").mock(
        return_value=httpx.Response(200, json=_ok(True)),
    )
    result = await connector.delete_message(chat_id=-100200300, message_id=42)
    assert result is True


@pytest.mark.asyncio
@respx.mock
async def test_send_photo_success(connector):
    route = respx.post(f"{API_BASE}/sendPhoto").mock(
        return_value=httpx.Response(200, json=_ok(SAMPLE_MESSAGE)),
    )
    result = await connector.send_photo(
        chat_id=-100200300,
        photo_url="https://example.com/cat.jpg",
        caption="Look at this cat",
    )
    assert route.called
    payload = _json.loads(route.calls.last.request.content)
    assert payload["photo"] == "https://example.com/cat.jpg"
    assert payload["caption"] == "Look at this cat"
    assert result["message_id"] == 42


@pytest.mark.asyncio
@respx.mock
async def test_send_document_success(connector):
    route = respx.post(f"{API_BASE}/sendDocument").mock(
        return_value=httpx.Response(200, json=_ok(SAMPLE_MESSAGE)),
    )
    result = await connector.send_document(
        chat_id=-100200300,
        document_url="https://example.com/report.pdf",
        caption="quarterly",
    )
    payload = _json.loads(route.calls.last.request.content)
    assert payload["document"] == "https://example.com/report.pdf"
    assert payload["caption"] == "quarterly"
    assert result["message_id"] == 42


# ═══════════════════════════════════════════════════════════════════════════
# forward_message()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_forward_message_success(connector):
    route = respx.post(f"{API_BASE}/forwardMessage").mock(
        return_value=httpx.Response(200, json=_ok(SAMPLE_MESSAGE)),
    )
    result = await connector.forward_message(
        chat_id=999, from_chat_id=-100200300, message_id=42,
    )
    payload = _json.loads(route.calls.last.request.content)
    assert payload["chat_id"] == 999
    assert payload["from_chat_id"] == -100200300
    assert payload["message_id"] == 42
    assert result["message_id"] == 42


# ═══════════════════════════════════════════════════════════════════════════
# get_updates() — offset propagation
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_updates_with_offset(connector):
    sample_update = {"update_id": 100, "message": SAMPLE_MESSAGE}
    route = respx.get(f"{API_BASE}/getUpdates").mock(
        return_value=httpx.Response(200, json=_ok([sample_update])),
    )
    result = await connector.get_updates(offset=99, limit=50, timeout=0)
    assert route.called
    req = route.calls.last.request
    assert req.url.params["offset"] == "99"
    assert req.url.params["limit"] == "50"
    assert req.url.params["timeout"] == "0"
    assert isinstance(result, list)
    assert result[0]["update_id"] == 100


# ═══════════════════════════════════════════════════════════════════════════
# Webhooks
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_set_webhook_success(connector):
    route = respx.post(f"{API_BASE}/setWebhook").mock(
        return_value=httpx.Response(200, json=_ok(True)),
    )
    result = await connector.set_webhook(
        url="https://shielva.example.com/hook",
        secret_token="s3cr3t",
        allowed_updates=["message", "callback_query"],
    )
    assert result is True
    payload = _json.loads(route.calls.last.request.content)
    assert payload["url"] == "https://shielva.example.com/hook"
    assert payload["secret_token"] == "s3cr3t"
    assert payload["allowed_updates"] == ["message", "callback_query"]


@pytest.mark.asyncio
@respx.mock
async def test_delete_webhook_success(connector):
    route = respx.post(f"{API_BASE}/deleteWebhook").mock(
        return_value=httpx.Response(200, json=_ok(True)),
    )
    result = await connector.delete_webhook(drop_pending_updates=True)
    assert result is True
    payload = _json.loads(route.calls.last.request.content)
    assert payload["drop_pending_updates"] is True


@pytest.mark.asyncio
@respx.mock
async def test_get_webhook_info_unwraps(connector):
    info = {
        "url": "https://shielva.example.com/hook",
        "has_custom_certificate": False,
        "pending_update_count": 0,
    }
    respx.get(f"{API_BASE}/getWebhookInfo").mock(
        return_value=httpx.Response(200, json=_ok(info)),
    )
    result = await connector.get_webhook_info()
    assert result["url"] == "https://shielva.example.com/hook"


# ═══════════════════════════════════════════════════════════════════════════
# Callback query + chat inspection + files
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_answer_callback_query_success(connector):
    route = respx.post(f"{API_BASE}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json=_ok(True)),
    )
    result = await connector.answer_callback_query(
        callback_query_id="cb-1", text="Got it", show_alert=False,
    )
    assert result is True
    payload = _json.loads(route.calls.last.request.content)
    assert payload["callback_query_id"] == "cb-1"
    assert payload["text"] == "Got it"
    assert payload["show_alert"] is False


@pytest.mark.asyncio
@respx.mock
async def test_get_chat_success(connector):
    chat = {"id": -100200300, "type": "supergroup", "title": "Room"}
    respx.get(f"{API_BASE}/getChat").mock(
        return_value=httpx.Response(200, json=_ok(chat)),
    )
    result = await connector.get_chat(chat_id=-100200300)
    assert result["id"] == -100200300


@pytest.mark.asyncio
@respx.mock
async def test_get_chat_member_success(connector):
    member = {"status": "member", "user": {"id": 1001, "first_name": "Alice"}}
    respx.get(f"{API_BASE}/getChatMember").mock(
        return_value=httpx.Response(200, json=_ok(member)),
    )
    result = await connector.get_chat_member(chat_id=-100200300, user_id=1001)
    assert result["status"] == "member"


@pytest.mark.asyncio
@respx.mock
async def test_get_chat_administrators_success(connector):
    admins = [{"status": "administrator", "user": {"id": 1, "first_name": "A"}}]
    respx.get(f"{API_BASE}/getChatAdministrators").mock(
        return_value=httpx.Response(200, json=_ok(admins)),
    )
    result = await connector.get_chat_administrators(chat_id=-100200300)
    assert isinstance(result, list)
    assert result[0]["status"] == "administrator"


@pytest.mark.asyncio
@respx.mock
async def test_get_file_returns_path(connector):
    file_obj = {
        "file_id": "AwACAg",
        "file_unique_id": "uniq",
        "file_size": 1234,
        "file_path": "documents/file_42.pdf",
    }
    respx.get(f"{API_BASE}/getFile").mock(
        return_value=httpx.Response(200, json=_ok(file_obj)),
    )
    result = await connector.get_file(file_id="AwACAg")
    assert result["file_path"] == "documents/file_42.pdf"
    # And the client exposes a builder for the actual download URL:
    download_url = connector.http_client.file_url(BOT_TOKEN, result["file_path"])
    assert download_url == (
        f"{TELEGRAM_BASE}/file/bot{BOT_TOKEN}/documents/file_42.pdf"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Error path: 401 / 403 / 404 / 400 / 409 / 5xx
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_send_message_auth_error_raises(connector, no_retry_sleep):
    respx.post(f"{API_BASE}/sendMessage").mock(
        return_value=httpx.Response(
            401, json=_err("Unauthorized", error_code=401),
        ),
    )
    with pytest.raises(TelegramAuthError):
        await connector.send_message(chat_id=1, text="x")


@pytest.mark.asyncio
@respx.mock
async def test_send_message_forbidden_raises(connector, no_retry_sleep):
    respx.post(f"{API_BASE}/sendMessage").mock(
        return_value=httpx.Response(
            403, json=_err("Forbidden: bot was blocked", error_code=403),
        ),
    )
    with pytest.raises(TelegramForbiddenError):
        await connector.send_message(chat_id=1, text="x")


@pytest.mark.asyncio
@respx.mock
async def test_send_message_chat_not_found(connector, no_retry_sleep):
    respx.post(f"{API_BASE}/sendMessage").mock(
        return_value=httpx.Response(
            404, json=_err("chat not found", error_code=404),
        ),
    )
    with pytest.raises(TelegramNotFound):
        await connector.send_message(chat_id=999, text="x")


@pytest.mark.asyncio
@respx.mock
async def test_send_message_bad_request_raises(connector, no_retry_sleep):
    respx.post(f"{API_BASE}/sendMessage").mock(
        return_value=httpx.Response(
            400, json=_err("Bad Request: text is empty", error_code=400),
        ),
    )
    with pytest.raises(TelegramBadRequestError):
        await connector.send_message(chat_id=1, text="")


@pytest.mark.asyncio
@respx.mock
async def test_get_updates_conflict_raises(connector, no_retry_sleep):
    """Telegram returns 409 if getUpdates is called while a webhook is set."""
    respx.get(f"{API_BASE}/getUpdates").mock(
        return_value=httpx.Response(
            409,
            json=_err(
                "Conflict: terminated by setWebhook",
                error_code=409,
            ),
        ),
    )
    with pytest.raises(TelegramConflictError):
        await connector.get_updates()


@pytest.mark.asyncio
@respx.mock
async def test_send_message_server_error_retries_then_raises(
    connector, no_retry_sleep,
):
    respx.post(f"{API_BASE}/sendMessage").mock(
        return_value=httpx.Response(
            500, json=_err("Internal", error_code=500),
        ),
    )
    with pytest.raises(TelegramServerError):
        await connector.send_message(chat_id=1, text="x")


# ═══════════════════════════════════════════════════════════════════════════
# Retry-on-429 honoring parameters.retry_after
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_send_message_retries_on_429(connector, mocker):
    """First call returns 429 with retry_after=0.01; second call succeeds."""
    route = respx.post(f"{API_BASE}/sendMessage").mock(
        side_effect=[
            httpx.Response(
                429,
                json=_err(
                    "Too Many Requests: retry later",
                    error_code=429,
                    parameters={"retry_after": 0.01},
                ),
            ),
            httpx.Response(200, json=_ok(SAMPLE_MESSAGE)),
        ]
    )
    sleep_mock = mocker.patch("helpers.utils.asyncio.sleep", new=mocker.AsyncMock())
    result = await connector.send_message(chat_id=-100200300, text="Hi")
    assert route.call_count == 2
    sleep_mock.assert_awaited()
    awaited_delays = [call.args[0] for call in sleep_mock.await_args_list]
    assert 0.01 in awaited_delays
    assert result["message_id"] == 42


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_error_exposes_retry_after(connector, mocker):
    respx.post(f"{API_BASE}/sendMessage").mock(
        return_value=httpx.Response(
            429,
            json=_err(
                "Too Many Requests",
                error_code=429,
                parameters={"retry_after": 5},
            ),
        ),
    )
    mocker.patch("helpers.utils.asyncio.sleep", new=mocker.AsyncMock())
    with pytest.raises(TelegramRateLimitError) as exc_info:
        await connector.send_message(chat_id=1, text="x")
    assert exc_info.value.retry_after == 5.0


# ═══════════════════════════════════════════════════════════════════════════
# sync() — drains updates, advances last_update_id checkpoint
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_sync_drains_updates_and_advances_checkpoint(connector, mocker):
    updates = [
        {"update_id": 100, "message": SAMPLE_MESSAGE},
        {"update_id": 101, "message": {**SAMPLE_MESSAGE, "message_id": 43}},
    ]
    respx.get(f"{API_BASE}/getUpdates").mock(
        return_value=httpx.Response(200, json=_ok(updates)),
    )
    set_metadata = mocker.patch.object(
        TelegramConnector, "set_metadata", new_callable=mocker.AsyncMock,
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0
    # Checkpoint advanced to the max update_id.
    set_metadata.assert_awaited_with("last_update_id", 101)


@pytest.mark.asyncio
@respx.mock
async def test_sync_no_updates_returns_completed(connector):
    respx.get(f"{API_BASE}/getUpdates").mock(
        return_value=httpx.Response(200, json=_ok([])),
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0


@pytest.mark.asyncio
async def test_sync_missing_token_fails():
    c = TelegramConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={},
    )
    result = await c.sync()
    assert result.status == SyncStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# Webhook routing: process_callback + handle_webhook + handle_event
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_process_callback_passes_when_no_secret_configured(connector):
    result = await connector.process_callback({"update_id": 1}, headers={})
    assert result["verified"] is True


@pytest.mark.asyncio
async def test_process_callback_verifies_secret_token():
    c = TelegramConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={**TEST_CONFIG, "webhook_secret_token": WEBHOOK_SECRET},
    )
    ok = await c.process_callback(
        {"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )
    assert ok["verified"] is True

    bad = await c.process_callback(
        {"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert bad["verified"] is False

    missing = await c.process_callback({"update_id": 1}, headers={})
    assert missing["verified"] is False


@pytest.mark.asyncio
async def test_handle_webhook_routes_message_update(connector):
    payload = {"update_id": 100, "message": SAMPLE_MESSAGE}
    result = await connector.handle_webhook(payload, headers={})
    assert result["status"] == "processed"
    assert result["kind"] == "message"
    assert result["update_id"] == 100
    assert result["result"]["processed"] is True


@pytest.mark.asyncio
async def test_handle_webhook_routes_callback_query(connector):
    payload = {
        "update_id": 101,
        "callback_query": {"id": "cb-99", "data": "btn-1"},
    }
    result = await connector.handle_webhook(payload, headers={})
    assert result["kind"] == "callback_query"
    assert result["result"]["processed"] is True


@pytest.mark.asyncio
async def test_handle_webhook_ignores_unsupported_payload(connector):
    result = await connector.handle_webhook(
        {"update_id": 102, "shipping_query": {"id": "sq-1"}}, headers={},
    )
    assert result["status"] == "ignored"


@pytest.mark.asyncio
async def test_handle_webhook_rejects_bad_secret():
    c = TelegramConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={**TEST_CONFIG, "webhook_secret_token": WEBHOOK_SECRET},
    )
    result = await c.handle_webhook(
        {"update_id": 1, "message": SAMPLE_MESSAGE},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert result["status"] == "ignored"


@pytest.mark.asyncio
async def test_handle_event_dispatches_unknown_kind(connector):
    result = await connector.handle_event(
        {"id": 1, "type": "shipping_query", "data": {}},
    )
    assert result["processed"] is False


@pytest.mark.asyncio
async def test_batch_processor_aggregates_per_item(connector):
    items = [
        {"update_id": 1, "message": SAMPLE_MESSAGE},
        {"update_id": 2, "callback_query": {"id": "cb-1"}},
    ]
    result = await connector.batch_processor(items)
    assert result["processed"] == 2
    assert result["failed"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type():
    assert TelegramConnector.CONNECTOR_TYPE == "telegram"


def test_auth_type():
    assert TelegramConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert TelegramConnector.REQUIRED_CONFIG_KEYS == ["bot_token"]


def test_status_map_defined():
    assert 401 in TelegramConnector._STATUS_MAP
    assert 403 in TelegramConnector._STATUS_MAP
    assert 429 in TelegramConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = TelegramConnector(
        tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG),
    )
    c2 = TelegramConnector(
        tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG),
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    # suppress unused-import warnings
    _ = (TENANT_ID, CONNECTOR_ID, RETRY_DELAY_S)


# ═══════════════════════════════════════════════════════════════════════════
# Normaliser produces tenant-scoped ids
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_message_scopes_id_by_connector_id():
    from helpers.normalizer import normalize_message

    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.id == f"{CONNECTOR_ID}_-100200300_42"
    assert doc.source == "telegram"
    assert doc.author == "@alice"
    assert doc.content == "Hello from Telegram"
    assert doc.metadata["kind"] == "telegram.message"
