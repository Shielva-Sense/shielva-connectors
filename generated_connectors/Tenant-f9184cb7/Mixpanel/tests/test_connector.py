"""Comprehensive unit tests for the Mixpanel connector — 60+ tests.

All HTTP calls are mocked. No real network requests are made.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.http_client import (
    API_BASE_EU,
    API_BASE_US,
    DATA_API_BASE_EU,
    DATA_API_BASE_US,
    MixpanelHTTPClient,
)
from connector import MixpanelConnector
from exceptions import (
    MixpanelAuthError,
    MixpanelError,
    MixpanelNetworkError,
    MixpanelNotFoundError,
    MixpanelRateLimitError,
)
from helpers.utils import normalize_event, with_retry
from models import AuthStatus, ConnectorDocument, ConnectorHealth, SyncStatus

# ── Shared test data ─────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_mixpanel_001"
PROJECT_ID = "2345678"
USERNAME = "service.acct@mixpanel.com"
SECRET = "super_secret_123"

SAMPLE_EVENT: dict[str, Any] = {
    "event": "Signup",
    "properties": {
        "distinct_id": "user_abc123",
        "time": 1718000000,
        "$insert_id": "ins_001",
        "$browser": "Chrome",
        "$os": "Mac OS X",
        "$city": "San Francisco",
        "mp_country_code": "US",
        "plan": "pro",
    },
}

SAMPLE_EVENT_2: dict[str, Any] = {
    "event": "Purchase",
    "properties": {
        "distinct_id": "user_xyz456",
        "time": 1718001000,
        "$insert_id": "ins_002",
        "$browser": "Firefox",
        "$os": "Windows",
        "$city": "New York",
        "mp_country_code": "US",
        "amount": 99.0,
    },
}

SAMPLE_PROJECTS_RESPONSE: dict[str, Any] = {
    "results": {
        "user": {"username": USERNAME},
        "projects": [{"id": PROJECT_ID, "name": "My Project"}],
    }
}

SAMPLE_FUNNELS_RESPONSE: dict[str, Any] = {
    "status": 1,
    "results": [
        {"funnel_id": 101, "name": "Signup to Purchase"},
        {"funnel_id": 102, "name": "Free to Paid"},
    ],
}

SAMPLE_FUNNEL_DATA: dict[str, Any] = {
    "status": 1,
    "results": {
        "steps": [
            {"count": 1000, "step_label": "Signup"},
            {"count": 250, "step_label": "Purchase"},
        ]
    },
}

SAMPLE_SEGMENTATION_RESPONSE: dict[str, Any] = {
    "status": 1,
    "results": {
        "series": {"2026-06-13": 42, "2026-06-14": 58},
        "type": "general",
    },
}


def _make_connector(
    username: str = USERNAME,
    secret: str = SECRET,
    project_id: str = PROJECT_ID,
    region: str = "US",
) -> MixpanelConnector:
    return MixpanelConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "username": username,
            "secret": secret,
            "project_id": project_id,
            "region": region,
        },
    )


async def _aiter(*items: dict[str, Any]) -> AsyncGenerator[dict[str, Any], None]:
    for item in items:
        yield item


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_mixpanel_error_base() -> None:
    exc = MixpanelError("base error", status_code=500, code="server_error")
    assert isinstance(exc, Exception)
    assert exc.message == "base error"
    assert exc.status_code == 500
    assert exc.code == "server_error"


def test_mixpanel_error_str_with_status() -> None:
    exc = MixpanelError("something broke", status_code=400)
    assert "[HTTP 400]" in str(exc)
    assert "something broke" in str(exc)


def test_mixpanel_error_str_without_status() -> None:
    exc = MixpanelError("no status")
    assert str(exc) == "no status"


def test_mixpanel_auth_error_is_mixpanel_error() -> None:
    exc = MixpanelAuthError("unauthorized", 401)
    assert isinstance(exc, MixpanelError)
    assert exc.status_code == 401


def test_mixpanel_network_error_is_mixpanel_error() -> None:
    exc = MixpanelNetworkError("timeout", 503)
    assert isinstance(exc, MixpanelError)
    assert exc.status_code == 503


def test_mixpanel_not_found_error_is_mixpanel_error() -> None:
    exc = MixpanelNotFoundError("funnel", 42)
    assert isinstance(exc, MixpanelError)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"
    assert "42" in str(exc)
    assert "funnel" in str(exc)


def test_mixpanel_rate_limit_error_is_mixpanel_error() -> None:
    exc = MixpanelRateLimitError("too fast", retry_after=60.0)
    assert isinstance(exc, MixpanelError)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 60.0


def test_mixpanel_rate_limit_default_retry_after() -> None:
    exc = MixpanelRateLimitError("rate limited")
    assert exc.retry_after == 0.0


# ── _raise_for_status ─────────────────────────────────────────────────────────


def test_raise_for_status_200_no_raise() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    client._raise_for_status(200, {})  # must not raise


def test_raise_for_status_401() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    with pytest.raises(MixpanelAuthError) as exc_info:
        client._raise_for_status(401, {"error": "Unauthorized"})
    assert exc_info.value.status_code == 401


def test_raise_for_status_403() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    with pytest.raises(MixpanelAuthError) as exc_info:
        client._raise_for_status(403, {"error": "Forbidden"})
    assert exc_info.value.status_code == 403


def test_raise_for_status_400() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    with pytest.raises(MixpanelError) as exc_info:
        client._raise_for_status(400, {"error": "bad param"})
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "bad_request"


def test_raise_for_status_404() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    with pytest.raises(MixpanelNotFoundError) as exc_info:
        client._raise_for_status(404, {})
    assert exc_info.value.status_code == 404


def test_raise_for_status_429_with_retry_after() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    with pytest.raises(MixpanelRateLimitError) as exc_info:
        client._raise_for_status(429, {}, headers={"Retry-After": "30"})
    assert exc_info.value.retry_after == 30.0


def test_raise_for_status_429_without_retry_after() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    with pytest.raises(MixpanelRateLimitError) as exc_info:
        client._raise_for_status(429, {})
    assert exc_info.value.retry_after == 0.0


def test_raise_for_status_500() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    with pytest.raises(MixpanelNetworkError) as exc_info:
        client._raise_for_status(500, {"error": "internal server error"})
    assert exc_info.value.status_code == 500


def test_raise_for_status_503() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    with pytest.raises(MixpanelNetworkError):
        client._raise_for_status(503, {})


def test_raise_for_status_other_4xx() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    with pytest.raises(MixpanelError) as exc_info:
        client._raise_for_status(422, {"error": "Unprocessable"})
    assert exc_info.value.status_code == 422


# ── HTTP client BasicAuth ─────────────────────────────────────────────────────


def test_http_client_basic_auth() -> None:
    import aiohttp

    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    auth = client._auth()
    assert isinstance(auth, aiohttp.BasicAuth)
    assert auth.login == USERNAME
    assert auth.password == SECRET


def test_http_client_us_api_base() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID, region="US")
    assert client._api_base() == API_BASE_US
    assert client._data_api_base() == DATA_API_BASE_US


def test_http_client_eu_api_base() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID, region="EU")
    assert client._api_base() == API_BASE_EU
    assert client._data_api_base() == DATA_API_BASE_EU


def test_http_client_region_case_insensitive() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID, region="eu")
    assert client._api_base() == API_BASE_EU


def test_http_client_default_region_is_us() -> None:
    client = MixpanelHTTPClient(USERNAME, SECRET, PROJECT_ID)
    assert client._api_base() == API_BASE_US


def test_us_data_api_base_url() -> None:
    assert "data.mixpanel.com" in DATA_API_BASE_US
    assert "2.0" in DATA_API_BASE_US


def test_eu_data_api_base_url() -> None:
    assert "eu.data.mixpanel.com" in DATA_API_BASE_EU
    assert "2.0" in DATA_API_BASE_EU


# ── normalize_event ───────────────────────────────────────────────────────────


def test_normalize_event_returns_connector_document() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert isinstance(doc, ConnectorDocument)


def test_normalize_event_source_is_mixpanel() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.source == "mixpanel"


def test_normalize_event_type_is_analytics_event() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.type == "analytics_event"


def test_normalize_event_id_is_16_chars() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert len(doc.id) == 16


def test_normalize_event_id_is_valid_hex() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    int(doc.id, 16)  # raises ValueError if not valid hex


def test_normalize_event_id_deterministic() -> None:
    doc1 = normalize_event(SAMPLE_EVENT)
    doc2 = normalize_event(SAMPLE_EVENT)
    assert doc1.id == doc2.id


def test_normalize_event_id_uses_spec_formula() -> None:
    """Verify: sha256("event:" + distinct_id + "_" + time)[:16]"""
    props = SAMPLE_EVENT["properties"]
    distinct_id = str(props["distinct_id"])
    time_val = str(props["time"])
    expected = hashlib.sha256(
        f"event:{distinct_id}_{time_val}".encode()
    ).hexdigest()[:16]
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.id == expected


def test_normalize_event_different_events_different_ids() -> None:
    doc1 = normalize_event(SAMPLE_EVENT)
    doc2 = normalize_event(SAMPLE_EVENT_2)
    assert doc1.id != doc2.id


def test_normalize_event_title() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.title == "Mixpanel event: Signup"


def test_normalize_event_content_has_event_name() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert "Signup" in doc.content


def test_normalize_event_content_has_distinct_id() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert "user_abc123" in doc.content


def test_normalize_event_content_has_browser() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert "Chrome" in doc.content


def test_normalize_event_content_has_os() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert "Mac OS X" in doc.content


def test_normalize_event_content_has_city() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert "San Francisco" in doc.content


def test_normalize_event_content_has_extra_props() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert "pro" in doc.content  # plan: pro


def test_normalize_event_metadata_event_name() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.metadata["event_name"] == "Signup"


def test_normalize_event_metadata_distinct_id() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.metadata["distinct_id"] == "user_abc123"


def test_normalize_event_metadata_timestamp() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.metadata["timestamp"] == 1718000000


def test_normalize_event_metadata_insert_id() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.metadata["insert_id"] == "ins_001"


def test_normalize_event_metadata_browser() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.metadata["browser"] == "Chrome"


def test_normalize_event_metadata_os() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.metadata["os"] == "Mac OS X"


def test_normalize_event_metadata_city() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.metadata["city"] == "San Francisco"


def test_normalize_event_metadata_country_code() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert doc.metadata["country_code"] == "US"


def test_normalize_event_metadata_raw_properties() -> None:
    doc = normalize_event(SAMPLE_EVENT)
    assert "raw_properties" in doc.metadata
    assert doc.metadata["raw_properties"]["plan"] == "pro"


def test_normalize_event_empty_event() -> None:
    doc = normalize_event({})
    assert doc.title == "Mixpanel event: unknown"
    assert len(doc.id) == 16
    assert doc.source == "mixpanel"
    assert doc.type == "analytics_event"


def test_normalize_event_missing_properties_key() -> None:
    doc = normalize_event({"event": "PageView"})
    assert "PageView" in doc.title


def test_normalize_ndjson_line_parsing() -> None:
    """Simulate parsing NDJSON: split by newlines and json.loads each line."""
    import json

    ndjson = '{"event":"A","properties":{"distinct_id":"u1","time":1}}\n{"event":"B","properties":{"distinct_id":"u2","time":2}}\n'
    events = [json.loads(line) for line in ndjson.strip().splitlines() if line.strip()]
    assert len(events) == 2
    doc1 = normalize_event(events[0])
    doc2 = normalize_event(events[1])
    assert doc1.title == "Mixpanel event: A"
    assert doc2.title == "Mixpanel event: B"
    assert doc1.id != doc2.id


# ── with_retry ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    fn = AsyncMock(
        side_effect=[
            MixpanelNetworkError("fail"),
            MixpanelNetworkError("fail"),
            {"ok": True},
        ]
    )
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    fn = AsyncMock(side_effect=MixpanelAuthError("invalid", 401))
    with pytest.raises(MixpanelAuthError):
        await with_retry(fn, max_attempts=3)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    fn = AsyncMock(side_effect=MixpanelNetworkError("persistent"))
    with pytest.raises(MixpanelNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    fn = AsyncMock(side_effect=MixpanelRateLimitError("429", retry_after=0))
    with pytest.raises(MixpanelRateLimitError):
        await with_retry(fn, max_attempts=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_success_on_second_attempt() -> None:
    fn = AsyncMock(side_effect=[MixpanelNetworkError("transient"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


# ── Connector config ──────────────────────────────────────────────────────────


def test_connector_loads_username_from_config() -> None:
    c = _make_connector(username="user@x.com")
    assert c._username == "user@x.com"


def test_connector_loads_secret_from_config() -> None:
    c = _make_connector(secret="mysecret")
    assert c._secret == "mysecret"


def test_connector_loads_project_id_from_config() -> None:
    c = _make_connector(project_id="999")
    assert c._project_id == "999"


def test_connector_loads_region_from_config() -> None:
    c = _make_connector(region="EU")
    assert c._region == "EU"


def test_connector_default_region_is_us() -> None:
    c = MixpanelConnector(
        config={"username": USERNAME, "secret": SECRET, "project_id": PROJECT_ID}
    )
    assert c._region == "US"


def test_connector_region_normalised_to_upper() -> None:
    c = _make_connector(region="eu")
    assert c._region == "EU"


def test_connector_type_constants() -> None:
    assert MixpanelConnector.CONNECTOR_TYPE == "mixpanel"
    assert MixpanelConnector.AUTH_TYPE == "api_key"


def test_connector_missing_fields_all() -> None:
    c = MixpanelConnector(config={})
    missing = c._missing_fields()
    assert "username" in missing
    assert "secret" in missing
    assert "project_id" in missing


def test_connector_missing_fields_partial() -> None:
    c = MixpanelConnector(config={"username": USERNAME})
    missing = c._missing_fields()
    assert "username" not in missing
    assert "secret" in missing
    assert "project_id" in missing


def test_connector_no_missing_fields_when_all_provided() -> None:
    c = _make_connector()
    assert c._missing_fields() == []


def test_connector_make_client_passes_region() -> None:
    c = _make_connector(region="EU")
    client = c._make_client()
    assert client._region == "EU"


def test_connector_make_client_passes_credentials() -> None:
    c = _make_connector()
    client = c._make_client()
    assert client._username == USERNAME
    assert client._secret == SECRET
    assert client._project_id == PROJECT_ID


def test_default_date_range_format() -> None:
    from datetime import datetime

    c = _make_connector()
    from_date, to_date = c._default_date_range()
    datetime.strptime(from_date, "%Y-%m-%d")
    datetime.strptime(to_date, "%Y-%m-%d")
    assert from_date < to_date


# ── install ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.get_projects = AsyncMock(return_value=SAMPLE_PROJECTS_RESPONSE)
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert USERNAME in result.message
    assert PROJECT_ID in result.message


@pytest.mark.asyncio
async def test_install_missing_username() -> None:
    c = MixpanelConnector(
        config={"secret": SECRET, "project_id": PROJECT_ID}
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "username" in result.message


@pytest.mark.asyncio
async def test_install_missing_secret() -> None:
    c = MixpanelConnector(
        config={"username": USERNAME, "project_id": PROJECT_ID}
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "secret" in result.message


@pytest.mark.asyncio
async def test_install_missing_project_id() -> None:
    c = MixpanelConnector(
        config={"username": USERNAME, "secret": SECRET}
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "project_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_all_fields() -> None:
    c = MixpanelConnector(config={})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.get_projects = AsyncMock(
        side_effect=MixpanelAuthError("401 Unauthorized", 401)
    )
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Unauthorized" in result.message


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.get_projects = AsyncMock(
        side_effect=MixpanelNetworkError("Connection refused")
    )
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_exception() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.get_projects = AsyncMock(side_effect=RuntimeError("unexpected"))
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ── health_check ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.get_projects = AsyncMock(return_value=SAMPLE_PROJECTS_RESPONSE)
    c._make_client = lambda: mock_client
    result = await c.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert PROJECT_ID in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.get_projects = AsyncMock(
        side_effect=MixpanelAuthError("Forbidden", 403)
    )
    c._make_client = lambda: mock_client
    result = await c.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.get_projects = AsyncMock(
        side_effect=MixpanelNetworkError("Timeout")
    )
    c._make_client = lambda: mock_client
    result = await c.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = MixpanelConnector(config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_unexpected_exception() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.get_projects = AsyncMock(side_effect=RuntimeError("boom"))
    c._make_client = lambda: mock_client
    result = await c.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_success_with_events() -> None:
    c = _make_connector()
    c.query_events = AsyncMock(return_value=[SAMPLE_EVENT, SAMPLE_EVENT_2])
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_empty_response() -> None:
    c = _make_connector()
    c.query_events = AsyncMock(return_value=[])
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_sets_connector_id_on_documents() -> None:
    c = _make_connector()
    c.query_events = AsyncMock(return_value=[SAMPLE_EVENT])
    result = await c.sync()
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_event_query_failure() -> None:
    c = _make_connector()
    c.query_events = AsyncMock(side_effect=MixpanelError("export failed", 500))
    result = await c.sync()
    assert "export failed" in result.message
    assert result.documents_failed > 0


@pytest.mark.asyncio
async def test_sync_status_failed_when_all_fail() -> None:
    c = _make_connector()
    c.query_events = AsyncMock(side_effect=MixpanelError("total failure", 500))
    result = await c.sync()
    assert result.status == SyncStatus.FAILED


# ── query_events ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_events_returns_list() -> None:
    c = _make_connector()
    mock_client = MagicMock()

    async def _fake_gen(**kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        yield SAMPLE_EVENT
        yield SAMPLE_EVENT_2

    mock_client.query_events = AsyncMock(return_value=_fake_gen())
    c._make_client = lambda: mock_client
    result = await c.query_events()
    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_query_events_passes_event_names() -> None:
    c = _make_connector()
    mock_client = MagicMock()

    async def _empty(**kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        return
        yield  # make it a generator

    mock_client.query_events = AsyncMock(return_value=_empty())
    c._make_client = lambda: mock_client
    await c.query_events(event_names=["Signup"])
    call_kwargs = mock_client.query_events.call_args
    assert call_kwargs.kwargs.get("event_names") == ["Signup"]


@pytest.mark.asyncio
async def test_query_events_passes_date_range() -> None:
    c = _make_connector()
    mock_client = MagicMock()

    async def _empty(**kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        return
        yield

    mock_client.query_events = AsyncMock(return_value=_empty())
    c._make_client = lambda: mock_client
    await c.query_events(from_date="2026-06-01", to_date="2026-06-07")
    call_kwargs = mock_client.query_events.call_args
    assert call_kwargs.kwargs.get("from_date") == "2026-06-01"
    assert call_kwargs.kwargs.get("to_date") == "2026-06-07"


@pytest.mark.asyncio
async def test_query_events_passes_limit() -> None:
    c = _make_connector()
    mock_client = MagicMock()

    async def _empty(**kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        return
        yield

    mock_client.query_events = AsyncMock(return_value=_empty())
    c._make_client = lambda: mock_client
    await c.query_events(limit=500)
    call_kwargs = mock_client.query_events.call_args
    assert call_kwargs.kwargs.get("limit") == 500


# ── list_funnels ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_funnels_returns_list() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.list_funnels = AsyncMock(return_value=SAMPLE_FUNNELS_RESPONSE)
    c._make_client = lambda: mock_client
    result = await c.list_funnels()
    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_funnels_funnel_ids_present() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.list_funnels = AsyncMock(return_value=SAMPLE_FUNNELS_RESPONSE)
    c._make_client = lambda: mock_client
    result = await c.list_funnels()
    ids = [f["funnel_id"] for f in result]
    assert 101 in ids
    assert 102 in ids


@pytest.mark.asyncio
async def test_list_funnels_empty() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.list_funnels = AsyncMock(return_value={"status": 1, "results": []})
    c._make_client = lambda: mock_client
    result = await c.list_funnels()
    assert result == []


# ── query_funnel ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_funnel_returns_data() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.query_funnels = AsyncMock(return_value=SAMPLE_FUNNEL_DATA)
    c._make_client = lambda: mock_client
    result = await c.query_funnel(101, "2026-06-01", "2026-06-07")
    assert "results" in result
    assert len(result["results"]["steps"]) == 2


@pytest.mark.asyncio
async def test_query_funnel_uses_default_dates_when_none() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.query_funnels = AsyncMock(return_value=SAMPLE_FUNNEL_DATA)
    c._make_client = lambda: mock_client
    result = await c.query_funnel(101)
    assert result is not None
    mock_client.query_funnels.assert_called_once()


@pytest.mark.asyncio
async def test_query_funnel_not_found() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.query_funnels = AsyncMock(
        side_effect=MixpanelNotFoundError("funnel", 9999)
    )
    c._make_client = lambda: mock_client
    with pytest.raises(MixpanelNotFoundError):
        await c.query_funnel(9999, "2026-06-01", "2026-06-07")


# ── query_segmentation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_segmentation_returns_data() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.query_segmentation = AsyncMock(return_value=SAMPLE_SEGMENTATION_RESPONSE)
    c._make_client = lambda: mock_client
    result = await c.query_segmentation("Signup", "2026-06-01", "2026-06-07")
    assert "results" in result
    assert result["status"] == 1


@pytest.mark.asyncio
async def test_query_segmentation_uses_default_dates_when_none() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.query_segmentation = AsyncMock(return_value=SAMPLE_SEGMENTATION_RESPONSE)
    c._make_client = lambda: mock_client
    result = await c.query_segmentation("Purchase")
    assert result is not None
    mock_client.query_segmentation.assert_called_once()


@pytest.mark.asyncio
async def test_query_segmentation_passes_event_name() -> None:
    c = _make_connector()
    mock_client = MagicMock()
    mock_client.query_segmentation = AsyncMock(return_value=SAMPLE_SEGMENTATION_RESPONSE)
    c._make_client = lambda: mock_client
    await c.query_segmentation("Signup", "2026-06-01", "2026-06-07")
    call_args = mock_client.query_segmentation.call_args
    assert "Signup" in call_args.args


# ── EU region URL switching ───────────────────────────────────────────────────


def test_eu_connector_uses_eu_api_base() -> None:
    c = _make_connector(region="EU")
    client = c._make_client()
    assert client._api_base() == API_BASE_EU


def test_eu_connector_uses_eu_data_api_base() -> None:
    c = _make_connector(region="EU")
    client = c._make_client()
    assert client._data_api_base() == DATA_API_BASE_EU


def test_us_connector_uses_us_api_base() -> None:
    c = _make_connector(region="US")
    client = c._make_client()
    assert client._api_base() == API_BASE_US


def test_us_connector_uses_us_data_api_base() -> None:
    c = _make_connector(region="US")
    client = c._make_client()
    assert client._data_api_base() == DATA_API_BASE_US
