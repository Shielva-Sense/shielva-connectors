"""OpenAI connector unit tests.

Conforms to TEST_SYSTEM_PROMPT:
- `from connector import OpenAIConnector` (rootdir-based, no package prefix)
- Patch target strings start with `connector.`
- httpx mock pattern: AsyncMock for request, MagicMock for response (.json sync)
- side_effect uses plain dicts, never AsyncMock wrappers
- No freezegun / factory_boy / hypothesis / faker (none installed)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from connector import OpenAIConnector
from exceptions import (
    OpenAIAuthError,
    OpenAIError,
    OpenAINetworkError,
    OpenAIRateLimitError,
)
from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus


# ─────────────────────────────────────────────────────────────────────
# install()
# ─────────────────────────────────────────────────────────────────────


class TestInstall:
    async def test_install_missing_credentials_returns_offline(self, empty_connector):
        status = await empty_connector.install()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert status.connector_id == "conn-1"

    async def test_install_with_creds_returns_healthy_without_api_call(
        self, connector, mock_OpenAIHTTPClient
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        status = await connector.install()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.AUTHENTICATED
        # CONNECTOR_SYSTEM_PROMPT rule: install() MUST NOT call the API.
        assert mock_instance.request.await_count == 0

    async def test_install_persists_config_via_save_config(self, connector):
        # save_config is mocked via the autouse fixture.
        await connector.install()
        assert connector.save_config.await_count == 1
        saved = connector.save_config.await_args.args[0]
        assert saved["api_key"] == "sk-test-openai-key"
        assert saved["organization_id"] == "org-test-123"


# ─────────────────────────────────────────────────────────────────────
# authorize()  — API-key connector returns a TokenInfo wrapper.
# ─────────────────────────────────────────────────────────────────────


class TestAuthorize:
    async def test_authorize_returns_api_key_token(self, connector):
        token = await connector.authorize(auth_code="", state="")
        assert token.access_token == "sk-test-openai-key"
        assert token.token_type == "api_key"
        assert token.refresh_token is None


# ─────────────────────────────────────────────────────────────────────
# health_check()
# ─────────────────────────────────────────────────────────────────────


class TestHealthCheck:
    async def test_missing_credentials(self, empty_connector):
        status = await empty_connector.health_check()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_healthy(self, connector, mock_OpenAIHTTPClient, response_factory):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"object": "list", "data": [{"id": "gpt-4o-mini"}]}
        )
        status = await connector.health_check()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.CONNECTED
        # Probes GET /models.
        method = mock_instance.request.call_args.args[0]
        url = mock_instance.request.call_args.args[1]
        assert method == "GET"
        assert "/models" in url

    async def test_health_check_401_maps_to_token_expired_offline(
        self, connector, mock_OpenAIHTTPClient
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.side_effect = OpenAIAuthError("bad key", status_code=401)
        status = await connector.health_check()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.TOKEN_EXPIRED

    async def test_health_check_403_maps_to_invalid_credentials_unhealthy(
        self, connector, mock_OpenAIHTTPClient
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        err = OpenAIError("forbidden", status_code=403)
        mock_instance.request.side_effect = err
        status = await connector.health_check()
        assert status.health == ConnectorHealth.UNHEALTHY
        assert status.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_429_maps_to_degraded(
        self, connector, mock_OpenAIHTTPClient
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.side_effect = OpenAIRateLimitError(
            "rate limited", status_code=429, retry_after_s=1.0
        )
        status = await connector.health_check()
        assert status.health == ConnectorHealth.DEGRADED
        assert status.auth_status == AuthStatus.CONNECTED

    async def test_health_check_network_error_maps_to_offline_connected(
        self, connector, mock_OpenAIHTTPClient
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.side_effect = OpenAINetworkError("dns broken")
        status = await connector.health_check()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.CONNECTED


# ─────────────────────────────────────────────────────────────────────
# Models surface
# ─────────────────────────────────────────────────────────────────────


class TestModels:
    async def test_list_models(self, connector, mock_OpenAIHTTPClient, response_factory):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"object": "list", "data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]}
        )
        result = await connector.list_models()
        assert result["object"] == "list"
        assert len(result["data"]) == 2
        method = mock_instance.request.call_args.args[0]
        url = mock_instance.request.call_args.args[1]
        assert method == "GET"
        assert url == "/models"

    async def test_get_model(self, connector, mock_OpenAIHTTPClient, response_factory):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"id": "gpt-4o-mini", "object": "model"}
        )
        result = await connector.get_model("gpt-4o-mini")
        assert result["id"] == "gpt-4o-mini"
        assert mock_instance.request.call_args.args[1] == "/models/gpt-4o-mini"

    async def test_get_model_empty_raises(self, connector):
        with pytest.raises(OpenAIError):
            await connector.get_model("")


# ─────────────────────────────────────────────────────────────────────
# Chat Completions surface
# ─────────────────────────────────────────────────────────────────────


class TestChatCompletion:
    async def test_create_chat_completion_normalises_response(
        self, connector, mock_OpenAIHTTPClient, response_factory
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={
                "id": "chatcmpl-1",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hello world"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
        )
        result = await connector.create_chat_completion(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert result["content"] == "hello world"
        assert result["finish_reason"] == "stop"
        assert result["usage"]["total_tokens"] == 7
        assert result["raw"]["id"] == "chatcmpl-1"
        # Verify request shape.
        method, url = mock_instance.request.call_args.args[:2]
        assert method == "POST"
        assert url == "/chat/completions"
        body = mock_instance.request.call_args.kwargs["json_body"]
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"][0]["content"] == "hi"

    async def test_create_chat_completion_requires_model(self, connector):
        with pytest.raises(OpenAIError):
            await connector.create_chat_completion(model="", messages=[{"role": "user", "content": "x"}])

    async def test_create_chat_completion_requires_messages(self, connector):
        with pytest.raises(OpenAIError):
            await connector.create_chat_completion(model="gpt-4o", messages=[])

    async def test_create_chat_completion_forwards_kwargs(
        self, connector, mock_OpenAIHTTPClient, response_factory
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"choices": [{"message": {"content": "ok"}}]}
        )
        await connector.create_chat_completion(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            top_p=0.9,
            stream=False,
        )
        body = mock_instance.request.call_args.kwargs["json_body"]
        assert body["top_p"] == 0.9
        assert body["stream"] is False


# ─────────────────────────────────────────────────────────────────────
# Embeddings surface
# ─────────────────────────────────────────────────────────────────────


class TestEmbeddings:
    async def test_create_embedding(self, connector, mock_OpenAIHTTPClient, response_factory):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={
                "object": "list",
                "data": [{"embedding": [0.1, 0.2, 0.3]}],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            }
        )
        result = await connector.create_embedding(
            model="text-embedding-3-small",
            input="hello",
        )
        assert result["data"][0]["embedding"] == [0.1, 0.2, 0.3]
        body = mock_instance.request.call_args.kwargs["json_body"]
        assert body == {"model": "text-embedding-3-small", "input": "hello"}

    async def test_create_embedding_with_dimensions(
        self, connector, mock_OpenAIHTTPClient, response_factory
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"data": []})
        await connector.create_embedding(model="text-embedding-3-small", input="hi", dimensions=256)
        body = mock_instance.request.call_args.kwargs["json_body"]
        assert body["dimensions"] == 256

    async def test_create_embedding_requires_model(self, connector):
        with pytest.raises(OpenAIError):
            await connector.create_embedding(model="", input="hi")

    async def test_create_embedding_requires_input(self, connector):
        with pytest.raises(OpenAIError):
            await connector.create_embedding(model="text-embedding-3-small", input="")


# ─────────────────────────────────────────────────────────────────────
# Files surface
# ─────────────────────────────────────────────────────────────────────


class TestFiles:
    async def test_list_files(self, connector, mock_OpenAIHTTPClient, response_factory):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"data": [{"id": "file-1", "filename": "a.jsonl"}]}
        )
        result = await connector.list_files()
        assert result["data"][0]["id"] == "file-1"
        method, url = mock_instance.request.call_args.args[:2]
        assert method == "GET"
        assert url == "/files"

    async def test_list_files_with_purpose_filter(
        self, connector, mock_OpenAIHTTPClient, response_factory
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"data": []})
        await connector.list_files(purpose="assistants")
        params = mock_instance.request.call_args.kwargs.get("params")
        assert params == {"purpose": "assistants"}

    async def test_upload_file(self, connector, mock_OpenAIHTTPClient, response_factory):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"id": "file-9", "filename": "data.jsonl", "purpose": "batch"}
        )
        result = await connector.upload_file(
            purpose="batch",
            file_name="data.jsonl",
            content=b'{"x":1}\n',
        )
        assert result["id"] == "file-9"
        files = mock_instance.request.call_args.kwargs["files"]
        data = mock_instance.request.call_args.kwargs["data"]
        assert files["file"][0] == "data.jsonl"
        assert data == {"purpose": "batch"}

    async def test_upload_file_rejects_invalid_purpose(self, connector):
        with pytest.raises(OpenAIError):
            await connector.upload_file(purpose="bogus", file_name="x.txt", content=b"x")

    async def test_delete_file_returns_payload_when_present(
        self, connector, mock_OpenAIHTTPClient, response_factory
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"id": "file-1", "deleted": True}
        )
        result = await connector.delete_file("file-1")
        assert result == {"id": "file-1", "deleted": True}
        method, url = mock_instance.request.call_args.args[:2]
        assert method == "DELETE"
        assert url == "/files/file-1"

    async def test_delete_file_empty_body_returns_placeholder(
        self, connector, mock_OpenAIHTTPClient, response_factory
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(content=b"")
        result = await connector.delete_file("file-1")
        assert result == {"id": "file-1", "deleted": True}

    async def test_delete_file_requires_id(self, connector):
        with pytest.raises(OpenAIError):
            await connector.delete_file("")


# ─────────────────────────────────────────────────────────────────────
# Images surface
# ─────────────────────────────────────────────────────────────────────


class TestImages:
    async def test_create_image(self, connector, mock_OpenAIHTTPClient, response_factory):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"created": 1, "data": [{"url": "https://x/img.png"}]}
        )
        result = await connector.create_image(prompt="a cat", size="1024x1024", n=1)
        assert result["data"][0]["url"].endswith(".png")
        body = mock_instance.request.call_args.kwargs["json_body"]
        assert body["prompt"] == "a cat"
        assert body["size"] == "1024x1024"
        assert body["n"] == 1
        assert body["model"] == "dall-e-3"

    async def test_create_image_requires_prompt(self, connector):
        with pytest.raises(OpenAIError):
            await connector.create_image(prompt="")


# ─────────────────────────────────────────────────────────────────────
# Speech surface
# ─────────────────────────────────────────────────────────────────────


class TestSpeech:
    async def test_create_speech_returns_bytes(
        self, connector, mock_OpenAIHTTPClient, response_factory
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(content=b"MP3-AUDIO")
        data = await connector.create_speech(model="tts-1", voice="alloy", input="hello")
        assert data == b"MP3-AUDIO"
        body = mock_instance.request.call_args.kwargs["json_body"]
        assert body == {
            "model": "tts-1",
            "voice": "alloy",
            "input": "hello",
            "response_format": "mp3",
        }
        method, url = mock_instance.request.call_args.args[:2]
        assert method == "POST"
        assert url == "/audio/speech"

    async def test_create_speech_requires_input(self, connector):
        with pytest.raises(OpenAIError):
            await connector.create_speech(model="tts-1", voice="alloy", input="")


# ─────────────────────────────────────────────────────────────────────
# Audio transcription surface
# ─────────────────────────────────────────────────────────────────────


class TestTranscription:
    async def test_create_transcription(
        self, connector, mock_OpenAIHTTPClient, response_factory
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"text": "hello world"}
        )
        result = await connector.create_transcription(
            file_name="clip.mp3",
            content=b"\x00\x01\x02",
            language="en",
        )
        assert result["text"] == "hello world"
        files = mock_instance.request.call_args.kwargs["files"]
        data = mock_instance.request.call_args.kwargs["data"]
        assert files["file"][0] == "clip.mp3"
        assert data["model"] == "whisper-1"
        assert data["language"] == "en"

    async def test_create_transcription_requires_file_name(self, connector):
        with pytest.raises(OpenAIError):
            await connector.create_transcription(file_name="", content=b"x")


# ─────────────────────────────────────────────────────────────────────
# Moderations surface
# ─────────────────────────────────────────────────────────────────────


class TestModeration:
    async def test_create_moderation(self, connector, mock_OpenAIHTTPClient, response_factory):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"results": [{"flagged": False}]}
        )
        result = await connector.create_moderation(input="say hi")
        assert result["results"][0]["flagged"] is False
        body = mock_instance.request.call_args.kwargs["json_body"]
        assert body == {"model": "text-moderation-latest", "input": "say hi"}

    async def test_create_moderation_requires_input(self, connector):
        with pytest.raises(OpenAIError):
            await connector.create_moderation(input="")


# ─────────────────────────────────────────────────────────────────────
# sync()
# ─────────────────────────────────────────────────────────────────────


class TestSync:
    async def test_sync_missing_credentials_fails(self, empty_connector):
        result = await empty_connector.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_no_files_returns_success_zero(
        self, connector, mock_OpenAIHTTPClient, response_factory
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"data": []})
        result = await connector.sync()
        assert result.status == SyncStatus.SUCCESS
        assert result.documents_synced == 0
        assert result.documents_found == 0

    async def test_sync_with_files_ingests_tenant_scoped_docs(
        self, connector, mock_OpenAIHTTPClient, response_factory
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={
                "data": [
                    {
                        "id": "file-a",
                        "filename": "a.jsonl",
                        "purpose": "batch",
                        "bytes": 100,
                        "status": "processed",
                        "created_at": 1717777777,
                    },
                    {
                        "id": "file-b",
                        "filename": "b.jsonl",
                        "purpose": "assistants",
                        "bytes": 50,
                    },
                ]
            }
        )
        connector.ingest_batch = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = await connector.sync()
        assert result.status == SyncStatus.SUCCESS
        assert result.documents_synced == 2
        ingested = connector.ingest_batch.await_args.args[0]
        # Multi-tenant scoping: every doc id starts with tenant_id_
        assert ingested[0].id.startswith("tenant-1_")
        assert ingested[0].tenant_id == "tenant-1"
        assert ingested[0].connector_id == "conn-1"

    async def test_sync_fetch_failure_returns_failed(
        self, connector, mock_OpenAIHTTPClient
    ):
        _, mock_instance = mock_OpenAIHTTPClient
        mock_instance.request.side_effect = OpenAIError("boom", status_code=500)
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "boom" in result.errors[0]


# ─────────────────────────────────────────────────────────────────────
# Webhook / event handlers
# ─────────────────────────────────────────────────────────────────────


class TestWebhooks:
    async def test_process_callback_no_secret_accepts_unverified(self, connector):
        result = await connector.process_callback(
            {"type": "response.completed"}, headers={}
        )
        assert result["verified"] is True
        assert result.get("unverified") is True

    async def test_process_callback_with_secret_valid(
        self, mock_OpenAIHTTPClient, connector_config
    ):
        import hashlib
        import hmac as _hmac
        import json as _json

        c = OpenAIConnector(
            tenant_id="tenant-1",
            connector_id="conn-1",
            config={**connector_config, "webhook_secret": "shh"},
        )
        payload = {"type": "response.completed", "id": "evt-1"}
        raw = _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = _hmac.new(b"shh", raw, hashlib.sha256).hexdigest()
        result = await c.process_callback(payload, headers={"OpenAI-Signature": sig})
        assert result["verified"] is True

    async def test_process_callback_with_secret_invalid(
        self, mock_OpenAIHTTPClient, connector_config
    ):
        c = OpenAIConnector(
            tenant_id="tenant-1",
            connector_id="conn-1",
            config={**connector_config, "webhook_secret": "shh"},
        )
        result = await c.process_callback(
            {"type": "response.completed"}, headers={"OpenAI-Signature": "deadbeef"}
        )
        assert result["verified"] is False
        assert result["error"] == "signature_mismatch"

    async def test_handle_webhook_routes_known_event(self, connector):
        result = await connector.handle_webhook(
            {"type": "response.completed", "id": "evt-1"}, headers={}
        )
        assert result["status"] == "ok"
        assert result["event"] == "response.completed"
        assert result["event_id"] == "evt-1"

    async def test_handle_webhook_ignores_unknown_event(self, connector):
        result = await connector.handle_webhook({"type": "weird"}, headers={})
        assert result["status"] == "ignored"

    async def test_handle_event(self, connector):
        result = await connector.handle_event({"id": "evt-9"})
        assert result["processed"] is True
        assert result["event_id"] == "evt-9"

    async def test_batch_processor_counts_processed(self, connector):
        result = await connector.batch_processor([{"id": "e-1"}, {"id": "e-2"}])
        assert result["processed"] == 2
        assert result["failed"] == 0


# ─────────────────────────────────────────────────────────────────────
# Connector identity + multi-tenant isolation
# ─────────────────────────────────────────────────────────────────────


class TestIdentity:
    def test_connector_type_class_attr(self):
        assert OpenAIConnector.CONNECTOR_TYPE == "openai"

    def test_auth_type_class_attr(self):
        assert OpenAIConnector.AUTH_TYPE == "api_key"

    def test_required_config_keys(self):
        assert "api_key" in OpenAIConnector.REQUIRED_CONFIG_KEYS

    def test_status_map_classifies_401_403_429(self):
        assert OpenAIConnector._STATUS_MAP[401] == ("OFFLINE", "TOKEN_EXPIRED")
        assert OpenAIConnector._STATUS_MAP[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
        assert OpenAIConnector._STATUS_MAP[429] == ("DEGRADED", "CONNECTED")

    def test_independent_instances_per_tenant(self, mock_OpenAIHTTPClient):
        c1 = OpenAIConnector(tenant_id="t-A", connector_id="conn-1", config={"api_key": "sk-a"})
        c2 = OpenAIConnector(tenant_id="t-B", connector_id="conn-2", config={"api_key": "sk-b"})
        assert c1.tenant_id != c2.tenant_id
        assert c1.api_key != c2.api_key
