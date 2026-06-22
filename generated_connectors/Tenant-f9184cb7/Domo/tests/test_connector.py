"""Tests for the Domo connector — no live API calls.

Coverage:
    exceptions (5+)          models (8+)             normalize_dataset (7+)
    normalize_page (6+)      normalize_user (6+)     with_retry (6+)
    HTTP client (16+)        install (4+)             health_check (5+)
    sync (8+)                list_* / get_* (6+)     pagination (4+)
    stable IDs (3+)
"""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    DomoAuthError,
    DomoError,
    DomoNetworkError,
    DomoNotFoundError,
    DomoRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    DomoDataset,
    DomoGroup,
    DomoPage,
    DomoResourceType,
    DomoUser,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
    TokenResponse,
)
from helpers.utils import (
    _stable_id,
    normalize_dataset,
    normalize_page,
    normalize_user,
    with_retry,
)
from client.http_client import DomoHTTPClient
from connector import DomoConnector, AUTH_TYPE, CONNECTOR_TYPE

# ── constants ─────────────────────────────────────────────────────────────────

TENANT = "Tenant-f9184cb7"
CONNECTOR_ID = "domo_test"
CLIENT_ID = "test_client_id_abc"
CLIENT_SECRET = "test_client_secret_xyz"
ACCESS_TOKEN = "domo_access_token_123"

DATASET_ID = "dataset-abc123"
PAGE_ID = 42
USER_ID = 100
GROUP_ID = 7


# ── factories ─────────────────────────────────────────────────────────────────

def _make_dataset(
    dataset_id: str = DATASET_ID,
    name: str = "Sales Data",
    rows: int = 5000,
    columns: int = 12,
) -> Dict[str, Any]:
    return {
        "id": dataset_id,
        "name": name,
        "description": "Monthly sales figures",
        "rows": rows,
        "columns": columns,
        "createdAt": "2024-01-15T08:00:00Z",
        "updatedAt": "2024-06-01T12:00:00Z",
        "owner": {"id": USER_ID, "name": "Alice Smith"},
        "status": "SUCCESS",
        "dataSource": {"type": "CSV"},
    }


def _make_page(
    page_id: int = PAGE_ID,
    name: str = "Executive Dashboard",
    parent_id: int = None,
    card_count: int = 8,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "id": page_id,
        "name": name,
        "cardCount": card_count,
        "visibility": "PUBLIC",
        "collectionIds": [],
    }
    if parent_id is not None:
        result["parentId"] = parent_id
    return result


def _make_user(
    user_id: int = USER_ID,
    name: str = "Alice Smith",
    email: str = "alice@example.com",
    role: str = "Admin",
) -> Dict[str, Any]:
    return {
        "id": user_id,
        "name": name,
        "email": email,
        "role": role,
        "title": "Head of Analytics",
        "department": "Business Intelligence",
        "phone": "+1-555-0100",
        "location": "New York",
        "createdAt": "2023-03-01T09:00:00Z",
    }


def _make_group(
    group_id: int = GROUP_ID,
    name: str = "Data Team",
    member_count: int = 5,
) -> Dict[str, Any]:
    return {
        "id": group_id,
        "name": name,
        "memberCount": member_count,
        "default": False,
        "active": True,
    }


def _make_token_response() -> Dict[str, Any]:
    return {
        "access_token": ACCESS_TOKEN,
        "expires_in": 3600,
        "token_type": "bearer",
    }


def _make_connector(
    config: Dict[str, Any] | None = None,
) -> DomoConnector:
    cfg = config or {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    return DomoConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )


# ── exception tests ───────────────────────────────────────────────────────────

class TestExceptions:
    def test_domo_error_is_exception(self) -> None:
        exc = DomoError("base error")
        assert isinstance(exc, Exception)
        assert str(exc) == "base error"

    def test_auth_error_inherits_domo_error(self) -> None:
        exc = DomoAuthError("401 unauthorized")
        assert isinstance(exc, DomoError)

    def test_network_error_inherits_domo_error(self) -> None:
        exc = DomoNetworkError("connection refused")
        assert isinstance(exc, DomoError)

    def test_not_found_error_inherits_domo_error(self) -> None:
        exc = DomoNotFoundError("dataset not found")
        assert isinstance(exc, DomoError)

    def test_rate_limit_error_inherits_domo_error(self) -> None:
        exc = DomoRateLimitError("429 too many requests")
        assert isinstance(exc, DomoError)

    def test_error_hierarchy_distinct(self) -> None:
        assert DomoAuthError is not DomoNetworkError
        assert DomoNotFoundError is not DomoRateLimitError


# ── model tests ───────────────────────────────────────────────────────────────

class TestModels:
    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(id="abc123", title="Test", content="Body")
        assert doc.type == "domo_resource"
        assert doc.metadata == {}

    def test_connector_document_custom_type(self) -> None:
        doc = ConnectorDocument(id="x", title="T", content="C", type="dataset")
        assert doc.type == "dataset"

    def test_install_result(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.MISSING_CREDENTIALS,
            connector_id="c1",
            message="Missing client_id",
        )
        assert r.health == ConnectorHealth.OFFLINE
        assert r.auth_status == AuthStatus.MISSING_CREDENTIALS

    def test_health_check_result(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="OK",
        )
        assert r.health == ConnectorHealth.HEALTHY

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_sync_status_enum(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_auth_status_enum(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_domo_resource_type_enum(self) -> None:
        assert DomoResourceType.DATASET == "dataset"
        assert DomoResourceType.PAGE == "page"
        assert DomoResourceType.USER == "user"
        assert DomoResourceType.GROUP == "group"

    def test_token_response(self) -> None:
        t = TokenResponse(access_token="tok", expires_in=3600)
        assert t.access_token == "tok"
        assert t.token_type == "bearer"

    def test_domo_dataset_dataclass(self) -> None:
        d = DomoDataset(id="ds1", name="Revenue", row_count=100)
        assert d.id == "ds1"
        assert d.owner == {}

    def test_domo_user_dataclass(self) -> None:
        u = DomoUser(id=1, name="Bob", email="bob@co.com")
        assert u.role == ""

    def test_domo_page_dataclass(self) -> None:
        p = DomoPage(id=5, name="Ops Dashboard")
        assert p.parent_id is None

    def test_domo_group_dataclass(self) -> None:
        g = DomoGroup(id=2, name="Analysts")
        assert g.active is True


# ── stable ID tests ───────────────────────────────────────────────────────────

class TestStableId:
    def test_stable_id_deterministic(self) -> None:
        id1 = _stable_id("dataset", "abc123")
        id2 = _stable_id("dataset", "abc123")
        assert id1 == id2

    def test_stable_id_length_16(self) -> None:
        sid = _stable_id("page", "42")
        assert len(sid) == 16

    def test_stable_id_prefix_changes_output(self) -> None:
        ds_id = _stable_id("dataset", "1")
        pg_id = _stable_id("page", "1")
        assert ds_id != pg_id

    def test_stable_id_matches_sha256(self) -> None:
        expected = hashlib.sha256("user:100".encode()).hexdigest()[:16]
        assert _stable_id("user", "100") == expected


# ── normalize_dataset tests ───────────────────────────────────────────────────

class TestNormalizeDataset:
    def test_basic_fields(self) -> None:
        raw = _make_dataset()
        doc = normalize_dataset(raw)
        assert doc.title == "Sales Data"
        assert doc.type == "dataset"

    def test_stable_id_formula(self) -> None:
        raw = _make_dataset(dataset_id="ds-xyz")
        doc = normalize_dataset(raw)
        expected = hashlib.sha256("dataset:ds-xyz".encode()).hexdigest()[:16]
        assert doc.id == expected

    def test_metadata_contains_resource_type(self) -> None:
        raw = _make_dataset()
        doc = normalize_dataset(raw)
        assert doc.metadata["resource_type"] == "dataset"
        assert doc.metadata["source"] == "domo"

    def test_metadata_row_column_counts(self) -> None:
        raw = _make_dataset(rows=1000, columns=25)
        doc = normalize_dataset(raw)
        assert doc.metadata["row_count"] == 1000
        assert doc.metadata["column_count"] == 25

    def test_owner_name_in_content(self) -> None:
        raw = _make_dataset()
        doc = normalize_dataset(raw)
        assert "Alice Smith" in doc.content

    def test_description_in_content(self) -> None:
        raw = _make_dataset()
        doc = normalize_dataset(raw)
        assert "Monthly sales figures" in doc.content

    def test_missing_description_ok(self) -> None:
        raw = _make_dataset()
        raw.pop("description")
        doc = normalize_dataset(raw)
        assert isinstance(doc, ConnectorDocument)

    def test_alt_field_names_rowcount(self) -> None:
        raw = {"id": "d1", "name": "Test", "rowCount": 99, "columnCount": 5}
        doc = normalize_dataset(raw)
        assert doc.metadata["row_count"] == 99
        assert doc.metadata["column_count"] == 5


# ── normalize_page tests ──────────────────────────────────────────────────────

class TestNormalizePage:
    def test_basic_fields(self) -> None:
        raw = _make_page()
        doc = normalize_page(raw)
        assert doc.title == "Executive Dashboard"
        assert doc.type == "dashboard"

    def test_stable_id_formula(self) -> None:
        raw = _make_page(page_id=42)
        doc = normalize_page(raw)
        expected = hashlib.sha256("page:42".encode()).hexdigest()[:16]
        assert doc.id == expected

    def test_metadata_resource_type(self) -> None:
        raw = _make_page()
        doc = normalize_page(raw)
        assert doc.metadata["resource_type"] == "dashboard"
        assert doc.metadata["source"] == "domo"

    def test_parent_id_in_metadata(self) -> None:
        raw = _make_page(parent_id=10)
        doc = normalize_page(raw)
        assert doc.metadata["parent_id"] == "10"

    def test_no_parent_id(self) -> None:
        raw = _make_page()
        doc = normalize_page(raw)
        assert doc.metadata["parent_id"] == ""

    def test_card_count_in_metadata(self) -> None:
        raw = _make_page(card_count=15)
        doc = normalize_page(raw)
        assert doc.metadata["card_count"] == 15

    def test_content_contains_dashboard_name(self) -> None:
        raw = _make_page()
        doc = normalize_page(raw)
        assert "Dashboard: Executive Dashboard" in doc.content


# ── normalize_user tests ──────────────────────────────────────────────────────

class TestNormalizeUser:
    def test_basic_fields(self) -> None:
        raw = _make_user()
        doc = normalize_user(raw)
        assert doc.title == "Alice Smith"
        assert doc.type == "user"

    def test_stable_id_formula(self) -> None:
        raw = _make_user(user_id=100)
        doc = normalize_user(raw)
        expected = hashlib.sha256("user:100".encode()).hexdigest()[:16]
        assert doc.id == expected

    def test_metadata_resource_type(self) -> None:
        raw = _make_user()
        doc = normalize_user(raw)
        assert doc.metadata["resource_type"] == "user"
        assert doc.metadata["source"] == "domo"

    def test_email_in_metadata_and_content(self) -> None:
        raw = _make_user(email="alice@example.com")
        doc = normalize_user(raw)
        assert doc.metadata["email"] == "alice@example.com"
        assert "alice@example.com" in doc.content

    def test_role_in_content(self) -> None:
        raw = _make_user(role="Admin")
        doc = normalize_user(raw)
        assert "Admin" in doc.content

    def test_department_in_metadata(self) -> None:
        raw = _make_user()
        doc = normalize_user(raw)
        assert doc.metadata["department"] == "Business Intelligence"

    def test_missing_optional_fields(self) -> None:
        raw = {"id": 5, "name": "Bob"}
        doc = normalize_user(raw)
        assert doc.title == "Bob"
        assert doc.metadata["email"] == ""


# ── with_retry tests ──────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_succeeds_on_first_attempt(self) -> None:
        mock = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock, max_attempts=3)
        assert result == {"ok": True}
        assert mock.call_count == 1

    async def test_retries_on_domo_error(self) -> None:
        mock = AsyncMock(side_effect=[DomoError("transient"), {"ok": True}])
        result = await with_retry(mock, max_attempts=3, base_delay=0.0)
        assert result == {"ok": True}
        assert mock.call_count == 2

    async def test_no_retry_on_auth_error(self) -> None:
        mock = AsyncMock(side_effect=DomoAuthError("401"))
        with pytest.raises(DomoAuthError):
            await with_retry(mock, max_attempts=3, base_delay=0.0)
        assert mock.call_count == 1

    async def test_no_retry_on_not_found_error(self) -> None:
        mock = AsyncMock(side_effect=DomoNotFoundError("404"))
        with pytest.raises(DomoNotFoundError):
            await with_retry(mock, max_attempts=3, base_delay=0.0)
        assert mock.call_count == 1

    async def test_raises_after_max_attempts(self) -> None:
        mock = AsyncMock(side_effect=DomoError("persistent"))
        with pytest.raises(DomoError, match="persistent"):
            await with_retry(mock, max_attempts=3, base_delay=0.0)
        assert mock.call_count == 3

    async def test_retries_on_generic_exception(self) -> None:
        mock = AsyncMock(side_effect=[RuntimeError("flaky"), {"ok": True}])
        result = await with_retry(mock, max_attempts=3, base_delay=0.0)
        assert result == {"ok": True}


# ── HTTP client tests ─────────────────────────────────────────────────────────

class TestDomoHTTPClient:
    def _client(self, token: str = "") -> DomoHTTPClient:
        return DomoHTTPClient(
            config={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "access_token": token,
            }
        )

    def test_basic_auth_header_format(self) -> None:
        client = self._client()
        header = client._basic_auth_header()
        expected_raw = f"{CLIENT_ID}:{CLIENT_SECRET}"
        expected = "Basic " + base64.b64encode(expected_raw.encode()).decode()
        assert header == expected

    def test_bearer_headers_with_token(self) -> None:
        client = self._client(token=ACCESS_TOKEN)
        headers = client._bearer_headers()
        assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"

    def test_raise_for_status_200_returns_body(self) -> None:
        client = self._client()
        body = [{"id": "1", "name": "Dataset"}]
        result = client._raise_for_status(200, body, "list_datasets")
        assert result == body

    def test_raise_for_status_401_raises_auth_error(self) -> None:
        client = self._client()
        with pytest.raises(DomoAuthError, match="401"):
            client._raise_for_status(401, {"message": "Unauthorized"}, "get_token")

    def test_raise_for_status_403_raises_auth_error(self) -> None:
        client = self._client()
        with pytest.raises(DomoAuthError, match="403"):
            client._raise_for_status(403, {"message": "Forbidden"}, "list_users")

    def test_raise_for_status_404_raises_not_found(self) -> None:
        client = self._client()
        with pytest.raises(DomoNotFoundError, match="404"):
            client._raise_for_status(404, {"message": "Not found"}, "get_dataset")

    def test_raise_for_status_429_raises_rate_limit(self) -> None:
        client = self._client()
        with pytest.raises(DomoRateLimitError, match="429"):
            client._raise_for_status(429, {"message": "Too many requests"}, "list_pages")

    def test_raise_for_status_500_raises_domo_error(self) -> None:
        client = self._client()
        with pytest.raises(DomoError, match="500"):
            client._raise_for_status(500, {"message": "Internal server error"}, "list_datasets")

    async def test_get_token_uses_basic_auth_and_stores_token(self) -> None:
        client = self._client()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=_make_token_response())
        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.get_token()

        assert result["access_token"] == ACCESS_TOKEN
        assert client._config["access_token"] == ACCESS_TOKEN
        # Verify Basic auth was passed
        call_kwargs = mock_session.get.call_args
        assert "headers" in call_kwargs.kwargs
        assert "Basic" in call_kwargs.kwargs["headers"]["Authorization"]

    async def test_list_datasets_uses_bearer_and_offset(self) -> None:
        client = self._client(token=ACCESS_TOKEN)
        datasets = [_make_dataset()]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=datasets)
        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.list_datasets(limit=50, offset=0)

        assert result == datasets
        call_kwargs = mock_session.get.call_args
        assert f"Bearer {ACCESS_TOKEN}" in call_kwargs.kwargs["headers"]["Authorization"]
        assert call_kwargs.kwargs["params"]["offset"] == 0
        assert call_kwargs.kwargs["params"]["limit"] == 50

    async def test_list_datasets_offset_pagination(self) -> None:
        client = self._client(token=ACCESS_TOKEN)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=[])
        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.list_datasets(limit=50, offset=100)

        call_kwargs = mock_session.get.call_args
        assert call_kwargs.kwargs["params"]["offset"] == 100

    async def test_get_dataset_returns_dict(self) -> None:
        client = self._client(token=ACCESS_TOKEN)
        ds = _make_dataset()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=ds)
        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.get_dataset(DATASET_ID)

        assert result["id"] == DATASET_ID

    async def test_list_pages_returns_list(self) -> None:
        client = self._client(token=ACCESS_TOKEN)
        pages = [_make_page()]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=pages)
        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.list_pages()

        assert result == pages

    async def test_list_users_returns_list(self) -> None:
        client = self._client(token=ACCESS_TOKEN)
        users = [_make_user()]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=users)
        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.list_users(limit=1)

        assert len(result) == 1
        assert result[0]["name"] == "Alice Smith"

    async def test_list_groups_returns_list(self) -> None:
        client = self._client(token=ACCESS_TOKEN)
        groups = [_make_group()]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=groups)
        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.list_groups()

        assert result == groups

    async def test_get_token_network_error_raises_domo_network_error(self) -> None:
        client = self._client()
        with patch("client.http_client.aiohttp.ClientSession") as MockSession:
            import aiohttp
            MockSession.return_value.__aenter__ = AsyncMock(
                side_effect=aiohttp.ClientError("timeout")
            )
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(DomoNetworkError):
                await client.get_token()

    async def test_list_datasets_wraps_dict_response_as_empty(self) -> None:
        # If API returns a dict instead of list, client returns []
        client = self._client(token=ACCESS_TOKEN)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"data": []})  # unexpected dict shape
        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.list_datasets()

        # dict is not a list → returns []
        assert result == []


# ── install tests ─────────────────────────────────────────────────────────────

class TestInstall:
    async def test_missing_client_id_returns_offline(self) -> None:
        conn = DomoConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"client_secret": CLIENT_SECRET},
        )
        result = await conn.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in result.message

    async def test_missing_client_secret_returns_offline(self) -> None:
        conn = DomoConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"client_id": CLIENT_ID},
        )
        result = await conn.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.OFFLINE
        assert "client_secret" in result.message

    async def test_both_missing_lists_all_fields(self) -> None:
        conn = DomoConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={},
        )
        result = await conn.install()
        assert "client_id" in result.message
        assert "client_secret" in result.message

    async def test_credentials_present_returns_healthy(self) -> None:
        conn = _make_connector()
        result = await conn.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED


# ── health_check tests ────────────────────────────────────────────────────────

class TestHealthCheck:
    async def test_healthy_when_token_and_users_ok(self) -> None:
        conn = _make_connector()
        conn.client.get_token = AsyncMock(return_value=_make_token_response())
        conn.client.list_users = AsyncMock(return_value=[_make_user()])
        result = await conn.health_check()
        assert isinstance(result, HealthCheckResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_calls_get_token(self) -> None:
        conn = _make_connector()
        conn.client.get_token = AsyncMock(return_value=_make_token_response())
        conn.client.list_users = AsyncMock(return_value=[])
        await conn.health_check()
        conn.client.get_token.assert_awaited_once()

    async def test_auth_error_returns_invalid_credentials(self) -> None:
        conn = _make_connector()
        conn.client.get_token = AsyncMock(side_effect=DomoAuthError("Invalid credentials"))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_network_error_returns_degraded_failed(self) -> None:
        conn = _make_connector()
        conn.client.get_token = AsyncMock(side_effect=DomoNetworkError("timeout"))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_message_contains_user_count(self) -> None:
        conn = _make_connector()
        conn.client.get_token = AsyncMock(return_value=_make_token_response())
        conn.client.list_users = AsyncMock(return_value=[_make_user()])
        result = await conn.health_check()
        assert "1 user(s)" in result.message


# ── sync tests ────────────────────────────────────────────────────────────────

class TestSync:
    def _mock_client(self, conn: DomoConnector) -> None:
        conn.client.get_token = AsyncMock(return_value=_make_token_response())

    async def test_sync_empty_returns_completed(self) -> None:
        conn = _make_connector()
        self._mock_client(conn)
        conn.client.list_datasets = AsyncMock(return_value=[])
        conn.client.list_pages = AsyncMock(return_value=[])
        result = await conn.sync()
        assert isinstance(result, SyncResult)
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_counts_datasets(self) -> None:
        conn = _make_connector()
        self._mock_client(conn)
        datasets = [_make_dataset("ds1"), _make_dataset("ds2")]
        conn.client.list_datasets = AsyncMock(return_value=datasets)
        conn.client.list_pages = AsyncMock(return_value=[])
        result = await conn.sync()
        # 2 datasets in single page (< 50) → stops after one batch
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_counts_pages(self) -> None:
        conn = _make_connector()
        self._mock_client(conn)
        conn.client.list_datasets = AsyncMock(return_value=[])
        pages = [_make_page(1), _make_page(2), _make_page(3)]
        conn.client.list_pages = AsyncMock(return_value=pages)
        result = await conn.sync()
        assert result.documents_found == 3
        assert result.documents_synced == 3

    async def test_sync_combined_datasets_and_pages(self) -> None:
        conn = _make_connector()
        self._mock_client(conn)
        conn.client.list_datasets = AsyncMock(return_value=[_make_dataset()])
        conn.client.list_pages = AsyncMock(return_value=[_make_page()])
        result = await conn.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_partial_on_normalization_failure(self) -> None:
        conn = _make_connector()
        self._mock_client(conn)
        # broken dataset missing "id"
        broken = {"name": "Broken"}
        conn.client.list_datasets = AsyncMock(return_value=[broken, _make_dataset()])
        conn.client.list_pages = AsyncMock(return_value=[])

        with patch("connector.normalize_dataset", side_effect=[RuntimeError("bad"), ConnectorDocument(id="x", title="T", content="C")]):
            result = await conn.sync()

        assert result.documents_failed >= 1
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_pagination_stops_on_short_batch(self) -> None:
        conn = _make_connector()
        self._mock_client(conn)
        # Return fewer than PAGE_SIZE → stops after one call
        conn.client.list_datasets = AsyncMock(return_value=[_make_dataset()])
        conn.client.list_pages = AsyncMock(return_value=[])
        result = await conn.sync()
        conn.client.list_datasets.assert_awaited_once()

    async def test_sync_full_pagination_two_pages(self) -> None:
        conn = _make_connector()
        self._mock_client(conn)
        # First call returns 50 items, second returns 1 → stops
        batch1 = [_make_dataset(f"ds{i}") for i in range(50)]
        batch2 = [_make_dataset("ds50")]
        conn.client.list_datasets = AsyncMock(side_effect=[batch1, batch2])
        conn.client.list_pages = AsyncMock(return_value=[])
        result = await conn.sync()
        assert result.documents_found == 51
        assert conn.client.list_datasets.await_count == 2

    async def test_sync_token_error_returns_failed(self) -> None:
        conn = _make_connector()
        conn.client.get_token = AsyncMock(side_effect=DomoAuthError("bad creds"))
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED


# ── list_* / get_* tests ──────────────────────────────────────────────────────

class TestQueryMethods:
    async def test_list_datasets_auto_paginates(self) -> None:
        conn = _make_connector()
        batch1 = [_make_dataset(f"ds{i}") for i in range(50)]
        batch2 = [_make_dataset("ds50")]
        conn.client.list_datasets = AsyncMock(side_effect=[batch1, batch2])
        result = await conn.list_datasets()
        assert len(result) == 51
        assert conn.client.list_datasets.await_count == 2

    async def test_list_pages_returns_all(self) -> None:
        conn = _make_connector()
        conn.client.list_pages = AsyncMock(return_value=[_make_page(1), _make_page(2)])
        result = await conn.list_pages()
        assert len(result) == 2

    async def test_list_users_returns_all(self) -> None:
        conn = _make_connector()
        conn.client.list_users = AsyncMock(return_value=[_make_user()])
        result = await conn.list_users()
        assert len(result) == 1

    async def test_get_dataset_returns_dict(self) -> None:
        conn = _make_connector()
        conn.client.get_dataset = AsyncMock(return_value=_make_dataset())
        result = await conn.get_dataset(DATASET_ID)
        assert result["id"] == DATASET_ID

    async def test_get_page_returns_dict(self) -> None:
        conn = _make_connector()
        conn.client.get_page = AsyncMock(return_value=_make_page())
        result = await conn.get_page(PAGE_ID)
        assert result["id"] == PAGE_ID

    async def test_get_dataset_propagates_not_found(self) -> None:
        conn = _make_connector()
        conn.client.get_dataset = AsyncMock(side_effect=DomoNotFoundError("404"))
        with pytest.raises(DomoNotFoundError):
            await conn.get_dataset("nonexistent")


# ── connector meta tests ──────────────────────────────────────────────────────

class TestConnectorMeta:
    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "domo"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_class_attributes(self) -> None:
        assert DomoConnector.CONNECTOR_TYPE == "domo"
        assert DomoConnector.AUTH_TYPE == "api_key"
        assert DomoConnector.CONNECTOR_NAME == "Domo"

    def test_required_config_keys(self) -> None:
        assert "client_id" in DomoConnector.REQUIRED_CONFIG_KEYS
        assert "client_secret" in DomoConnector.REQUIRED_CONFIG_KEYS

    def test_connector_stores_config(self) -> None:
        conn = _make_connector()
        assert conn.config["client_id"] == CLIENT_ID
        assert conn.config["client_secret"] == CLIENT_SECRET

    def test_connector_stores_tenant_and_id(self) -> None:
        conn = _make_connector()
        assert conn.tenant_id == TENANT
        assert conn.connector_id == CONNECTOR_ID

    async def test_context_manager(self) -> None:
        async with DomoConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        ) as conn:
            assert isinstance(conn, DomoConnector)
