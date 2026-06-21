"""Unit tests for AnthropicConnector — HTTP client patched via autospec'd fixture, zero real I/O."""
from unittest.mock import AsyncMock

import pytest

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import AnthropicConnector
from exceptions import (
    AnthropicAuthError,
    AnthropicError,
    AnthropicNotFoundError,
    AnthropicRateLimitError,
    AnthropicServerError,
)

from tests.conftest import (
    ANTHROPIC_BASE,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_ANTHROPIC_VERSION,
    TEST_API_KEY,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_blank_api_key(connector):
    connector.config["api_key"] = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — api-key shim
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_api_key_tokeninfo(connector):
    token = await connector.authorize(auth_code="", state="")
    assert token.access_token == TEST_API_KEY
    assert token.refresh_token is None
    assert token.token_type == "api_key"


# ═══════════════════════════════════════════════════════════════════════════
# Construction wires through to AnthropicHTTPClient with correct headers
# ═══════════════════════════════════════════════════════════════════════════

def test_http_client_constructed_with_api_key_and_version(
    mock_AnthropicHTTPClient,
):
    AnthropicConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
    mock_AnthropicHTTPClient.assert_called_once()
    kwargs = mock_AnthropicHTTPClient.call_args.kwargs
    assert kwargs["api_key"] == TEST_API_KEY
    assert kwargs["anthropic_version"] == TEST_ANTHROPIC_VERSION
    assert kwargs["base_url"] == ANTHROPIC_BASE
    assert kwargs["rate_limit_per_min"] == 50


def test_default_base_url_when_omitted(mock_AnthropicHTTPClient):
    cfg = dict(TEST_CONFIG)
    cfg.pop("base_url", None)
    AnthropicConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    kwargs = mock_AnthropicHTTPClient.call_args.kwargs
    assert kwargs["base_url"] == "https://api.anthropic.com/v1"


def test_default_anthropic_version_when_omitted(mock_AnthropicHTTPClient):
    cfg = dict(TEST_CONFIG)
    cfg.pop("anthropic_version", None)
    AnthropicConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    kwargs = mock_AnthropicHTTPClient.call_args.kwargs
    assert kwargs["anthropic_version"] == "2023-06-01"


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    connector.http_client.list_models = AsyncMock(
        return_value={"data": [{"id": "claude-sonnet-4-5"}], "has_more": False}
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    connector.http_client.list_models.assert_called_once_with(limit=1)


@pytest.mark.asyncio
async def test_health_check_401_offline(connector):
    connector.http_client.list_models = AsyncMock(
        side_effect=AnthropicAuthError(
            "401 Unauthorized: bad key", status_code=401,
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@pytest.mark.asyncio
async def test_health_check_403_unhealthy(connector):
    connector.http_client.list_models = AsyncMock(
        side_effect=AnthropicAuthError(
            "403 Forbidden: org disabled", status_code=403,
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.UNHEALTHY
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_server_error_degraded(connector):
    connector.http_client.list_models = AsyncMock(
        side_effect=AnthropicServerError("500 Internal", status_code=500)
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# sync() — documented no-op for an inference API
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_is_documented_noop(connector):
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0
    assert "inference" in (result.message or "").lower()


# ═══════════════════════════════════════════════════════════════════════════
# Webhook overrides — Anthropic has no webhooks today
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_handle_webhook_noop(connector):
    result = await connector.handle_webhook({"x": "y"}, headers={})
    assert result["status"] == "ignored"


@pytest.mark.asyncio
async def test_process_callback_noop(connector):
    result = await connector.process_callback({}, headers={})
    assert result["status"] == "ignored"


@pytest.mark.asyncio
async def test_handle_event_noop(connector):
    result = await connector.handle_event({"type": "anything"})
    assert result["status"] == "ignored"


@pytest.mark.asyncio
async def test_batch_processor_noop(connector):
    result = await connector.batch_processor([{"a": 1}])
    assert result == {"processed": 0, "items": []}


# ═══════════════════════════════════════════════════════════════════════════
# Messages
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_message_delegates_to_http_client(connector):
    connector.http_client.create_message = AsyncMock(
        return_value={
            "id": "msg_01",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }
    )
    result = await connector.create_message(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
    )
    assert result["id"] == "msg_01"
    connector.http_client.create_message.assert_called_once()
    kwargs = connector.http_client.create_message.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-5"
    assert kwargs["max_tokens"] == 10


@pytest.mark.asyncio
async def test_create_message_propagates_system_and_temperature(connector):
    connector.http_client.create_message = AsyncMock(return_value={"id": "m"})
    await connector.create_message(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=5,
        system="be terse",
        temperature=0.3,
    )
    kwargs = connector.http_client.create_message.call_args.kwargs
    assert kwargs["system"] == "be terse"
    assert kwargs["temperature"] == 0.3


@pytest.mark.asyncio
async def test_count_tokens_delegates(connector):
    connector.http_client.count_tokens = AsyncMock(
        return_value={"input_tokens": 12},
    )
    result = await connector.count_tokens(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hello there"}],
    )
    assert result["input_tokens"] == 12
    connector.http_client.count_tokens.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_models_delegates_with_limit(connector):
    connector.http_client.list_models = AsyncMock(
        return_value={
            "data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-haiku-4-5"}],
            "has_more": False,
        }
    )
    result = await connector.list_models(limit=5)
    assert len(result["data"]) == 2
    connector.http_client.list_models.assert_called_once_with(
        limit=5, before_id=None, after_id=None,
    )


@pytest.mark.asyncio
async def test_get_model_delegates(connector):
    connector.http_client.get_model = AsyncMock(
        return_value={"id": "claude-sonnet-4-5", "display_name": "Sonnet 4.5"},
    )
    result = await connector.get_model("claude-sonnet-4-5")
    assert result["id"] == "claude-sonnet-4-5"
    connector.http_client.get_model.assert_called_once_with("claude-sonnet-4-5")


@pytest.mark.asyncio
async def test_get_model_404_raises_not_found(connector):
    connector.http_client.get_model = AsyncMock(
        side_effect=AnthropicNotFoundError("404 model not found", status_code=404),
    )
    with pytest.raises(AnthropicNotFoundError):
        await connector.get_model("bogus")


# ═══════════════════════════════════════════════════════════════════════════
# Message Batches
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_batch_delegates(connector):
    connector.http_client.create_batch = AsyncMock(
        return_value={"id": "batch_01", "processing_status": "in_progress"},
    )
    reqs = [
        {"custom_id": "a", "params": {"model": "claude-haiku-4-5", "messages": [], "max_tokens": 1}},
    ]
    result = await connector.create_batch(reqs)
    assert result["id"] == "batch_01"
    connector.http_client.create_batch.assert_called_once_with(reqs)


@pytest.mark.asyncio
async def test_get_batch_delegates(connector):
    connector.http_client.get_batch = AsyncMock(
        return_value={"id": "batch_01", "processing_status": "ended"},
    )
    result = await connector.get_batch("batch_01")
    assert result["processing_status"] == "ended"


@pytest.mark.asyncio
async def test_list_batches_delegates(connector):
    connector.http_client.list_batches = AsyncMock(
        return_value={"data": [{"id": "batch_01"}], "has_more": False},
    )
    result = await connector.list_batches(limit=10)
    assert result["data"][0]["id"] == "batch_01"


@pytest.mark.asyncio
async def test_cancel_batch_delegates(connector):
    connector.http_client.cancel_batch = AsyncMock(
        return_value={"id": "batch_01", "processing_status": "canceling"},
    )
    result = await connector.cancel_batch("batch_01")
    assert result["processing_status"] == "canceling"


@pytest.mark.asyncio
async def test_get_batch_results_delegates(connector):
    connector.http_client.get_batch_results = AsyncMock(
        return_value={"raw": "{\"custom_id\":\"a\",\"result\":{}}\n"},
    )
    result = await connector.get_batch_results("batch_01")
    assert "raw" in result


# ═══════════════════════════════════════════════════════════════════════════
# Files (beta)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_files_delegates(connector):
    connector.http_client.list_files = AsyncMock(
        return_value={"data": [{"id": "file_01"}], "has_more": False},
    )
    result = await connector.list_files(limit=5)
    assert result["data"][0]["id"] == "file_01"
    connector.http_client.list_files.assert_called_once_with(
        limit=5, before_id=None, after_id=None,
    )


@pytest.mark.asyncio
async def test_get_file_delegates(connector):
    connector.http_client.get_file = AsyncMock(
        return_value={"id": "file_01", "filename": "x.txt"},
    )
    result = await connector.get_file("file_01")
    assert result["filename"] == "x.txt"


@pytest.mark.asyncio
async def test_delete_file_delegates(connector):
    connector.http_client.delete_file = AsyncMock(
        return_value={"id": "file_01", "deleted": True},
    )
    result = await connector.delete_file("file_01")
    assert result["deleted"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Error surfacing — auth + rate-limit propagate to caller
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_message_401_raises_auth_error(connector):
    connector.http_client.create_message = AsyncMock(
        side_effect=AnthropicAuthError(
            "401 Unauthorized: invalid key", status_code=401,
        )
    )
    with pytest.raises(AnthropicAuthError):
        await connector.create_message(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=1,
        )


@pytest.mark.asyncio
async def test_list_models_403_raises_auth_error(connector):
    connector.http_client.list_models = AsyncMock(
        side_effect=AnthropicAuthError("403 Forbidden", status_code=403),
    )
    with pytest.raises(AnthropicAuthError):
        await connector.list_models()


@pytest.mark.asyncio
async def test_retry_on_transient_then_success(connector, no_retry_sleep):
    """with_retry retries non-auth errors and converges on success."""
    call_log: list[int] = []

    async def flaky(*_args, **_kwargs):
        call_log.append(1)
        if len(call_log) < 2:
            raise AnthropicError("500 transient", status_code=500)
        return {"data": [{"id": "claude-sonnet-4-5"}]}

    connector.http_client.list_models = AsyncMock(side_effect=flaky)
    result = await connector.list_models()
    assert result["data"][0]["id"] == "claude-sonnet-4-5"
    assert len(call_log) == 2


@pytest.mark.asyncio
async def test_retry_does_not_swallow_auth_error(connector, no_retry_sleep):
    """Auth errors must NOT be retried — surface immediately."""
    call_log: list[int] = []

    async def always_401(*_args, **_kwargs):
        call_log.append(1)
        raise AnthropicAuthError("401 bad key", status_code=401)

    connector.http_client.create_message = AsyncMock(side_effect=always_401)
    with pytest.raises(AnthropicAuthError):
        await connector.create_message(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=1,
        )
    assert len(call_log) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert AnthropicConnector.CONNECTOR_TYPE == "anthropic"


def test_auth_type_class_attr():
    assert AnthropicConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(AnthropicConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in AnthropicConnector.REQUIRED_CONFIG_KEYS


def test_status_map_classifies_401_403_429():
    sm = AnthropicConnector._STATUS_MAP
    assert sm[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert sm[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert sm[429] == ("DEGRADED", "CONNECTED")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant(mock_AnthropicHTTPClient):
    c1 = AnthropicConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = AnthropicConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_tenant_id_propagates_to_instance(mock_AnthropicHTTPClient):
    c = AnthropicConnector(tenant_id="tenant-XYZ", connector_id="x", config=dict(TEST_CONFIG))
    assert c.tenant_id == "tenant-XYZ"
