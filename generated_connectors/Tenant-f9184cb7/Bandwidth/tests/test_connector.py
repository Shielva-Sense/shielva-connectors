"""Bandwidth connector unit tests.

Conforms to TEST_SYSTEM_PROMPT:
- `from connector import BandwidthConnector` (rootdir-based, no package prefix)
- Patch target strings start with `connector.`
- httpx mock pattern: AsyncMock for request, MagicMock for response (.json sync)
- side_effect uses plain dicts, never AsyncMock wrappers
- Default list mocks omit pagination tokens — pagination tested via side_effect
- No freezegun / factory_boy / hypothesis / faker (none installed)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from connector import BandwidthConnector
from shared.base_connector import (
    AuthStatus,
    ConnectorHealth,
    SyncStatus,
)


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
        self, connector, mock_BandwidthHTTPClient
    ):
        _, mock_instance = mock_BandwidthHTTPClient
        status = await connector.install()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.CONNECTED
        # CONNECTOR_SYSTEM_PROMPT rule: install() MUST NOT call the API.
        assert mock_instance.request.await_count == 0


# ─────────────────────────────────────────────────────────────────────
# health_check()
# ─────────────────────────────────────────────────────────────────────


class TestHealthCheck:
    async def test_missing_credentials(self, empty_connector):
        status = await empty_connector.health_check()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_healthy(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"applications": []})
        await connector.install()
        status = await connector.health_check()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.CONNECTED
        # health_check() should probe Dashboard /applications.
        called_url = mock_instance.request.call_args.args[1]
        assert "/applications" in called_url

    async def test_health_check_failure(self, connector, mock_BandwidthHTTPClient):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.side_effect = RuntimeError("boom")
        await connector.install()
        status = await connector.health_check()
        assert status.health == ConnectorHealth.UNHEALTHY
        assert status.auth_status == AuthStatus.FAILED


# ─────────────────────────────────────────────────────────────────────
# Messaging surface
# ─────────────────────────────────────────────────────────────────────


class TestMessaging:
    async def test_send_message(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"id": "msg-1", "text": "hi"}
        )
        await connector.install()
        result = await connector.send_message({
            "applicationId": "app-1",
            "to": ["+15551112222"],
            "from": "+15553334444",
            "text": "hi",
        })
        assert result == {"id": "msg-1", "text": "hi"}
        method = mock_instance.request.call_args.args[0]
        url = mock_instance.request.call_args.args[1]
        assert method == "POST"
        assert url.endswith("/users/5000123/messages")

    async def test_get_message(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"id": "msg-2"})
        await connector.install()
        result = await connector.get_message("msg-2")
        assert result["id"] == "msg-2"
        assert mock_instance.request.call_args.args[1].endswith("/users/5000123/messages/msg-2")

    async def test_list_messages_extracts_pagination_token(
        self, connector, mock_BandwidthHTTPClient, response_factory
    ):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"messages": [{"id": "m1"}, {"id": "m2"}]},
            headers={"Link": '<https://example.com/messages?pageToken=abc>; rel="next"'},
        )
        await connector.install()
        page = await connector.list_messages(limit=50)
        assert len(page["items"]) == 2
        assert page["next_page_token"] == "abc"

    async def test_list_messages_default_no_next_token(
        self, connector, mock_BandwidthHTTPClient, response_factory
    ):
        # Default mock MUST NOT carry a continuation token (avoid sync() infinite loop).
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"messages": []})
        await connector.install()
        page = await connector.list_messages()
        assert page["items"] == []
        assert page["next_page_token"] is None

    async def test_upload_media(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory()
        await connector.install()
        result = await connector.upload_media("media-1", b"\x00\x01", content_type="image/png")
        assert result == {"media_id": "media-1", "uploaded": True}
        assert mock_instance.request.call_args.args[0] == "PUT"

    async def test_delete_media(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory()
        await connector.install()
        result = await connector.delete_media("media-1")
        assert result == {"media_id": "media-1", "deleted": True}

    async def test_list_media(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(json_body=[{"mediaName": "x"}])
        await connector.install()
        items = await connector.list_media()
        assert items == [{"mediaName": "x"}]


# ─────────────────────────────────────────────────────────────────────
# Voice surface
# ─────────────────────────────────────────────────────────────────────


class TestVoice:
    async def test_create_call(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"callId": "c-1"})
        await connector.install()
        result = await connector.create_call({
            "applicationId": "app-1",
            "to": "+15551112222",
            "from": "+15553334444",
            "answerUrl": "https://x/answer",
        })
        assert result["callId"] == "c-1"
        assert mock_instance.request.call_args.args[1].endswith("/accounts/5000123/calls")

    async def test_get_call(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"callId": "c-2"})
        await connector.install()
        result = await connector.get_call("c-2")
        assert result["callId"] == "c-2"

    async def test_update_call_returns_body_when_present(
        self, connector, mock_BandwidthHTTPClient, response_factory
    ):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"callId": "c-2", "state": "completed"}
        )
        await connector.install()
        result = await connector.update_call("c-2", {"state": "completed"})
        assert result["state"] == "completed"

    async def test_update_call_empty_body_returns_placeholder(
        self, connector, mock_BandwidthHTTPClient, response_factory
    ):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(content=b"")
        await connector.install()
        result = await connector.update_call("c-2", {"state": "completed"})
        assert result == {"call_id": "c-2", "updated": True}

    async def test_list_calls_extracts_pagination(
        self, connector, mock_BandwidthHTTPClient, response_factory
    ):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body=[{"callId": "c-1"}, {"callId": "c-2"}],
            headers={"Link": '<https://example.com/calls?pageToken=xyz>; rel="next"'},
        )
        await connector.install()
        page = await connector.list_calls(limit=2)
        assert len(page["items"]) == 2
        assert page["next_page_token"] == "xyz"

    async def test_get_call_recordings(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(json_body=[{"recordingId": "r-1"}])
        await connector.install()
        recs = await connector.get_call_recordings("c-1")
        assert recs[0]["recordingId"] == "r-1"

    async def test_download_recording_returns_bytes(
        self, connector, mock_BandwidthHTTPClient, response_factory
    ):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(content=b"WAV-AUDIO")
        await connector.install()
        data = await connector.download_recording("c-1", "r-1")
        assert data == b"WAV-AUDIO"


# ─────────────────────────────────────────────────────────────────────
# Dashboard surface
# ─────────────────────────────────────────────────────────────────────


class TestDashboard:
    async def test_list_applications(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"applications": [{"applicationId": "a-1"}]}
        )
        await connector.install()
        apps = await connector.list_applications()
        assert apps[0]["applicationId"] == "a-1"

    async def test_list_phone_numbers(self, connector, mock_BandwidthHTTPClient, response_factory):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"orders": []})
        await connector.install()
        body = await connector.list_phone_numbers(page=1, size=10)
        assert "orders" in body


# ─────────────────────────────────────────────────────────────────────
# sync() — multi-tenant isolation
# ─────────────────────────────────────────────────────────────────────


class TestSync:
    async def test_sync_missing_credentials_fails(self, empty_connector):
        result = await empty_connector.sync()
        assert result.status == SyncStatus.FAILED
        assert result.documents_synced == 0

    async def test_sync_aggregates_messages_and_calls(
        self, connector, mock_BandwidthHTTPClient, response_factory
    ):
        _, mock_instance = mock_BandwidthHTTPClient
        # side_effect uses plain dict payloads (no AsyncMock wrappers) per TEST_SYSTEM_PROMPT.
        # Bandwidth Link header carries pagination; last page omits the next link.
        mock_instance.request.side_effect = [
            response_factory(  # list_messages page 1 — last page (no next link)
                json_body={"messages": [{"id": "m-1", "text": "hello"}]},
                headers={},
            ),
            response_factory(  # list_calls page 1 — last page
                json_body=[],
                headers={},
            ),
        ]
        await connector.install()
        connector.ingest_batch = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = await connector.sync(full=True)
        assert result.status == SyncStatus.SUCCESS
        assert result.documents_synced == 1
        ingested = connector.ingest_batch.await_args.args[0]
        # Multi-tenant scoping: id has tenant_id_ prefix.
        assert ingested[0].id.startswith("tenant-1_")
        assert ingested[0].tenant_id == "tenant-1"
        assert ingested[0].connector_id == "conn-1"

    async def test_sync_paginates_messages_via_side_effect(
        self, connector, mock_BandwidthHTTPClient, response_factory
    ):
        _, mock_instance = mock_BandwidthHTTPClient
        mock_instance.request.side_effect = [
            response_factory(
                json_body={"messages": [{"id": "m-1"}]},
                headers={"Link": '<https://x/messages?pageToken=t2>; rel="next"'},
            ),
            response_factory(  # page 2, no next link
                json_body={"messages": [{"id": "m-2"}]},
                headers={},
            ),
            response_factory(json_body=[], headers={}),  # list_calls last page
        ]
        await connector.install()
        connector.ingest_batch = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = await connector.sync(full=True)
        assert result.status == SyncStatus.SUCCESS
        assert result.documents_synced == 2


# ─────────────────────────────────────────────────────────────────────
# Handler overrides — process_callback / handle_webhook
# ─────────────────────────────────────────────────────────────────────


class TestCallbacks:
    async def test_process_callback_no_secret_accepts_unverified(self, connector):
        result = await connector.process_callback({"eventType": "message-received"}, headers={})
        assert result["verified"] is True
        assert result.get("unverified") is True

    async def test_process_callback_with_secret_valid(
        self, mock_BandwidthHTTPClient, connector_config
    ):
        import hashlib
        import hmac as _hmac
        import json as _json

        from connector import BandwidthConnector

        c = BandwidthConnector(
            tenant_id="tenant-1",
            connector_id="conn-1",
            config={**connector_config, "webhook_secret": "shh"},
        )
        payload = {"eventType": "message-received", "message": {"id": "m-1"}}
        raw = _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = _hmac.new(b"shh", raw, hashlib.sha256).hexdigest()
        result = await c.process_callback(payload, headers={"X-Callback-Signature": sig})
        assert result["verified"] is True

    async def test_process_callback_with_secret_invalid(
        self, mock_BandwidthHTTPClient, connector_config
    ):
        from connector import BandwidthConnector

        c = BandwidthConnector(
            tenant_id="tenant-1",
            connector_id="conn-1",
            config={**connector_config, "webhook_secret": "shh"},
        )
        result = await c.process_callback(
            {"eventType": "message-received"}, headers={"X-Callback-Signature": "deadbeef"}
        )
        assert result["verified"] is False
        assert result["error"] == "signature_mismatch"

    async def test_handle_webhook_routes_known_event(self, connector):
        result = await connector.handle_webhook(
            {"eventType": "message-received", "message": {"id": "m-9"}}, headers={}
        )
        assert result["status"] == "ok"
        assert result["event"] == "message-received"
        assert result["message_id"] == "m-9"

    async def test_handle_webhook_ignores_unknown_event(self, connector):
        result = await connector.handle_webhook({"eventType": "weird-event"}, headers={})
        assert result["status"] == "ignored"

    async def test_handle_event_returns_processed(self, connector):
        result = await connector.handle_event({"id": "e-9"})
        assert result["processed"] is True
        assert result["event_id"] == "e-9"

    async def test_batch_processor_counts_processed(self, connector):
        result = await connector.batch_processor([{"id": "e-1"}, {"id": "e-2"}])
        assert result["processed"] == 2
        assert result["failed"] == 0
