"""Vonage connector unit tests.

Conforms to TEST_SYSTEM_PROMPT:
- `from connector import VonageConnector` (rootdir-based, no package prefix)
- Patch target strings start with `connector.`
- httpx mock pattern: AsyncMock for request, MagicMock for response (.json sync)
- side_effect uses plain dicts, never AsyncMock wrappers
- Default list mocks omit pagination tokens — pagination tested via side_effect
- No freezegun / factory_boy / hypothesis / faker (none installed)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from connector import VonageConnector
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

    async def test_install_with_full_creds_returns_healthy_without_api_call(
        self, connector, mock_VonageHTTPClient
    ):
        _, mock_instance = mock_VonageHTTPClient
        status = await connector.install()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.CONNECTED
        # CONNECTOR_SYSTEM_PROMPT rule: install() MUST NOT call the API.
        assert mock_instance.request.await_count == 0

    async def test_install_basic_only_also_healthy(
        self, connector_basic_only, mock_VonageHTTPClient
    ):
        """JWT credentials are optional — install passes with only api_key/secret."""
        _, mock_instance = mock_VonageHTTPClient
        status = await connector_basic_only.install()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.CONNECTED
        assert mock_instance.request.await_count == 0


# ─────────────────────────────────────────────────────────────────────
# health_check()
# ─────────────────────────────────────────────────────────────────────


class TestHealthCheck:
    async def test_missing_credentials(self, empty_connector):
        status = await empty_connector.health_check()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_healthy(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"value": 12.34, "autoReload": False}
        )
        await connector.install()
        status = await connector.health_check()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.CONNECTED
        # health_check() should probe /account/get-balance.
        called_url = mock_instance.request.call_args.args[1]
        assert "/account/get-balance" in called_url

    async def test_health_check_failure(self, connector, mock_VonageHTTPClient):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.side_effect = RuntimeError("boom")
        await connector.install()
        status = await connector.health_check()
        assert status.health == ConnectorHealth.UNHEALTHY
        assert status.auth_status == AuthStatus.FAILED


# ─────────────────────────────────────────────────────────────────────
# Account surface
# ─────────────────────────────────────────────────────────────────────


class TestAccount:
    async def test_get_balance(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"value": 100.0, "autoReload": False}
        )
        await connector.install()
        result = await connector.get_balance()
        assert result["value"] == 100.0
        called_url = mock_instance.request.call_args.args[1]
        assert called_url.endswith("/account/get-balance")


# ─────────────────────────────────────────────────────────────────────
# SMS surface
# ─────────────────────────────────────────────────────────────────────


class TestSMS:
    async def test_send_sms(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={
                "message-count": "1",
                "messages": [{"to": "447700900000", "message-id": "abc", "status": "0"}],
            }
        )
        await connector.install()
        result = await connector.send_sms({"from": "Shielva", "to": "447700900000", "text": "hi"})
        assert result["message-count"] == "1"
        method = mock_instance.request.call_args.args[0]
        url = mock_instance.request.call_args.args[1]
        kwargs = mock_instance.request.call_args.kwargs
        assert method == "POST"
        assert url.endswith("/sms/json")
        # check_envelope is requested so non-zero envelope statuses raise
        assert kwargs.get("check_envelope") is True
        # api_key + api_secret merged into form body
        assert kwargs["data"]["api_key"] == "test-api-key"
        assert kwargs["data"]["api_secret"] == "test-api-secret"
        assert kwargs["data"]["to"] == "447700900000"

    async def test_get_sms_status(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"message-id": "abc", "status": "delivered"}
        )
        await connector.install()
        result = await connector.get_sms_status("abc")
        assert result["message-id"] == "abc"
        url = mock_instance.request.call_args.args[1]
        assert url.endswith("/search/message")
        # ID passed via query params alongside api_key / api_secret
        params = mock_instance.request.call_args.kwargs["params"]
        assert params["id"] == "abc"
        assert params["api_key"] == "test-api-key"

    async def test_list_messages_extracts_next_url(
        self, connector, mock_VonageHTTPClient, response_factory
    ):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={
                "count": 2,
                "items": [{"message-id": "m1"}, {"message-id": "m2"}],
                "_links": {"next": {"href": "https://rest.nexmo.com/search/messages?page_index=1"}},
            }
        )
        await connector.install()
        page = await connector.list_messages(page_size=50)
        assert len(page["items"]) == 2
        assert page["next_url"].endswith("page_index=1")
        assert page["count"] == 2

    async def test_list_messages_default_no_next_url(
        self, connector, mock_VonageHTTPClient, response_factory
    ):
        # Default mock MUST NOT carry a continuation URL (avoid sync() infinite loop).
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"items": []})
        await connector.install()
        page = await connector.list_messages()
        assert page["items"] == []
        assert page["next_url"] is None


# ─────────────────────────────────────────────────────────────────────
# Voice surface (JWT mode)
# ─────────────────────────────────────────────────────────────────────


class TestVoice:
    async def test_create_call(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"uuid": "c-1", "status": "started"}
        )
        await connector.install()
        result = await connector.create_call({
            "to": [{"type": "phone", "number": "447700900001"}],
            "from": {"type": "phone", "number": "447700900000"},
            "answer_url": ["https://example.com/answer"],
        })
        assert result["uuid"] == "c-1"
        method = mock_instance.request.call_args.args[0]
        url = mock_instance.request.call_args.args[1]
        kwargs = mock_instance.request.call_args.kwargs
        assert method == "POST"
        assert url.endswith("/v1/calls")
        assert kwargs["auth_mode"] == "jwt"

    async def test_create_call_requires_ncco_or_answer_url(self, connector, mock_VonageHTTPClient):
        await connector.install()
        with pytest.raises(ValueError):
            await connector.create_call({
                "to": [{"type": "phone", "number": "447700900001"}],
                "from": {"type": "phone", "number": "447700900000"},
            })

    async def test_get_call(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"uuid": "c-2", "status": "completed"}
        )
        await connector.install()
        result = await connector.get_call("c-2")
        assert result["uuid"] == "c-2"
        assert mock_instance.request.call_args.args[1].endswith("/v1/calls/c-2")
        assert mock_instance.request.call_args.kwargs["auth_mode"] == "jwt"

    async def test_update_call_returns_body_when_present(
        self, connector, mock_VonageHTTPClient, response_factory
    ):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"uuid": "c-2", "status": "completed"}
        )
        await connector.install()
        result = await connector.update_call("c-2", {"action": "hangup"})
        assert result["status"] == "completed"

    async def test_update_call_empty_body_returns_placeholder(
        self, connector, mock_VonageHTTPClient, response_factory
    ):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(content=b"")
        await connector.install()
        result = await connector.update_call("c-2", {"action": "hangup"})
        assert result == {"call_uuid": "c-2", "updated": True}

    async def test_list_calls_extracts_pagination(
        self, connector, mock_VonageHTTPClient, response_factory
    ):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={
                "count": 2,
                "_embedded": {"calls": [{"uuid": "c-1"}, {"uuid": "c-2"}]},
                "_links": {"next": {"href": "https://api.nexmo.com/v1/calls?record_index=2"}},
            }
        )
        await connector.install()
        page = await connector.list_calls(page_size=2)
        assert len(page["items"]) == 2
        assert page["next_url"].endswith("record_index=2")

    async def test_list_calls_no_calls(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"count": 0})
        await connector.install()
        page = await connector.list_calls()
        assert page["items"] == []
        assert page["next_url"] is None

    async def test_get_call_recording_returns_bytes(
        self, connector, mock_VonageHTTPClient, response_factory
    ):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(content=b"WAV-AUDIO")
        await connector.install()
        data = await connector.get_call_recording("https://api.nexmo.com/v1/files/r-1")
        assert data == b"WAV-AUDIO"
        # Recording URL passes through verbatim + JWT auth + audio Accept header
        assert mock_instance.request.call_args.kwargs["auth_mode"] == "jwt"
        assert mock_instance.request.call_args.kwargs["headers"]["Accept"] == "audio/wav"


# ─────────────────────────────────────────────────────────────────────
# Verify v2 surface
# ─────────────────────────────────────────────────────────────────────


class TestVerify:
    async def test_send_verify_request(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"request_id": "req-1"}
        )
        await connector.install()
        result = await connector.send_verify_request({
            "brand": "Shielva",
            "workflow": [{"channel": "sms", "to": "447700900000"}],
        })
        assert result["request_id"] == "req-1"
        url = mock_instance.request.call_args.args[1]
        assert url.endswith("/v2/verify")
        assert mock_instance.request.call_args.kwargs["auth_mode"] == "basic"

    async def test_check_verify_code(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"status": "completed"})
        await connector.install()
        result = await connector.check_verify_code("req-1", "1234")
        assert result["status"] == "completed"
        url = mock_instance.request.call_args.args[1]
        assert url.endswith("/v2/verify/req-1")

    async def test_check_verify_code_empty_body(
        self, connector, mock_VonageHTTPClient, response_factory
    ):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(content=b"")
        await connector.install()
        result = await connector.check_verify_code("req-1", "1234")
        assert result == {"request_id": "req-1", "verified": True}

    async def test_cancel_verify(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(content=b"")
        await connector.install()
        result = await connector.cancel_verify("req-1")
        assert result == {"request_id": "req-1", "cancelled": True}
        assert mock_instance.request.call_args.args[0] == "DELETE"


# ─────────────────────────────────────────────────────────────────────
# Numbers surface
# ─────────────────────────────────────────────────────────────────────


class TestNumbers:
    async def test_list_numbers(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={"count": 1, "numbers": [{"msisdn": "447700900000", "country": "GB"}]}
        )
        await connector.install()
        result = await connector.list_numbers(country="GB", size=10)
        assert result["count"] == 1
        url = mock_instance.request.call_args.args[1]
        assert url.endswith("/account/numbers")

    async def test_search_numbers(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(json_body={"count": 0, "numbers": []})
        await connector.install()
        result = await connector.search_numbers("US", pattern="555", features="SMS")
        assert result["count"] == 0
        url = mock_instance.request.call_args.args[1]
        params = mock_instance.request.call_args.kwargs["params"]
        assert url.endswith("/number/search")
        assert params["country"] == "US"
        assert params["pattern"] == "555"
        assert params["features"] == "SMS"

    async def test_buy_number(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(content=b"")
        await connector.install()
        result = await connector.buy_number("GB", "447700900000")
        assert result == {"country": "GB", "msisdn": "447700900000", "purchased": True}
        url = mock_instance.request.call_args.args[1]
        assert url.endswith("/number/buy")
        data = mock_instance.request.call_args.kwargs["data"]
        assert data["country"] == "GB"
        assert data["api_key"] == "test-api-key"

    async def test_cancel_number(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(content=b"")
        await connector.install()
        result = await connector.cancel_number("GB", "447700900000")
        assert result == {"country": "GB", "msisdn": "447700900000", "cancelled": True}
        url = mock_instance.request.call_args.args[1]
        assert url.endswith("/number/cancel")


# ─────────────────────────────────────────────────────────────────────
# Applications surface
# ─────────────────────────────────────────────────────────────────────


class TestApplications:
    async def test_list_applications(self, connector, mock_VonageHTTPClient, response_factory):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.return_value = response_factory(
            json_body={
                "total_items": 1,
                "_embedded": {"applications": [{"id": "a-1", "name": "My App"}]},
            }
        )
        await connector.install()
        result = await connector.list_applications()
        assert result["items"][0]["id"] == "a-1"
        assert result["total_items"] == 1


# ─────────────────────────────────────────────────────────────────────
# sync() — multi-tenant isolation
# ─────────────────────────────────────────────────────────────────────


class TestSync:
    async def test_sync_missing_credentials_fails(self, empty_connector):
        result = await empty_connector.sync()
        assert result.status == SyncStatus.FAILED
        assert result.documents_synced == 0

    async def test_sync_aggregates_messages_and_calls(
        self, connector, mock_VonageHTTPClient, response_factory
    ):
        _, mock_instance = mock_VonageHTTPClient
        # side_effect uses plain dict payloads (no AsyncMock wrappers) per TEST_SYSTEM_PROMPT.
        # Last page omits _links.next.
        mock_instance.request.side_effect = [
            response_factory(  # list_messages page 1 — last page
                json_body={"items": [{"message-id": "m-1", "body": "hello"}]},
            ),
            response_factory(  # list_calls page 1 — last page
                json_body={"count": 0, "_embedded": {"calls": []}},
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

    async def test_sync_skips_calls_when_jwt_missing(
        self, connector_basic_only, mock_VonageHTTPClient, response_factory
    ):
        """Without application_id + private_key, sync only fetches SMS."""
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.side_effect = [
            response_factory(  # list_messages page 1 — last page
                json_body={"items": [{"message-id": "m-1", "body": "hello"}]},
            ),
        ]
        await connector_basic_only.install()
        connector_basic_only.ingest_batch = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = await connector_basic_only.sync(full=True)
        assert result.status == SyncStatus.SUCCESS
        assert result.documents_synced == 1
        # Only one request — calls iteration is skipped.
        assert mock_instance.request.await_count == 1

    async def test_sync_paginates_messages_via_side_effect(
        self, connector, mock_VonageHTTPClient, response_factory
    ):
        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.side_effect = [
            response_factory(
                json_body={
                    "items": [{"message-id": "m-1"}],
                    "_links": {"next": {"href": "https://x/search/messages?page_index=1"}},
                },
            ),
            response_factory(  # page 2, no next link
                json_body={"items": [{"message-id": "m-2"}]},
            ),
            response_factory(json_body={"count": 0, "_embedded": {"calls": []}}),  # list_calls
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
        result = await connector.process_callback({"event_type": "message:submitted"}, headers={})
        assert result["verified"] is True
        assert result.get("unverified") is True

    async def test_process_callback_jwt_valid(
        self, mock_VonageHTTPClient, connector_config
    ):
        import jwt as _jwt
        from connector import VonageConnector

        c = VonageConnector(
            tenant_id="tenant-1",
            connector_id="conn-1",
            config={**connector_config, "webhook_secret": "shh"},
        )
        payload = {"event_type": "message:submitted", "message_uuid": "m-1"}
        token = _jwt.encode({"iat": 1, "jti": "j-1"}, "shh", algorithm="HS256")
        if isinstance(token, bytes):
            token = token.decode("ascii")
        result = await c.process_callback(payload, headers={"Authorization": f"Bearer {token}"})
        assert result["verified"] is True

    async def test_process_callback_jwt_invalid(
        self, mock_VonageHTTPClient, connector_config
    ):
        from connector import VonageConnector

        c = VonageConnector(
            tenant_id="tenant-1",
            connector_id="conn-1",
            config={**connector_config, "webhook_secret": "shh"},
        )
        result = await c.process_callback(
            {"event_type": "message:submitted"},
            headers={"Authorization": "Bearer not.a.jwt"},
        )
        assert result["verified"] is False
        assert "jwt_invalid" in result["error"]

    async def test_process_callback_hmac_fallback_valid(
        self, mock_VonageHTTPClient, connector_config
    ):
        import hashlib
        import hmac as _hmac
        import json as _json

        from connector import VonageConnector

        c = VonageConnector(
            tenant_id="tenant-1",
            connector_id="conn-1",
            config={**connector_config, "webhook_secret": "shh"},
        )
        payload = {"event_type": "message:submitted", "message_uuid": "m-1"}
        raw = _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = _hmac.new(b"shh", raw, hashlib.sha256).hexdigest()
        result = await c.process_callback(payload, headers={"X-Vonage-Signature": sig})
        assert result["verified"] is True

    async def test_process_callback_hmac_fallback_invalid(
        self, mock_VonageHTTPClient, connector_config
    ):
        from connector import VonageConnector

        c = VonageConnector(
            tenant_id="tenant-1",
            connector_id="conn-1",
            config={**connector_config, "webhook_secret": "shh"},
        )
        result = await c.process_callback(
            {"event_type": "message:submitted"},
            headers={"X-Vonage-Signature": "deadbeef"},
        )
        assert result["verified"] is False
        assert result["error"] == "signature_mismatch"

    async def test_process_callback_missing_signature(
        self, mock_VonageHTTPClient, connector_config
    ):
        from connector import VonageConnector

        c = VonageConnector(
            tenant_id="tenant-1",
            connector_id="conn-1",
            config={**connector_config, "webhook_secret": "shh"},
        )
        result = await c.process_callback({"event_type": "x"}, headers={})
        assert result["verified"] is False
        assert result["error"] == "signature_missing"

    async def test_handle_webhook_routes_message_submitted(self, connector):
        result = await connector.handle_webhook(
            {"event_type": "message:submitted", "message_uuid": "m-9"}, headers={}
        )
        assert result["status"] == "ok"
        assert result["event"] == "message:submitted"
        assert result["message_id"] == "m-9"

    async def test_handle_webhook_routes_call_started(self, connector):
        result = await connector.handle_webhook(
            {"event_type": "call:started", "uuid": "c-9"}, headers={}
        )
        assert result["status"] == "ok"
        assert result["event"] == "call:started"
        assert result["call_uuid"] == "c-9"

    async def test_handle_webhook_infers_event_from_status(self, connector):
        """Voice payloads without `event_type` carry the lifecycle in `status`."""
        result = await connector.handle_webhook(
            {"status": "answered", "uuid": "c-9"}, headers={}
        )
        assert result["status"] == "ok"
        assert result["event"] == "call:answered"

    async def test_handle_webhook_ignores_unknown_event(self, connector):
        result = await connector.handle_webhook({"event_type": "weird-event"}, headers={})
        assert result["status"] == "ignored"

    async def test_handle_event_returns_processed(self, connector):
        result = await connector.handle_event({"message_uuid": "m-9"})
        assert result["processed"] is True
        assert result["event_id"] == "m-9"

    async def test_batch_processor_counts_processed(self, connector):
        result = await connector.batch_processor([{"id": "e-1"}, {"id": "e-2"}])
        assert result["processed"] == 2
        assert result["failed"] == 0


# ─────────────────────────────────────────────────────────────────────
# Failure classification
# ─────────────────────────────────────────────────────────────────────


class TestFailureClassification:
    async def test_401_maps_to_offline_token_expired(self, connector, mock_VonageHTTPClient):
        from exceptions import VonageAuthError

        _, mock_instance = mock_VonageHTTPClient
        exc = VonageAuthError("nope", status_code=401)
        mock_instance.request.side_effect = exc
        await connector.install()
        status = await connector.health_check()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.TOKEN_EXPIRED

    async def test_403_maps_to_unhealthy_invalid_creds(self, connector, mock_VonageHTTPClient):
        from exceptions import VonageAuthError

        _, mock_instance = mock_VonageHTTPClient
        exc = VonageAuthError("nope", status_code=403)
        mock_instance.request.side_effect = exc
        await connector.install()
        status = await connector.health_check()
        assert status.health == ConnectorHealth.UNHEALTHY
        assert status.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_429_maps_to_degraded_connected(self, connector, mock_VonageHTTPClient):
        from exceptions import VonageRateLimitError

        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.side_effect = VonageRateLimitError("slow down", retry_after_s=1.0)
        await connector.install()
        status = await connector.health_check()
        assert status.health == ConnectorHealth.DEGRADED
        assert status.auth_status == AuthStatus.CONNECTED

    async def test_insufficient_funds_maps_to_degraded(self, connector, mock_VonageHTTPClient):
        from exceptions import VonageInsufficientFunds

        _, mock_instance = mock_VonageHTTPClient
        mock_instance.request.side_effect = VonageInsufficientFunds("broke", status_code=402)
        await connector.install()
        status = await connector.health_check()
        assert status.health == ConnectorHealth.DEGRADED
        assert status.auth_status == AuthStatus.CONNECTED
        assert "insufficient funds" in status.message
