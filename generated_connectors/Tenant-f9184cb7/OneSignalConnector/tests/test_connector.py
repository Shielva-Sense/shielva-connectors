"""Unit tests for OneSignalConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

import httpx
import pytest
import respx
from shared.base_connector import AuthStatus, ConnectorHealth

from connector import OneSignalConnector
from exceptions import (
    OneSignalAuthError,
    OneSignalBadRequestError,
    OneSignalError,
    OneSignalNotFoundError,
)
from tests.conftest import (
    APP_ID,
    CONNECTOR_ID,
    ONESIGNAL_BASE,
    REST_API_KEY,
    TENANT_ID,
    TEST_CONFIG,
    USER_AUTH_KEY,
)

BASE = ONESIGNAL_BASE


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


async def test_install_success(connector):
    status = await connector.install()
    assert status.health == ConnectorHealth.HEALTHY
    assert status.auth_status == AuthStatus.AUTHENTICATED
    assert status.connector_id == CONNECTOR_ID


async def test_install_missing_rest_api_key(connector):
    connector.config.pop("rest_api_key", None)
    status = await connector.install()
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert status.health == ConnectorHealth.OFFLINE


async def test_install_missing_app_id(connector):
    connector.config.pop("app_id", None)
    status = await connector.install()
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape — "Basic <raw_key>" — OneSignal quirk
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_authorization_header_is_basic_raw_key(connector):
    """OneSignal expects ``Authorization: Basic <key>`` with the RAW key.

    The literal word ``Basic`` is prepended but the key is NOT base64-encoded.
    """
    route = respx.get(f"{BASE}/apps/{APP_ID}").mock(
        return_value=httpx.Response(200, json={"id": APP_ID, "name": "ok"}),
    )
    await connector.get_app()
    assert route.called
    sent_auth = route.calls.last.request.headers.get("authorization")
    # User auth key takes precedence in get_app when set
    assert sent_auth == f"Basic {USER_AUTH_KEY}"
    # Must NOT be base64-encoded — the key value appears verbatim
    assert USER_AUTH_KEY in sent_auth
    assert sent_auth.startswith("Basic ")


@respx.mock
async def test_send_notification_uses_rest_api_key_in_basic_header(connector):
    route = respx.post(f"{BASE}/notifications").mock(
        return_value=httpx.Response(200, json={"id": "n1", "recipients": 1}),
    )
    await connector.send_notification(
        contents={"en": "Hi"},
    )
    sent_auth = route.calls.last.request.headers.get("authorization")
    assert sent_auth == f"Basic {REST_API_KEY}"


# ═══════════════════════════════════════════════════════════════════════════
# Health check
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{BASE}/apps/{APP_ID}").mock(
        return_value=httpx.Response(200, json={"id": APP_ID, "name": "X"}),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
async def test_health_check_auth_error_401(connector):
    respx.get(f"{BASE}/apps/{APP_ID}").mock(
        return_value=httpx.Response(401, json={"errors": ["Invalid key"]}),
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
async def test_health_check_auth_error_403(connector):
    respx.get(f"{BASE}/apps/{APP_ID}").mock(
        return_value=httpx.Response(403, json={"errors": ["Forbidden"]}),
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Apps
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_list_apps_uses_user_auth_key(connector):
    route = respx.get(f"{BASE}/apps").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": APP_ID, "name": "App One", "players": 10},
                {"id": "other-app", "name": "App Two", "players": 0},
            ],
        ),
    )
    result = await connector.list_apps()
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["id"] == APP_ID
    sent_auth = route.calls.last.request.headers.get("authorization")
    assert sent_auth == f"Basic {USER_AUTH_KEY}"


@respx.mock
async def test_list_apps_without_user_auth_key_raises(connector):
    connector.user_auth_key = ""
    with pytest.raises(OneSignalAuthError):
        await connector.list_apps()


@respx.mock
async def test_get_app_not_found(connector):
    respx.get(f"{BASE}/apps/missing").mock(
        return_value=httpx.Response(404, json={"errors": ["App not found"]}),
    )
    with pytest.raises(OneSignalNotFoundError):
        await connector.get_app("missing")


@respx.mock
async def test_create_app_uses_user_auth_key(connector):
    route = respx.post(f"{BASE}/apps").mock(
        return_value=httpx.Response(200, json={"id": "new-app", "name": "Fresh"}),
    )
    result = await connector.create_app(name="Fresh", gcm_key="g-123")
    assert result["id"] == "new-app"
    import json as _json
    body = _json.loads(route.calls.last.request.content.decode())
    assert body == {"name": "Fresh", "gcm_key": "g-123"}
    assert route.calls.last.request.headers.get("authorization") == f"Basic {USER_AUTH_KEY}"


# ═══════════════════════════════════════════════════════════════════════════
# Notifications
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_send_notification_payload_shape(connector):
    route = respx.post(f"{BASE}/notifications").mock(
        return_value=httpx.Response(200, json={"id": "notif-abc-123", "recipients": 5}),
    )
    result = await connector.send_notification(
        contents={"en": "Hello world"},
        headings={"en": "Greetings"},
        included_segments=["Subscribed Users"],
        data={"campaign": "welcome"},
    )
    assert result["id"] == "notif-abc-123"
    import json as _json
    sent = route.calls.last.request
    body = _json.loads(sent.content.decode())
    assert body["app_id"] == APP_ID
    assert body["contents"] == {"en": "Hello world"}
    assert body["headings"] == {"en": "Greetings"}
    assert body["included_segments"] == ["Subscribed Users"]
    assert body["data"] == {"campaign": "welcome"}


@respx.mock
async def test_get_notification_includes_app_id_query(connector):
    notif_id = "notif-1"
    route = respx.get(f"{BASE}/notifications/{notif_id}").mock(
        return_value=httpx.Response(
            200, json={"id": notif_id, "successful": 100, "failed": 1},
        ),
    )
    result = await connector.get_notification(notification_id=notif_id)
    assert result["id"] == notif_id
    sent_url = str(route.calls.last.request.url)
    assert f"app_id={APP_ID}" in sent_url


@respx.mock
async def test_list_notifications_paging(connector):
    route = respx.get(f"{BASE}/notifications").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 2,
                "offset": 0,
                "limit": 50,
                "notifications": [
                    {"id": "n1", "successful": 10},
                    {"id": "n2", "successful": 5},
                ],
            },
        ),
    )
    result = await connector.list_notifications(limit=50, offset=0)
    assert result["total_count"] == 2
    sent_url = str(route.calls.last.request.url)
    assert f"app_id={APP_ID}" in sent_url
    assert "limit=50" in sent_url


@respx.mock
async def test_cancel_notification(connector):
    notif_id = "to-cancel"
    route = respx.delete(f"{BASE}/notifications/{notif_id}").mock(
        return_value=httpx.Response(200, json={"success": "true"}),
    )
    result = await connector.cancel_notification(notification_id=notif_id)
    assert result == {"success": "true"}
    sent_url = str(route.calls.last.request.url)
    assert f"app_id={APP_ID}" in sent_url


@respx.mock
async def test_notification_history_body(connector):
    notif_id = "notif-xyz"
    route = respx.post(f"{BASE}/notifications/{notif_id}/history").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "destination_url": "https://example/export.csv"},
        ),
    )
    result = await connector.notification_history(
        notification_id=notif_id, events="clicked",
    )
    assert result["success"] is True
    import json as _json
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["events"] == "clicked"
    assert body["app_id"] == APP_ID


# ═══════════════════════════════════════════════════════════════════════════
# Devices (Players)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_create_device_payload(connector):
    route = respx.post(f"{BASE}/players").mock(
        return_value=httpx.Response(200, json={"success": True, "id": "player-abc"}),
    )
    result = await connector.create_device(
        device_type=1,
        identifier="device-token-xyz",
        language="en",
        tags={"plan": "pro"},
        external_user_id="user-42",
    )
    assert result["id"] == "player-abc"
    import json as _json
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["app_id"] == APP_ID
    assert body["device_type"] == 1
    assert body["identifier"] == "device-token-xyz"
    assert body["external_user_id"] == "user-42"
    assert body["tags"] == {"plan": "pro"}


@respx.mock
async def test_update_device_injects_app_id(connector):
    player_id = "player-1"
    route = respx.put(f"{BASE}/players/{player_id}").mock(
        return_value=httpx.Response(200, json={"success": True}),
    )
    result = await connector.update_device(
        player_id=player_id,
        fields={"tags": {"plan": "enterprise"}},
    )
    assert result["success"] is True
    import json as _json
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["app_id"] == APP_ID
    assert body["tags"] == {"plan": "enterprise"}


@respx.mock
async def test_list_devices(connector):
    route = respx.get(f"{BASE}/players").mock(
        return_value=httpx.Response(
            200, json={"players": [{"id": "p1"}], "total_count": 1},
        ),
    )
    result = await connector.list_devices(limit=10, offset=0)
    assert result["total_count"] == 1
    sent_url = str(route.calls.last.request.url)
    assert f"app_id={APP_ID}" in sent_url
    assert "limit=10" in sent_url


@respx.mock
async def test_get_device(connector):
    route = respx.get(f"{BASE}/players/p99").mock(
        return_value=httpx.Response(200, json={"id": "p99", "device_type": 1}),
    )
    result = await connector.get_device("p99")
    assert result["id"] == "p99"
    sent_url = str(route.calls.last.request.url)
    assert f"app_id={APP_ID}" in sent_url


# ═══════════════════════════════════════════════════════════════════════════
# Segments
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_list_segments(connector):
    route = respx.get(f"{BASE}/apps/{APP_ID}/segments").mock(
        return_value=httpx.Response(200, json={"segments": [{"id": "s1", "name": "Active"}]}),
    )
    result = await connector.list_segments()
    assert result["segments"][0]["id"] == "s1"
    assert route.called


@respx.mock
async def test_create_segment_payload(connector):
    route = respx.post(f"{BASE}/apps/{APP_ID}/segments").mock(
        return_value=httpx.Response(201, json={"id": "seg-new", "success": True}),
    )
    filters = [{"field": "tag", "key": "plan", "relation": "=", "value": "pro"}]
    result = await connector.create_segment(name="ProUsers", filters=filters)
    assert result["id"] == "seg-new"
    import json as _json
    body = _json.loads(route.calls.last.request.content.decode())
    assert body == {"name": "ProUsers", "filters": filters}


@respx.mock
async def test_delete_segment(connector):
    route = respx.delete(f"{BASE}/apps/{APP_ID}/segments/seg-x").mock(
        return_value=httpx.Response(200, json={"success": True}),
    )
    result = await connector.delete_segment("seg-x")
    assert result["success"] is True
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Error mapping
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_400_raises_bad_request(connector):
    respx.post(f"{BASE}/notifications").mock(
        return_value=httpx.Response(400, json={"errors": ["empty audience"]}),
    )
    with pytest.raises(OneSignalBadRequestError):
        await connector.send_notification(contents={"en": "hi"})


@respx.mock
async def test_401_raises_auth_error(connector):
    respx.get(f"{BASE}/notifications").mock(
        return_value=httpx.Response(401, json={"errors": ["Invalid key"]}),
    )
    with pytest.raises(OneSignalAuthError):
        await connector.list_notifications()


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_retry_on_429_then_succeeds(connector, no_retry_sleep):
    route = respx.get(f"{BASE}/apps/{APP_ID}").mock(
        side_effect=[
            httpx.Response(429, json={"errors": ["Too many requests"]}),
            httpx.Response(200, json={"id": APP_ID, "name": "OK"}),
        ],
    )
    result = await connector.get_app()
    assert result["id"] == APP_ID
    assert route.call_count == 2


@respx.mock
async def test_retry_on_500_then_succeeds(connector, no_retry_sleep):
    route = respx.get(f"{BASE}/notifications").mock(
        side_effect=[
            httpx.Response(500, json={"errors": ["boom"]}),
            httpx.Response(200, json={"notifications": [], "total_count": 0}),
        ],
    )
    result = await connector.list_notifications()
    assert result["total_count"] == 0
    assert route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert OneSignalConnector.CONNECTOR_TYPE == "onesignal"


def test_auth_type_class_attr():
    assert OneSignalConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(OneSignalConnector, "REQUIRED_CONFIG_KEYS")
    assert "rest_api_key" in OneSignalConnector.REQUIRED_CONFIG_KEYS
    assert "app_id" in OneSignalConnector.REQUIRED_CONFIG_KEYS


def test_status_map_present():
    assert OneSignalConnector._STATUS_MAP[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert OneSignalConnector._STATUS_MAP[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert OneSignalConnector._STATUS_MAP[429] == ("DEGRADED", "CONNECTED")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation — NormalizedDocument ids are tenant-scoped
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = OneSignalConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = OneSignalConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_normalize_doc_id_is_tenant_scoped():
    from helpers.normalizer import normalize_app, normalize_notification, normalize_player

    raw = {"id": "src-123", "name": "X", "created_at": "2025-01-01T00:00:00Z"}
    doc_a = normalize_app(raw, connector_id="c", tenant_id="t-A")
    doc_b = normalize_app(raw, connector_id="c", tenant_id="t-B")
    assert doc_a.id == "t-A_src-123"
    assert doc_b.id == "t-B_src-123"
    assert doc_a.source_id == "src-123"

    notif = {"id": "n-1", "headings": {"en": "Hi"}, "contents": {"en": "Body"}}
    doc_n = normalize_notification(notif, connector_id="c", tenant_id="t-A")
    assert doc_n.id == "t-A_n-1"
    assert doc_n.title == "Hi"
    assert doc_n.content == "Body"

    player = {"id": "p-1", "device_os": "iOS", "device_model": "iPhone"}
    doc_p = normalize_player(player, connector_id="c", tenant_id="t-A")
    assert doc_p.id == "t-A_p-1"
    assert "iOS" in doc_p.content
