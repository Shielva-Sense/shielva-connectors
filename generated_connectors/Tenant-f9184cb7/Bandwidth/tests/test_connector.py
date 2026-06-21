"""Bandwidth connector unit tests.

Follows the TEST_SYSTEM_PROMPT contract:
- `from bandwidth_connector.connector import BandwidthConnector` (absolute, no relative)
- Patch where used (`bandwidth_connector.connector.BandwidthHTTPClient`)
- httpx mock: `request` is AsyncMock, response is MagicMock (json() is sync)
- No freezegun / factory_boy / faker
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bandwidth_connector.connector import (
    AuthStatus,
    BandwidthConnector,
    ConnectorHealth,
    SyncStatus,
)


# ─────────────────────────────────────────────────────────────────────
# install()
# ─────────────────────────────────────────────────────────────────────


class TestInstall:
    async def test_missing_credentials_returns_offline(self, empty_connector):
        status = await empty_connector.install()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_successful_install_probes_applications(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body={"applications": []})
        status = await connector.install()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.CONNECTED
        # The install probe should hit the Dashboard /applications endpoint.
        called_url = mock_http_client.request.call_args.args[1]
        assert "/applications" in called_url

    async def test_failed_install_returns_degraded(self, connector, mock_http_client):
        mock_http_client.request.side_effect = Exception("network down")
        status = await connector.install()
        assert status.health == ConnectorHealth.DEGRADED
        assert status.auth_status == AuthStatus.INVALID_CREDENTIALS


# ─────────────────────────────────────────────────────────────────────
# authorize() — basic_auth: returns empty token (no exchange)
# ─────────────────────────────────────────────────────────────────────


class TestAuthorize:
    async def test_authorize_returns_metadata_only(self, connector):
        token = await connector.authorize({})
        assert token.access_token == ""
        assert token.token_type == "Basic"
        assert token.metadata["connector_type"] == "bandwidth"
        assert token.metadata["auth_type"] == "basic_auth"


# ─────────────────────────────────────────────────────────────────────
# health_check()
# ─────────────────────────────────────────────────────────────────────


class TestHealthCheck:
    async def test_missing_credentials(self, empty_connector):
        status = await empty_connector.health_check()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_healthy(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body={"applications": []})
        status = await connector.health_check()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.AUTHENTICATED

    async def test_failure_path(self, connector, mock_http_client):
        mock_http_client.request.side_effect = RuntimeError("boom")
        status = await connector.health_check()
        assert status.health == ConnectorHealth.DEGRADED
        assert status.auth_status == AuthStatus.FAILED


# ─────────────────────────────────────────────────────────────────────
# Messaging surface
# ─────────────────────────────────────────────────────────────────────


class TestMessaging:
    async def test_send_message(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body={"id": "msg-1", "text": "hi"})
        result = await connector.send_message({"applicationId": "app-1", "to": ["+15551112222"], "from": "+15553334444", "text": "hi"})
        assert result == {"id": "msg-1", "text": "hi"}
        method, url = mock_http_client.request.call_args.args[0], mock_http_client.request.call_args.args[1]
        assert method == "POST"
        assert url.endswith("/users/5000123/messages")

    async def test_get_message(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body={"id": "msg-2"})
        result = await connector.get_message("msg-2")
        assert result["id"] == "msg-2"
        assert mock_http_client.request.call_args.args[1].endswith("/users/5000123/messages/msg-2")

    async def test_list_messages_with_pagination(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(
            json_body={"messages": [{"id": "m1"}, {"id": "m2"}]},
            headers={"Link": '<https://example.com/messages?pageToken=abc>; rel="next"'},
        )
        page = await connector.list_messages(limit=50)
        assert len(page["items"]) == 2
        assert page["next_page_token"] == "abc"

    async def test_list_messages_no_next(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body={"messages": []}, headers={})
        page = await connector.list_messages()
        assert page["items"] == []
        assert page["next_page_token"] is None

    async def test_upload_media(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory()
        result = await connector.upload_media("media-1", b"\x00\x01", content_type="image/png")
        assert result == {"media_id": "media-1", "uploaded": True}
        method = mock_http_client.request.call_args.args[0]
        assert method == "PUT"

    async def test_delete_media(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory()
        result = await connector.delete_media("media-1")
        assert result == {"media_id": "media-1", "deleted": True}

    async def test_list_media(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body=[{"mediaName": "x"}])
        items = await connector.list_media()
        assert items == [{"mediaName": "x"}]


# ─────────────────────────────────────────────────────────────────────
# Voice surface
# ─────────────────────────────────────────────────────────────────────


class TestVoice:
    async def test_create_call(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body={"callId": "c-1"})
        result = await connector.create_call({"applicationId": "app-1", "to": "+15551112222", "from": "+15553334444", "answerUrl": "https://x/answer"})
        assert result["callId"] == "c-1"
        url = mock_http_client.request.call_args.args[1]
        assert url.endswith("/accounts/5000123/calls")

    async def test_get_call(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body={"callId": "c-2"})
        result = await connector.get_call("c-2")
        assert result["callId"] == "c-2"

    async def test_update_call(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body={"callId": "c-2", "state": "completed"})
        result = await connector.update_call("c-2", {"state": "completed"})
        assert result["state"] == "completed"

    async def test_list_calls_pagination(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(
            json_body=[{"callId": "c-1"}, {"callId": "c-2"}],
            headers={"Link": '<https://example.com/calls?pageToken=xyz>; rel="next"'},
        )
        page = await connector.list_calls(limit=2)
        assert len(page["items"]) == 2
        assert page["next_page_token"] == "xyz"

    async def test_get_call_recordings(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body=[{"recordingId": "r-1"}])
        recs = await connector.get_call_recordings("c-1")
        assert recs[0]["recordingId"] == "r-1"

    async def test_download_recording_returns_bytes(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(content=b"WAV-AUDIO")
        data = await connector.download_recording("c-1", "r-1")
        assert data == b"WAV-AUDIO"


# ─────────────────────────────────────────────────────────────────────
# Dashboard surface
# ─────────────────────────────────────────────────────────────────────


class TestDashboard:
    async def test_list_applications(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(json_body={"applications": [{"applicationId": "a-1"}]})
        apps = await connector.list_applications()
        assert apps[0]["applicationId"] == "a-1"

    async def test_list_phone_numbers(self, connector, mock_http_client, response_factory):
        mock_http_client.request.return_value = response_factory(
            json_body={"orders": []},
            headers={"content-type": "application/json"},
        )
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

    async def test_sync_aggregates_messages_and_calls(self, connector, mock_http_client, response_factory):
        # First page (messages) → next page → empty
        # Then calls → empty
        responses = [
            response_factory(json_body={"messages": [{"id": "m-1", "text": "hello"}]}, headers={}),
            response_factory(json_body=[], headers={}),  # calls
        ]
        mock_http_client.request.side_effect = responses
        # Patch ingest_batch to count documents
        connector.ingest_batch = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = await connector.sync(full=True)
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_synced == 1
        # Tenant scoping on the normalized document
        ingest_call = connector.ingest_batch.await_args
        docs = ingest_call.args[0]
        assert docs[0].tenant_id == "tenant-1"
        assert docs[0].connector_id == "conn-1"


# ─────────────────────────────────────────────────────────────────────
# process_callback / handle_webhook (HMAC-SHA256)
# ─────────────────────────────────────────────────────────────────────


class TestCallbacks:
    async def test_process_callback_no_secret_returns_unverified_accept(self, connector):
        result = await connector.process_callback({"eventType": "message-received"}, headers={})
        assert result["verified"] is True
        assert result.get("unverified") is True

    async def test_process_callback_with_secret_valid(self, creds):
        import hashlib
        import hmac as _hmac
        import json as _json

        from bandwidth_connector.connector import BandwidthConnector

        connector = BandwidthConnector(
            tenant_id="tenant-1", connector_id="conn-1",
            config={**creds, "webhook_secret": "shh"},
        )
        payload = {"eventType": "message-received", "message": {"id": "m-1"}}
        raw = _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = _hmac.new(b"shh", raw, hashlib.sha256).hexdigest()
        result = await connector.process_callback(payload, headers={"X-Callback-Signature": sig})
        assert result["verified"] is True

    async def test_process_callback_with_secret_invalid(self, creds):
        from bandwidth_connector.connector import BandwidthConnector

        connector = BandwidthConnector(
            tenant_id="tenant-1", connector_id="conn-1",
            config={**creds, "webhook_secret": "shh"},
        )
        result = await connector.process_callback({"eventType": "message-received"}, headers={"X-Callback-Signature": "deadbeef"})
        assert result["verified"] is False
        assert result["error"] == "signature_mismatch"

    async def test_handle_webhook_routes_known_event(self, connector):
        result = await connector.handle_webhook(
            {"eventType": "message-received", "message": {"id": "m-9"}},
            headers={},
        )
        assert result["processed"] is True
        assert result["event"] == "message-received"
        assert result["message_id"] == "m-9"

    async def test_handle_webhook_rejects_unknown_event(self, connector):
        result = await connector.handle_webhook({"eventType": "weird-event"}, headers={})
        assert result["processed"] is False

    async def test_batch_processor(self, connector):
        result = await connector.batch_processor([{"id": "e-1"}, {"id": "e-2"}])
        assert result["processed"] == 2
        assert result["failed"] == 0
