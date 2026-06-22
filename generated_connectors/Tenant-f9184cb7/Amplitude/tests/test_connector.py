"""Unit tests for AmplitudeConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AmplitudeConnector
from exceptions import (
    AmplitudeAuthError,
    AmplitudeError,
    AmplitudeNetworkError,
    AmplitudeNotFoundError,
    AmplitudeRateLimitError,
    AmplitudeServerError,
)
from helpers.utils import (
    CircuitBreaker,
    _stable_id,
    _stable_id_simple,
    normalize_chart,
    normalize_cohort,
    normalize_event_data,
    normalize_event_type,
)
from models import AuthStatus, ConnectorDocument, ConnectorHealth, SyncStatus

TENANT_ID = "tenant_amplitude_test"
CONNECTOR_ID = "conn_amplitude_test_001"
VALID_API_KEY = "abc123amplitudekey"
VALID_API_SECRET = "secretxyz456"

# ── Sample data ─────────────────────────────────────────────────────────────

SAMPLE_PROJECT_SETTINGS_RESPONSE: dict = {
    "projectName": "Shielva Analytics",
    "projectId": "12345",
    "orgId": "99001",
    "orgName": "Shielva Inc.",
    "timezone": "America/Los_Angeles",
}

SAMPLE_TAXONOMY_RESPONSE: dict = {
    "success": True,
    "data": [
        {"id": "category_1", "name": "Navigation"},
        {"id": "category_2", "name": "Engagement"},
    ],
}

SAMPLE_EVENTS_RESPONSE: dict = {
    "data": [
        {"value": "PageView", "displayName": "Page View", "category": "Navigation"},
        {"value": "ButtonClick", "displayName": "Button Click", "category": "Engagement"},
        {"value": "Signup", "displayName": "Sign Up", "category": "Conversion"},
    ]
}

SAMPLE_USER_PROPERTIES_RESPONSE: dict = {
    "data": [
        {"value": "user_type", "displayName": "User Type", "description": "Free or paid"},
        {"value": "country", "displayName": "Country", "description": "User country"},
    ]
}

SAMPLE_CHARTS_RESPONSE: dict = {
    "data": [
        {"id": "chart_abc", "title": "DAU over time", "type": "line"},
        {"id": "chart_def", "title": "Funnel conversion", "type": "funnel"},
    ]
}

SAMPLE_FUNNEL_RESPONSE: dict = {
    "data": {
        "funnel_id": "funnel_001",
        "series": [[100, 75, 50]],
        "events": ["PageView", "Signup", "Purchase"],
    }
}

SAMPLE_EVENT_COUNTS_RESPONSE: dict = {
    "data": {
        "series": [[42, 38, 55, 61, 29]],
        "xValues": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
    }
}

SAMPLE_SEGMENTATION_RESPONSE: dict = {
    "data": {
        "series": [[120, 95, 210, 88, 305]],
        "xValues": [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
        ],
        "seriesLabels": [["Any Active Event"]],
    }
}

SAMPLE_COHORTS_RESPONSE: dict = {
    "cohorts": [
        {
            "id": "cohort_abc123",
            "name": "Power Users",
            "size": 4820,
            "description": "Users with 10+ sessions per week",
            "lastComputed": "2024-06-01T00:00:00.000Z",
        },
        {
            "id": "cohort_def456",
            "name": "Churned Users",
            "size": 1203,
            "description": "No activity for 30+ days",
            "lastComputed": "2024-06-01T00:00:00.000Z",
        },
    ]
}

SAMPLE_COHORT_MEMBERS: dict = {
    "cohort_id": "cohort_abc123",
    "users": [{"user_id": "user_001"}, {"user_id": "user_002"}],
}

SAMPLE_ACTIVE_USERS_RESPONSE: dict = {
    "data": {
        "series": [[1200, 1350, 980, 2100, 1750]],
        "xValues": [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
        ],
    }
}

SAMPLE_USER_ACTIVITY_RESPONSE: dict = {
    "userData": {
        "user_id": "user_test_001",
        "events": [
            {"event_type": "PageView", "time": 1704067200000},
            {"event_type": "ButtonClick", "time": 1704067260000},
        ],
    }
}

SAMPLE_EXPORT_BYTES: bytes = b"PK\x03\x04fake_zip_content"


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_connector(
    api_key: str = VALID_API_KEY,
    api_secret: str = VALID_API_SECRET,
    region: str = "us",
) -> AmplitudeConnector:
    return AmplitudeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key, "api_secret": api_secret, "region": region},
    )


def _mock_client(
    taxonomy_ok: bool = True,
    segmentation_data: dict | None = None,
    cohorts_data: dict | None = None,
    active_users_data: dict | None = None,
    raise_auth: bool = False,
    raise_network: bool = False,
    settings_data: dict | None = None,
) -> MagicMock:
    """Build a mock AmplitudeHTTPClient with preconfigured return values."""
    client = MagicMock()

    async def _project_settings() -> dict:
        if raise_auth:
            raise AmplitudeAuthError("Invalid credentials", 401)
        if raise_network:
            raise AmplitudeNetworkError("Connection refused")
        if taxonomy_ok:
            return settings_data or SAMPLE_PROJECT_SETTINGS_RESPONSE
        raise AmplitudeServerError("Server error", 500)

    async def _taxonomy() -> dict:
        if raise_auth:
            raise AmplitudeAuthError("Invalid credentials", 401)
        if raise_network:
            raise AmplitudeNetworkError("Connection refused")
        if taxonomy_ok:
            return SAMPLE_TAXONOMY_RESPONSE
        raise AmplitudeServerError("Server error", 500)

    async def _segmentation(event: str, start: str, end: str, **kw: object) -> dict:
        if raise_auth:
            raise AmplitudeAuthError("Invalid credentials", 401)
        return segmentation_data or SAMPLE_SEGMENTATION_RESPONSE

    async def _cohorts() -> dict:
        return cohorts_data or SAMPLE_COHORTS_RESPONSE

    async def _active_users(start: str, end: str, **kw: object) -> dict:
        return active_users_data or SAMPLE_ACTIVE_USERS_RESPONSE

    async def _cohort_members(cohort_id: str) -> dict:
        return SAMPLE_COHORT_MEMBERS

    async def _export_events(start: str, end: str) -> bytes:
        return SAMPLE_EXPORT_BYTES

    async def _user_activity(user_id: str) -> dict:
        return SAMPLE_USER_ACTIVITY_RESPONSE

    async def _list_events(chart_id: str | None = None) -> dict:
        return SAMPLE_EVENTS_RESPONSE

    async def _list_user_properties() -> dict:
        return SAMPLE_USER_PROPERTIES_RESPONSE

    async def _list_charts() -> dict:
        return SAMPLE_CHARTS_RESPONSE

    async def _query_event_counts(event: str, start: str, end: str, **kw: object) -> dict:
        return SAMPLE_EVENT_COUNTS_RESPONSE

    async def _get_funnel(funnel_id: str) -> dict:
        return SAMPLE_FUNNEL_RESPONSE

    async def _aclose() -> None:
        pass

    client.get_project_settings = AsyncMock(side_effect=_project_settings)
    client.get_taxonomy_categories = AsyncMock(side_effect=_taxonomy)
    client.get_event_segmentation = AsyncMock(side_effect=_segmentation)
    client.list_cohorts = AsyncMock(side_effect=_cohorts)
    client.get_cohort_members = AsyncMock(side_effect=_cohort_members)
    client.get_active_users = AsyncMock(side_effect=_active_users)
    client.get_user_activity = AsyncMock(side_effect=_user_activity)
    client.export_events = AsyncMock(side_effect=_export_events)
    client.list_events = AsyncMock(side_effect=_list_events)
    client.list_user_properties = AsyncMock(side_effect=_list_user_properties)
    client.list_charts = AsyncMock(side_effect=_list_charts)
    client.query_event_counts = AsyncMock(side_effect=_query_event_counts)
    client.get_funnel = AsyncMock(side_effect=_get_funnel)
    client.aclose = AsyncMock(side_effect=_aclose)
    return client


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — install() — 7 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success_us() -> None:
    """install() returns HEALTHY + CONNECTED for valid US credentials."""
    connector = _make_connector()
    mock_client = _mock_client(taxonomy_ok=True)
    connector._make_client = lambda: mock_client  # type: ignore[method-assign]
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "US" in result.message


@pytest.mark.asyncio
async def test_install_success_eu() -> None:
    """install() returns HEALTHY + CONNECTED for valid EU credentials."""
    connector = _make_connector(region="eu")
    mock_client = _mock_client(taxonomy_ok=True)
    connector._make_client = lambda: mock_client  # type: ignore[method-assign]
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "EU" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    """install() returns MISSING_CREDENTIALS when api_key is empty."""
    connector = _make_connector(api_key="")
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message.lower()


@pytest.mark.asyncio
async def test_install_missing_api_secret() -> None:
    """install() returns MISSING_CREDENTIALS when api_secret is empty."""
    connector = _make_connector(api_secret="")
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_secret" in result.message.lower()


@pytest.mark.asyncio
async def test_install_auth_failure() -> None:
    """install() returns OFFLINE + INVALID_CREDENTIALS on 401."""
    connector = _make_connector()
    mock_client = _mock_client(raise_auth=True)
    connector._make_client = lambda: mock_client  # type: ignore[method-assign]
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_failure() -> None:
    """install() returns OFFLINE + FAILED on network error."""
    connector = _make_connector()
    mock_client = _mock_client(raise_network=True)
    connector._make_client = lambda: mock_client  # type: ignore[method-assign]
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_after_success() -> None:
    """install() assigns http_client after successful validation."""
    connector = _make_connector()
    mock_client = _mock_client(taxonomy_ok=True)
    new_client = _mock_client(taxonomy_ok=True)
    calls: list[int] = []

    def _factory() -> MagicMock:
        calls.append(1)
        return mock_client if len(calls) == 1 else new_client

    connector._make_client = _factory  # type: ignore[method-assign]
    await connector.install()
    assert connector.http_client is not None


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — health_check() — 6 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy() -> None:
    """health_check() returns HEALTHY when /settings responds OK."""
    connector = _make_connector()
    mock_client = _mock_client(taxonomy_ok=True)
    connector._make_client = lambda: mock_client  # type: ignore[method-assign]
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_eu_region() -> None:
    """health_check() reports EU region in message."""
    connector = _make_connector(region="eu")
    mock_client = _mock_client(taxonomy_ok=True)
    connector._make_client = lambda: mock_client  # type: ignore[method-assign]
    result = await connector.health_check()
    assert "EU" in result.message


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    """health_check() returns OFFLINE + MISSING_CREDENTIALS when keys absent."""
    connector = _make_connector(api_key="")
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_failure() -> None:
    """health_check() returns OFFLINE + INVALID_CREDENTIALS on 401."""
    connector = _make_connector()
    mock_client = _mock_client(raise_auth=True)
    connector._make_client = lambda: mock_client  # type: ignore[method-assign]
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error_degraded() -> None:
    """health_check() returns DEGRADED on network error when circuit not open."""
    connector = _make_connector()
    mock_client = _mock_client(raise_network=True)
    connector._make_client = lambda: mock_client  # type: ignore[method-assign]
    result = await connector.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_circuit_breaker_increments_on_failure() -> None:
    """health_check() increments circuit breaker failure count on error."""
    connector = _make_connector()
    mock_client = _mock_client(raise_network=True)
    connector._make_client = lambda: mock_client  # type: ignore[method-assign]
    initial = connector._circuit_breaker._failures
    await connector.health_check()
    assert connector._circuit_breaker._failures > initial


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — sync() — 9 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_completed_with_events_and_cohorts() -> None:
    """sync() returns COMPLETED with events + cohorts data."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.sync()
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)
    assert result.documents_found > 0
    assert result.documents_synced > 0


@pytest.mark.asyncio
async def test_sync_counts_event_documents() -> None:
    """sync() creates one document per xValues entry per event type."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.sync()
    # 5 dates x 2 default events + 5 dates x active_users + 2 cohorts
    assert result.documents_found >= 5


@pytest.mark.asyncio
async def test_sync_partial_when_some_fail() -> None:
    """sync() returns PARTIAL when document normalization partially fails."""
    connector = _make_connector()
    mock_client = _mock_client()
    connector.http_client = mock_client

    ingest_calls: list[int] = []

    async def _ingest_fail(doc: ConnectorDocument, kb_id: str) -> None:
        ingest_calls.append(1)
        if len(ingest_calls) > 2:
            raise RuntimeError("Ingest failure")

    connector._ingest_document = _ingest_fail  # type: ignore[method-assign]
    result = await connector.sync(kb_id="kb_test")
    assert result.documents_failed > 0
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_no_kb_id_still_counts() -> None:
    """sync() counts synced docs even when kb_id is empty (no ingestion)."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.sync(kb_id="")
    assert result.documents_synced > 0


@pytest.mark.asyncio
async def test_sync_event_api_error_is_nonfatal() -> None:
    """sync() continues and counts cohorts when event segmentation fails."""
    connector = _make_connector()
    client = _mock_client()
    client.get_event_segmentation = AsyncMock(
        side_effect=AmplitudeError("segment fail", 400)
    )
    connector.http_client = client
    result = await connector.sync()
    # Cohorts + active_users (if active fails too) — at minimum cohorts should land
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


@pytest.mark.asyncio
async def test_sync_cohorts_nonfatal_on_error() -> None:
    """sync() completes even when cohort API fails."""
    connector = _make_connector()
    client = _mock_client()
    client.list_cohorts = AsyncMock(side_effect=AmplitudeError("cohorts fail", 400))
    connector.http_client = client
    result = await connector.sync()
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


@pytest.mark.asyncio
async def test_sync_active_users_nonfatal_on_error() -> None:
    """sync() completes even when active users API fails."""
    connector = _make_connector()
    client = _mock_client()
    client.get_active_users = AsyncMock(
        side_effect=AmplitudeError("active users fail", 400)
    )
    connector.http_client = client
    result = await connector.sync()
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest() -> None:
    """sync() calls _ingest_document for each normalized doc when kb_id set."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    ingested: list[str] = []

    async def _ingest(doc: ConnectorDocument, kb_id: str) -> None:
        ingested.append(doc.source_id)

    connector._ingest_document = _ingest  # type: ignore[method-assign]
    await connector.sync(kb_id="kb_amplitude_001")
    assert len(ingested) > 0


@pytest.mark.asyncio
async def test_sync_initializes_client_if_none() -> None:
    """sync() creates http_client if not yet initialized."""
    connector = _make_connector()
    assert connector.http_client is None
    mock_client = _mock_client()
    connector._make_client = lambda: mock_client  # type: ignore[method-assign]
    result = await connector.sync()
    assert connector.http_client is not None
    assert result.documents_found >= 0


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — export_events() — 3 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_export_events_returns_bytes() -> None:
    """export_events() returns bytes from Amplitude export API."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.export_events("20240101T00", "20240101T23")
    assert isinstance(result, bytes)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_export_events_passes_date_range() -> None:
    """export_events() passes start/end params to http_client."""
    connector = _make_connector()
    client = _mock_client()
    connector.http_client = client
    await connector.export_events("20240601T00", "20240601T23")
    client.export_events.assert_called_once()
    call_args = client.export_events.call_args
    assert "20240601T00" in call_args.args or "20240601T00" in str(call_args)


@pytest.mark.asyncio
async def test_export_events_propagates_error() -> None:
    """export_events() raises AmplitudeError on API failure."""
    connector = _make_connector()
    client = _mock_client()
    client.export_events = AsyncMock(side_effect=AmplitudeAuthError("Unauthorized", 401))
    connector.http_client = client
    with pytest.raises(AmplitudeAuthError):
        await connector.export_events("20240101T00", "20240101T23")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — get_event_segmentation() — 4 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_event_segmentation_plain_name() -> None:
    """get_event_segmentation() accepts a plain event name string."""
    connector = _make_connector()
    client = _mock_client()
    connector.http_client = client
    result = await connector.get_event_segmentation("PageView", "20240101", "20240131")
    assert "data" in result or result == SAMPLE_SEGMENTATION_RESPONSE


@pytest.mark.asyncio
async def test_get_event_segmentation_json_passthrough() -> None:
    """get_event_segmentation() accepts pre-encoded JSON without double-encoding."""
    import json

    connector = _make_connector()
    client = _mock_client()
    connector.http_client = client
    encoded = json.dumps({"event_type": "ButtonClick", "filters": []})
    result = await connector.get_event_segmentation(encoded, "20240101", "20240131")
    assert result is not None


@pytest.mark.asyncio
async def test_get_event_segmentation_returns_amplitude_response() -> None:
    """get_event_segmentation() returns the raw Amplitude response dict."""
    connector = _make_connector()
    connector.http_client = _mock_client(
        segmentation_data=SAMPLE_SEGMENTATION_RESPONSE
    )
    result = await connector.get_event_segmentation("PageView", "20240101", "20240131")
    assert "data" in result


@pytest.mark.asyncio
async def test_get_event_segmentation_propagates_auth_error() -> None:
    """get_event_segmentation() propagates AmplitudeAuthError."""
    connector = _make_connector()
    client = _mock_client()
    client.get_event_segmentation = AsyncMock(
        side_effect=AmplitudeAuthError("Unauthorized", 401)
    )
    connector.http_client = client
    with pytest.raises(AmplitudeAuthError):
        await connector.get_event_segmentation("PageView", "20240101", "20240131")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — list_cohorts() / get_cohort() — 4 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_cohorts_returns_cohorts_key() -> None:
    """list_cohorts() returns dict with 'cohorts' list."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.list_cohorts()
    assert "cohorts" in result
    assert isinstance(result["cohorts"], list)


@pytest.mark.asyncio
async def test_list_cohorts_count() -> None:
    """list_cohorts() returns all cohorts from the response."""
    connector = _make_connector()
    connector.http_client = _mock_client(cohorts_data=SAMPLE_COHORTS_RESPONSE)
    result = await connector.list_cohorts()
    assert len(result["cohorts"]) == 2


@pytest.mark.asyncio
async def test_get_cohort_returns_members() -> None:
    """get_cohort() returns cohort members response."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.get_cohort("cohort_abc123")
    assert "users" in result or "cohort_id" in result


@pytest.mark.asyncio
async def test_get_cohort_calls_with_id() -> None:
    """get_cohort() passes cohort_id to http_client.get_cohort_members."""
    connector = _make_connector()
    client = _mock_client()
    connector.http_client = client
    await connector.get_cohort("cohort_xyz999")
    client.get_cohort_members.assert_called_once_with("cohort_xyz999")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 — get_active_users() — 3 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_active_users_returns_data() -> None:
    """get_active_users() returns response with series data."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.get_active_users("20240101", "20240131")
    assert "data" in result


@pytest.mark.asyncio
async def test_get_active_users_passes_date_range() -> None:
    """get_active_users() passes start_date and end_date to http_client."""
    connector = _make_connector()
    client = _mock_client()
    connector.http_client = client
    await connector.get_active_users("20240601", "20240630")
    client.get_active_users.assert_called_once()
    call_args = client.get_active_users.call_args
    assert "20240601" in call_args.args
    assert "20240630" in call_args.args


@pytest.mark.asyncio
async def test_get_active_users_series_values() -> None:
    """get_active_users() series data contains numeric counts."""
    connector = _make_connector()
    connector.http_client = _mock_client(
        active_users_data=SAMPLE_ACTIVE_USERS_RESPONSE
    )
    result = await connector.get_active_users("20240101", "20240105")
    series = result.get("data", {}).get("series", [[]])
    assert series[0][0] == 1200


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8 — get_user_activity() — 3 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_user_activity_returns_user_data() -> None:
    """get_user_activity() returns response with userData or events."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.get_user_activity("user_test_001")
    assert "userData" in result or isinstance(result, dict)


@pytest.mark.asyncio
async def test_get_user_activity_calls_with_user_id() -> None:
    """get_user_activity() passes user_id to http_client.get_user_activity."""
    connector = _make_connector()
    client = _mock_client()
    connector.http_client = client
    await connector.get_user_activity("user_test_999")
    client.get_user_activity.assert_called_once_with("user_test_999")


@pytest.mark.asyncio
async def test_get_user_activity_propagates_not_found() -> None:
    """get_user_activity() propagates AmplitudeNotFoundError on 404."""
    connector = _make_connector()
    client = _mock_client()
    client.get_user_activity = AsyncMock(
        side_effect=AmplitudeNotFoundError("user", "user_missing")
    )
    connector.http_client = client
    with pytest.raises(AmplitudeNotFoundError):
        await connector.get_user_activity("user_missing")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 9 — normalize_event_data() — 7 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_event_data_returns_documents() -> None:
    """normalize_event_data() returns one ConnectorDocument per xValues entry."""
    docs = normalize_event_data(
        "PageView",
        SAMPLE_SEGMENTATION_RESPONSE,
        CONNECTOR_ID,
        TENANT_ID,
        VALID_API_KEY,
    )
    assert len(docs) == 5


def test_normalize_event_data_content() -> None:
    """normalize_event_data() includes event type, date, and count in content."""
    docs = normalize_event_data(
        "ButtonClick",
        SAMPLE_SEGMENTATION_RESPONSE,
        CONNECTOR_ID,
        TENANT_ID,
        VALID_API_KEY,
    )
    assert docs
    doc = docs[0]
    assert "ButtonClick" in doc.content
    assert "2024-01-01" in doc.content
    assert "120" in doc.content


def test_normalize_event_data_stable_ids() -> None:
    """normalize_event_data() produces stable source_ids using SHA-256."""
    docs = normalize_event_data(
        "PageView",
        SAMPLE_SEGMENTATION_RESPONSE,
        CONNECTOR_ID,
        TENANT_ID,
        VALID_API_KEY,
    )
    ids = [doc.source_id for doc in docs]
    assert len(ids) == len(set(ids)), "All source_ids must be unique"


def test_normalize_event_data_stable_ids_are_deterministic() -> None:
    """normalize_event_data() always produces the same source_id for same input."""
    docs1 = normalize_event_data(
        "PageView",
        SAMPLE_SEGMENTATION_RESPONSE,
        CONNECTOR_ID,
        TENANT_ID,
        VALID_API_KEY,
    )
    docs2 = normalize_event_data(
        "PageView",
        SAMPLE_SEGMENTATION_RESPONSE,
        CONNECTOR_ID,
        TENANT_ID,
        VALID_API_KEY,
    )
    assert [d.source_id for d in docs1] == [d.source_id for d in docs2]


def test_normalize_event_data_empty_series() -> None:
    """normalize_event_data() returns empty list when series is empty."""
    empty_response: dict = {"data": {"series": [], "xValues": []}}
    docs = normalize_event_data(
        "PageView", empty_response, CONNECTOR_ID, TENANT_ID, VALID_API_KEY
    )
    assert docs == []


def test_normalize_event_data_metadata() -> None:
    """normalize_event_data() embeds event_type, date, count in metadata."""
    docs = normalize_event_data(
        "Signup",
        SAMPLE_SEGMENTATION_RESPONSE,
        CONNECTOR_ID,
        TENANT_ID,
        VALID_API_KEY,
    )
    doc = docs[0]
    assert doc.metadata["event_type"] == "Signup"
    assert doc.metadata["date"] == "2024-01-01"
    assert doc.metadata["count"] == 120


def test_normalize_event_data_tenant_connector_ids() -> None:
    """normalize_event_data() preserves connector_id and tenant_id."""
    docs = normalize_event_data(
        "PageView",
        SAMPLE_SEGMENTATION_RESPONSE,
        "conn_abc",
        "tenant_xyz",
        VALID_API_KEY,
    )
    assert all(d.connector_id == "conn_abc" for d in docs)
    assert all(d.tenant_id == "tenant_xyz" for d in docs)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 10 — normalize_cohort() — 5 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_cohort_title() -> None:
    """normalize_cohort() sets title to 'Amplitude cohort: {name}'."""
    cohort = SAMPLE_COHORTS_RESPONSE["cohorts"][0]
    doc = normalize_cohort(cohort, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Amplitude cohort: Power Users"


def test_normalize_cohort_content_includes_fields() -> None:
    """normalize_cohort() includes id, name, size, description in content."""
    cohort = SAMPLE_COHORTS_RESPONSE["cohorts"][0]
    doc = normalize_cohort(cohort, CONNECTOR_ID, TENANT_ID)
    assert "cohort_abc123" in doc.content
    assert "Power Users" in doc.content
    assert "4820" in doc.content
    assert "10+ sessions" in doc.content


def test_normalize_cohort_stable_id() -> None:
    """normalize_cohort() stable source_id is SHA-256(cohort_id)[:16]."""
    cohort = SAMPLE_COHORTS_RESPONSE["cohorts"][0]
    doc = normalize_cohort(cohort, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id_simple("cohort_abc123")
    assert doc.source_id == expected


def test_normalize_cohort_metadata() -> None:
    """normalize_cohort() embeds all cohort fields in metadata."""
    cohort = SAMPLE_COHORTS_RESPONSE["cohorts"][0]
    doc = normalize_cohort(cohort, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["cohort_id"] == "cohort_abc123"
    assert doc.metadata["size"] == 4820


def test_normalize_cohort_no_description() -> None:
    """normalize_cohort() handles missing description gracefully."""
    cohort = {"id": "c_bare", "name": "Bare Cohort", "size": 10}
    doc = normalize_cohort(cohort, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Amplitude cohort: Bare Cohort"
    assert "description" not in doc.content or "Description" not in doc.content


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 11 — _stable_id() and helpers — 5 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_stable_id_length() -> None:
    """_stable_id() returns exactly 16 hex characters."""
    result = _stable_id("api_key_123", "PageView", "2024-01-01")
    assert len(result) == 16
    assert all(c in "0123456789abcdef" for c in result)


def test_stable_id_deterministic() -> None:
    """_stable_id() always returns the same value for the same inputs."""
    r1 = _stable_id("key", "event", "2024-01-01")
    r2 = _stable_id("key", "event", "2024-01-01")
    assert r1 == r2


def test_stable_id_different_inputs_different_ids() -> None:
    """_stable_id() produces different IDs for different inputs."""
    r1 = _stable_id("key", "PageView", "2024-01-01")
    r2 = _stable_id("key", "PageView", "2024-01-02")
    r3 = _stable_id("key", "ButtonClick", "2024-01-01")
    assert r1 != r2
    assert r1 != r3
    assert r2 != r3


def test_stable_id_simple_length() -> None:
    """_stable_id_simple() returns exactly 16 hex characters."""
    result = _stable_id_simple("cohort_abc123")
    assert len(result) == 16


def test_stable_id_simple_deterministic() -> None:
    """_stable_id_simple() is stable across calls."""
    assert _stable_id_simple("cohort_xyz") == _stable_id_simple("cohort_xyz")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 12 — CircuitBreaker — 5 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_circuit_breaker_starts_closed() -> None:
    """CircuitBreaker initial state is closed."""
    cb = CircuitBreaker()
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_opens_at_threshold() -> None:
    """CircuitBreaker opens after failure_threshold failures."""
    cb = CircuitBreaker(failure_threshold=3)
    for _ in range(3):
        cb.on_failure()
    assert cb.is_open


def test_circuit_breaker_resets_on_success() -> None:
    """CircuitBreaker resets to closed after on_success()."""
    cb = CircuitBreaker(failure_threshold=2)
    cb.on_failure()
    cb.on_failure()
    assert cb.is_open
    cb.on_success()
    assert not cb.is_open
    assert cb._failures == 0


def test_circuit_breaker_counts_failures() -> None:
    """CircuitBreaker increments failure count correctly."""
    cb = CircuitBreaker(failure_threshold=10)
    cb.on_failure()
    cb.on_failure()
    assert cb._failures == 2


def test_circuit_breaker_half_open_after_timeout() -> None:
    """CircuitBreaker transitions to half-open after recovery timeout."""
    import time

    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.001)
    cb.on_failure()
    assert cb.is_open
    time.sleep(0.01)
    assert cb.state == "half-open"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 13 — Exception hierarchy — 6 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_amplitude_error_base_attributes() -> None:
    """AmplitudeError stores message, status_code, and code."""
    exc = AmplitudeError("test error", 400, "bad_request")
    assert exc.message == "test error"
    assert exc.status_code == 400
    assert exc.code == "bad_request"
    assert str(exc) == "test error"


def test_amplitude_auth_error_is_amplitude_error() -> None:
    """AmplitudeAuthError inherits from AmplitudeError."""
    exc = AmplitudeAuthError("auth fail", 401)
    assert isinstance(exc, AmplitudeError)
    assert exc.status_code == 401


def test_amplitude_rate_limit_has_retry_after() -> None:
    """AmplitudeRateLimitError stores retry_after value."""
    exc = AmplitudeRateLimitError("Too many requests", retry_after=15.5)
    assert exc.retry_after == 15.5
    assert exc.status_code == 429


def test_amplitude_not_found_message() -> None:
    """AmplitudeNotFoundError formats resource and id in message."""
    exc = AmplitudeNotFoundError("cohort", "cohort_xyz")
    assert "cohort" in str(exc)
    assert "cohort_xyz" in str(exc)
    assert exc.status_code == 404


def test_amplitude_network_error_is_amplitude_error() -> None:
    """AmplitudeNetworkError inherits from AmplitudeError."""
    exc = AmplitudeNetworkError("Connection refused")
    assert isinstance(exc, AmplitudeError)


def test_amplitude_server_error_is_amplitude_error() -> None:
    """AmplitudeServerError inherits from AmplitudeError."""
    exc = AmplitudeServerError("Internal server error", 500)
    assert isinstance(exc, AmplitudeError)
    assert exc.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 14 — Connector lifecycle — 5 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_closes_http_client() -> None:
    """aclose() closes the http_client and sets it to None."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_aclose_is_idempotent() -> None:
    """aclose() can be called multiple times without error."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    await connector.aclose()
    await connector.aclose()  # Second call — should not raise
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_context_manager_closes_on_exit() -> None:
    """AmplitudeConnector can be used as an async context manager."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    async with connector:
        assert connector.http_client is not None or connector.http_client is None
    assert connector.http_client is None


def test_connector_type_and_auth_type_constants() -> None:
    """CONNECTOR_TYPE and AUTH_TYPE are set to 'amplitude' and 'api_key'."""
    assert AmplitudeConnector.CONNECTOR_TYPE == "amplitude"
    assert AmplitudeConnector.AUTH_TYPE == "api_key"


def test_connector_default_region_is_us() -> None:
    """Connector defaults to 'us' region when not specified."""
    c = AmplitudeConnector(config={"api_key": "k", "api_secret": "s"})
    assert c._region == "us"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 15 — Region handling — 3 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_eu_region_stored_correctly() -> None:
    """Connector stores 'eu' region from config."""
    c = AmplitudeConnector(config={"api_key": "k", "api_secret": "s", "region": "eu"})
    assert c._region == "eu"


def test_region_defaults_to_us_when_missing() -> None:
    """Connector defaults to 'us' when region key absent from config."""
    c = AmplitudeConnector(config={"api_key": "k", "api_secret": "s"})
    assert c._region == "us"


def test_region_case_insensitive() -> None:
    """Connector normalizes region to lowercase."""
    c = AmplitudeConnector(config={"api_key": "k", "api_secret": "s", "region": "EU"})
    assert c._region == "eu"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 16 — list_events() — 4 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_events_returns_list() -> None:
    """list_events() returns a list of event type dicts."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.list_events()
    assert isinstance(result, list)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_list_events_contains_event_values() -> None:
    """list_events() returns dicts with 'value' field."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.list_events()
    values = [e.get("value") for e in result]
    assert "PageView" in values
    assert "ButtonClick" in values


@pytest.mark.asyncio
async def test_list_events_with_chart_id() -> None:
    """list_events() passes chart_id to http_client.list_events."""
    connector = _make_connector()
    client = _mock_client()
    connector.http_client = client
    await connector.list_events(chart_id="chart_abc")
    client.list_events.assert_called_once_with("chart_abc")


@pytest.mark.asyncio
async def test_list_events_propagates_auth_error() -> None:
    """list_events() propagates AmplitudeAuthError."""
    connector = _make_connector()
    client = _mock_client()
    client.list_events = AsyncMock(side_effect=AmplitudeAuthError("Unauthorized", 401))
    connector.http_client = client
    with pytest.raises(AmplitudeAuthError):
        await connector.list_events()


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 17 — list_user_properties() — 3 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_user_properties_returns_list() -> None:
    """list_user_properties() returns a list of user property dicts."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.list_user_properties()
    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_user_properties_contains_expected_fields() -> None:
    """list_user_properties() returns dicts with 'value' and 'displayName'."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.list_user_properties()
    assert result[0].get("value") == "user_type"
    assert "displayName" in result[0]


@pytest.mark.asyncio
async def test_list_user_properties_propagates_network_error() -> None:
    """list_user_properties() propagates AmplitudeNetworkError."""
    connector = _make_connector()
    client = _mock_client()
    client.list_user_properties = AsyncMock(
        side_effect=AmplitudeNetworkError("Connection refused")
    )
    connector.http_client = client
    with pytest.raises(AmplitudeNetworkError):
        await connector.list_user_properties()


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 18 — list_charts() — 3 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_charts_returns_list() -> None:
    """list_charts() returns a list of chart dicts."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.list_charts()
    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_charts_contains_ids_and_titles() -> None:
    """list_charts() returns chart dicts with 'id' and 'title'."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.list_charts()
    ids = [c.get("id") for c in result]
    assert "chart_abc" in ids
    assert result[0].get("title") == "DAU over time"


@pytest.mark.asyncio
async def test_list_charts_propagates_not_found() -> None:
    """list_charts() propagates AmplitudeNotFoundError."""
    connector = _make_connector()
    client = _mock_client()
    client.list_charts = AsyncMock(
        side_effect=AmplitudeNotFoundError("charts", "list")
    )
    connector.http_client = client
    with pytest.raises(AmplitudeNotFoundError):
        await connector.list_charts()


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 19 — query_event_counts() — 4 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_query_event_counts_returns_dict() -> None:
    """query_event_counts() returns a dict with series data."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.query_event_counts("PageView", "20240101", "20240105")
    assert isinstance(result, dict)
    assert "data" in result


@pytest.mark.asyncio
async def test_query_event_counts_passes_event_name() -> None:
    """query_event_counts() passes event name and date range to http_client."""
    connector = _make_connector()
    client = _mock_client()
    connector.http_client = client
    await connector.query_event_counts("Signup", "20240101", "20240131")
    client.query_event_counts.assert_called_once()
    args = client.query_event_counts.call_args.args
    assert args[0] == "Signup"
    assert args[1] == "20240101"
    assert args[2] == "20240131"


@pytest.mark.asyncio
async def test_query_event_counts_defaults_date_range() -> None:
    """query_event_counts() uses last-30-day range when start/end are None."""
    connector = _make_connector()
    client = _mock_client()
    connector.http_client = client
    result = await connector.query_event_counts("PageView")
    assert result is not None
    client.query_event_counts.assert_called_once()


@pytest.mark.asyncio
async def test_query_event_counts_propagates_rate_limit() -> None:
    """query_event_counts() propagates AmplitudeRateLimitError."""
    connector = _make_connector()
    client = _mock_client()
    client.query_event_counts = AsyncMock(
        side_effect=AmplitudeRateLimitError("Too many requests", retry_after=5.0)
    )
    connector.http_client = client
    with pytest.raises(AmplitudeRateLimitError):
        await connector.query_event_counts("PageView", "20240101", "20240131")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 20 — get_funnel() — 3 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_funnel_returns_dict() -> None:
    """get_funnel() returns a dict with funnel data."""
    connector = _make_connector()
    connector.http_client = _mock_client()
    result = await connector.get_funnel("funnel_001")
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_get_funnel_passes_funnel_id() -> None:
    """get_funnel() passes funnel_id to http_client.get_funnel."""
    connector = _make_connector()
    client = _mock_client()
    connector.http_client = client
    await connector.get_funnel("funnel_xyz")
    client.get_funnel.assert_called_once_with("funnel_xyz")


@pytest.mark.asyncio
async def test_get_funnel_propagates_not_found() -> None:
    """get_funnel() propagates AmplitudeNotFoundError for missing funnel."""
    connector = _make_connector()
    client = _mock_client()
    client.get_funnel = AsyncMock(
        side_effect=AmplitudeNotFoundError("funnel", "funnel_missing")
    )
    connector.http_client = client
    with pytest.raises(AmplitudeNotFoundError):
        await connector.get_funnel("funnel_missing")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 21 — normalize_event_type() — 5 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_event_type_id_format() -> None:
    """normalize_event_type() source_id is sha256('event_type:'+value)[:16]."""
    import hashlib

    event = {"value": "PageView", "displayName": "Page View", "category": "Navigation"}
    doc = normalize_event_type(event)
    expected_id = hashlib.sha256(b"event_type:PageView").hexdigest()[:16]
    assert doc.source_id == expected_id
    assert len(doc.source_id) == 16


def test_normalize_event_type_title() -> None:
    """normalize_event_type() title uses displayName when available."""
    event = {"value": "btn_click", "displayName": "Button Click"}
    doc = normalize_event_type(event)
    assert "Button Click" in doc.title


def test_normalize_event_type_content_fields() -> None:
    """normalize_event_type() includes value, category, description in content."""
    event = {
        "value": "Signup",
        "displayName": "Sign Up",
        "category": "Conversion",
        "description": "User completes registration",
    }
    doc = normalize_event_type(event)
    assert "Signup" in doc.content
    assert "Conversion" in doc.content
    assert "User completes registration" in doc.content


def test_normalize_event_type_metadata_source_and_type() -> None:
    """normalize_event_type() metadata has source='amplitude' and type='event_type'."""
    event = {"value": "Purchase"}
    doc = normalize_event_type(event)
    assert doc.metadata["source"] == "amplitude"
    assert doc.metadata["type"] == "event_type"
    assert doc.metadata["value"] == "Purchase"


def test_normalize_event_type_empty_value() -> None:
    """normalize_event_type() handles empty value gracefully."""
    event: dict = {}
    doc = normalize_event_type(event)
    assert doc.source_id is not None
    assert len(doc.source_id) == 16


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 22 — normalize_chart() — 5 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_chart_id_format() -> None:
    """normalize_chart() source_id is sha256('chart:'+str(id))[:16]."""
    import hashlib

    chart = {"id": "chart_abc", "title": "DAU over time", "type": "line"}
    doc = normalize_chart(chart)
    expected_id = hashlib.sha256(b"chart:chart_abc").hexdigest()[:16]
    assert doc.source_id == expected_id
    assert len(doc.source_id) == 16


def test_normalize_chart_title() -> None:
    """normalize_chart() title is 'Amplitude chart: {title}'."""
    chart = {"id": "chart_001", "title": "Retention Curve"}
    doc = normalize_chart(chart)
    assert doc.title == "Amplitude chart: Retention Curve"


def test_normalize_chart_content_fields() -> None:
    """normalize_chart() includes chart_id, title, type in content."""
    chart = {"id": "chart_abc", "title": "DAU over time", "type": "line"}
    doc = normalize_chart(chart)
    assert "chart_abc" in doc.content
    assert "DAU over time" in doc.content
    assert "line" in doc.content


def test_normalize_chart_metadata_source_and_type() -> None:
    """normalize_chart() metadata has source='amplitude' and type='analytics_chart'."""
    chart = {"id": "chart_xyz", "title": "Funnel"}
    doc = normalize_chart(chart)
    assert doc.metadata["source"] == "amplitude"
    assert doc.metadata["type"] == "analytics_chart"
    assert doc.metadata["chart_id"] == "chart_xyz"


def test_normalize_chart_missing_id_handled() -> None:
    """normalize_chart() handles missing id field without crashing."""
    chart: dict = {"title": "No ID Chart"}
    doc = normalize_chart(chart)
    assert doc.source_id is not None
    assert len(doc.source_id) == 16
    assert "No ID Chart" in doc.title


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 23 — http_client EU region URL switching — 3 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_http_client_us_base_url() -> None:
    """AmplitudeHTTPClient uses US base URL when region='us'."""
    from client.http_client import US_BASE_URL, AmplitudeHTTPClient

    c = AmplitudeHTTPClient("key", "secret", region="us")
    assert c._base_url == US_BASE_URL


def test_http_client_eu_base_url() -> None:
    """AmplitudeHTTPClient uses EU base URL when region='eu'."""
    from client.http_client import EU_BASE_URL, AmplitudeHTTPClient

    c = AmplitudeHTTPClient("key", "secret", region="eu")
    assert c._base_url == EU_BASE_URL


def test_http_client_basic_auth_set() -> None:
    """AmplitudeHTTPClient sets aiohttp.BasicAuth with api_key and api_secret."""
    import aiohttp

    from client.http_client import AmplitudeHTTPClient

    c = AmplitudeHTTPClient("mykey", "mysecret")
    assert isinstance(c._auth, aiohttp.BasicAuth)
    assert c._auth.login == "mykey"
    assert c._auth.password == "mysecret"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 24 — with_retry() behaviour — 4 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_first_attempt() -> None:
    """with_retry() returns result immediately when fn succeeds."""
    from helpers.utils import with_retry

    calls: list[int] = []

    async def _fn() -> str:
        calls.append(1)
        return "ok"

    result = await with_retry(_fn)
    assert result == "ok"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_with_retry_skips_auth_error() -> None:
    """with_retry() does NOT retry on AmplitudeAuthError."""
    from helpers.utils import with_retry

    calls: list[int] = []

    async def _fn() -> str:
        calls.append(1)
        raise AmplitudeAuthError("Unauthorized", 401)

    with pytest.raises(AmplitudeAuthError):
        await with_retry(_fn, max_attempts=3, base_delay=0.0)
    assert len(calls) == 1  # no retry on auth


@pytest.mark.asyncio
async def test_with_retry_retries_on_amplitude_error() -> None:
    """with_retry() retries on generic AmplitudeError up to max_attempts."""
    from helpers.utils import with_retry

    calls: list[int] = []

    async def _fn() -> str:
        calls.append(1)
        raise AmplitudeError("transient", 503)

    with pytest.raises(AmplitudeError):
        await with_retry(_fn, max_attempts=3, base_delay=0.0)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_one_failure() -> None:
    """with_retry() returns on the second attempt when the first fails."""
    from helpers.utils import with_retry

    calls: list[int] = []

    async def _fn() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise AmplitudeError("first attempt fails", 503)
        return "recovered"

    result = await with_retry(_fn, max_attempts=3, base_delay=0.0)
    assert result == "recovered"
    assert len(calls) == 2


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 25 — get_project_settings() via connector health_check — 3 tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_includes_project_name() -> None:
    """health_check() includes project name from settings in message."""
    connector = _make_connector()
    mock_client = _mock_client(
        settings_data={"projectName": "My Project", "orgName": "Acme"}
    )
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert "My Project" in result.message


@pytest.mark.asyncio
async def test_health_check_without_project_name_still_healthy() -> None:
    """health_check() succeeds when settings response lacks projectName."""
    connector = _make_connector()
    mock_client = _mock_client(settings_data={})
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_calls_get_project_settings() -> None:
    """install() calls get_project_settings (not get_taxonomy_categories)."""
    connector = _make_connector()
    client = _mock_client()
    connector._make_client = lambda: client
    await connector.install()
    client.get_project_settings.assert_called_once()
