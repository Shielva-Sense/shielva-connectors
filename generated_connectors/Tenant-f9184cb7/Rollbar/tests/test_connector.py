"""Unit tests for RollbarConnector — all HTTP calls are mocked via AsyncMock.

Coverage targets:
  exceptions        (5 tests)
  models            (7 tests)
  normalize_item    (5 tests)
  normalize_occurrence (5 tests)
  normalize_deploy  (5 tests)
  with_retry        (6 tests)
  HTTP client       (15 tests)
  install           (5 tests)
  health_check      (5 tests)
  sync              (7 tests)
  list_items        (5 tests)
  list_occurrences  (3 tests)
  list_deploys      (3 tests)
  get_item          (3 tests)
  Total: 79 tests
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import RollbarConnector
from exceptions import (
    RollbarAuthError,
    RollbarError,
    RollbarNetworkError,
    RollbarNotFoundError,
    RollbarRateLimitError,
)
from helpers.utils import (
    normalize_deploy,
    normalize_item,
    normalize_occurrence,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ──────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_rollbar_test_001"
ACCESS_TOKEN = "rollbar_test_token_abc123XYZ"

SAMPLE_PROJECT: dict = {
    "id": 12345,
    "name": "my-backend",
    "account_id": 99,
    "status": "enabled",
}

SAMPLE_ITEM: dict = {
    "id": 987654,
    "title": "AttributeError: 'NoneType' object has no attribute 'split'",
    "level": "error",
    "status": "active",
    "environment": "production",
    "first_occurrence_timestamp": 1717200000,
    "last_occurrence_timestamp": 1718400000,
    "total_occurrences": 153,
    "resolved_in_version": "",
    "assigned_user_id": None,
}

SAMPLE_OCCURRENCE: dict = {
    "id": 111222333,
    "item_id": 987654,
    "timestamp": 1718400000,
    "environment": "production",
    "level": "error",
    "language": "python",
    "framework": "django",
    "body": {
        "trace": {
            "exception": {
                "class": "AttributeError",
                "message": "'NoneType' object has no attribute 'split'",
            }
        }
    },
}

SAMPLE_DEPLOY: dict = {
    "id": 55667788,
    "environment": "production",
    "revision": "abc1234def5678",
    "rollbar_username": "alice",
    "local_username": "",
    "comment": "Hotfix for auth bug",
    "status": "succeeded",
    "start_time": 1718390000,
    "finish_time": 1718390120,
}


def _make_connector(
    access_token: str = ACCESS_TOKEN,
    account_access_token: str = "",
) -> RollbarConnector:
    return RollbarConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "access_token": access_token,
            "account_access_token": account_access_token,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# Exceptions (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_rollbar_error_base(self) -> None:
        exc = RollbarError("base error", status_code=400, code="bad_request")
        assert str(exc) == "base error"
        assert exc.status_code == 400
        assert exc.code == "bad_request"

    def test_rollbar_auth_error_is_rollbar_error(self) -> None:
        exc = RollbarAuthError("auth failed", status_code=401, code="auth_error")
        assert isinstance(exc, RollbarError)
        assert exc.status_code == 401

    def test_rollbar_rate_limit_error_defaults(self) -> None:
        exc = RollbarRateLimitError("rate limited")
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 0.0

    def test_rollbar_not_found_error_message(self) -> None:
        exc = RollbarNotFoundError("item", "99999")
        assert "99999" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"

    def test_rollbar_network_error_is_rollbar_error(self) -> None:
        exc = RollbarNetworkError("timeout", status_code=504)
        assert isinstance(exc, RollbarError)
        assert exc.status_code == 504


# ══════════════════════════════════════════════════════════════════════════════
# Models (7 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_connector_health_values(self) -> None:
        from models import ConnectorHealth
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        from models import AuthStatus
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        from models import SyncStatus
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"
        assert SyncStatus.RUNNING == "running"

    def test_item_level_enum(self) -> None:
        from models import ItemLevel
        assert ItemLevel.ERROR == "error"
        assert ItemLevel.WARNING == "warning"
        assert ItemLevel.CRITICAL == "critical"

    def test_item_status_enum(self) -> None:
        from models import ItemStatus
        assert ItemStatus.ACTIVE == "active"
        assert ItemStatus.RESOLVED == "resolved"
        assert ItemStatus.MUTED == "muted"

    def test_install_result_fields(self) -> None:
        from models import InstallResult
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="c123",
            message="ok",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.connector_id == "c123"

    def test_connector_document_fields(self) -> None:
        from models import ConnectorDocument
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="content",
            connector_id="conn1",
            tenant_id="tenant1",
            source_url="https://example.com",
            metadata={"key": "val"},
        )
        assert doc.source_id == "abc123"
        assert doc.metadata["key"] == "val"


# ══════════════════════════════════════════════════════════════════════════════
# normalize_item (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestNormalizeItem:
    def test_basic_fields(self) -> None:
        doc = normalize_item(SAMPLE_ITEM)
        assert "AttributeError" in doc.title
        assert "error" in doc.content
        assert "production" in doc.content

    def test_source_id_is_16_chars_hex(self) -> None:
        doc = normalize_item(SAMPLE_ITEM)
        assert len(doc.source_id) == 16
        assert all(c in "0123456789abcdef" for c in doc.source_id)

    def test_source_id_stable(self) -> None:
        doc1 = normalize_item(SAMPLE_ITEM)
        doc2 = normalize_item(SAMPLE_ITEM)
        assert doc1.source_id == doc2.source_id

    def test_metadata_contains_item_fields(self) -> None:
        doc = normalize_item(SAMPLE_ITEM)
        assert doc.metadata["item_id"] == "987654"
        assert doc.metadata["level"] == "error"
        assert doc.metadata["status"] == "active"
        assert doc.metadata["environment"] == "production"
        assert doc.metadata["occurrence_count"] == 153

    def test_empty_item_fallback_title(self) -> None:
        doc = normalize_item({})
        assert "Item" in doc.title
        assert len(doc.source_id) == 16


# ══════════════════════════════════════════════════════════════════════════════
# normalize_occurrence (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestNormalizeOccurrence:
    def test_basic_fields(self) -> None:
        doc = normalize_occurrence(SAMPLE_OCCURRENCE)
        assert "AttributeError" in doc.title
        assert "django" in doc.content

    def test_source_id_is_16_chars_hex(self) -> None:
        doc = normalize_occurrence(SAMPLE_OCCURRENCE)
        assert len(doc.source_id) == 16
        assert all(c in "0123456789abcdef" for c in doc.source_id)

    def test_source_id_stable(self) -> None:
        doc1 = normalize_occurrence(SAMPLE_OCCURRENCE)
        doc2 = normalize_occurrence(SAMPLE_OCCURRENCE)
        assert doc1.source_id == doc2.source_id

    def test_metadata_contains_occurrence_fields(self) -> None:
        doc = normalize_occurrence(SAMPLE_OCCURRENCE)
        assert doc.metadata["occurrence_id"] == "111222333"
        assert doc.metadata["item_id"] == "987654"
        assert doc.metadata["environment"] == "production"
        assert doc.metadata["exc_class"] == "AttributeError"
        assert doc.metadata["language"] == "python"

    def test_empty_occurrence_fallback_title(self) -> None:
        doc = normalize_occurrence({})
        assert "Occurrence" in doc.title
        assert len(doc.source_id) == 16


# ══════════════════════════════════════════════════════════════════════════════
# normalize_deploy (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestNormalizeDeploy:
    def test_basic_fields(self) -> None:
        doc = normalize_deploy(SAMPLE_DEPLOY)
        assert "55667788" in doc.title or "production" in doc.title
        assert "Hotfix" in doc.content

    def test_source_id_is_16_chars_hex(self) -> None:
        doc = normalize_deploy(SAMPLE_DEPLOY)
        assert len(doc.source_id) == 16
        assert all(c in "0123456789abcdef" for c in doc.source_id)

    def test_source_id_stable(self) -> None:
        doc1 = normalize_deploy(SAMPLE_DEPLOY)
        doc2 = normalize_deploy(SAMPLE_DEPLOY)
        assert doc1.source_id == doc2.source_id

    def test_metadata_contains_deploy_fields(self) -> None:
        doc = normalize_deploy(SAMPLE_DEPLOY)
        assert doc.metadata["deploy_id"] == "55667788"
        assert doc.metadata["environment"] == "production"
        assert doc.metadata["revision"] == "abc1234def5678"
        assert doc.metadata["deployer"] == "alice"
        assert doc.metadata["status"] == "succeeded"

    def test_empty_deploy_fallback_title(self) -> None:
        doc = normalize_deploy({})
        assert "Deploy" in doc.title
        assert len(doc.source_id) == 16


# ══════════════════════════════════════════════════════════════════════════════
# with_retry (6 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[RollbarNetworkError("timeout"), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=RollbarAuthError("bad token"))
        with pytest.raises(RollbarAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    async def test_raises_after_max_attempts(self) -> None:
        err = RollbarNetworkError("persistent error")
        fn = AsyncMock(side_effect=err)
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RollbarNetworkError):
                await with_retry(fn, max_attempts=3)
        assert fn.call_count == 3

    async def test_rate_limit_retry(self) -> None:
        fn = AsyncMock(
            side_effect=[
                RollbarRateLimitError("rate limited", retry_after=0.0),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}

    async def test_passes_args_and_kwargs(self) -> None:
        fn = AsyncMock(return_value="result")
        result = await with_retry(fn, "arg1", kwarg1="val1", max_attempts=2)
        fn.assert_called_once_with("arg1", kwarg1="val1")
        assert result == "result"


# ══════════════════════════════════════════════════════════════════════════════
# HTTP client (mocked) (15 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestRollbarHTTPClient:
    def _make_client(self) -> "RollbarHTTPClient":
        from client.http_client import RollbarHTTPClient
        return RollbarHTTPClient(config={"access_token": ACCESS_TOKEN})

    def test_access_token_in_auth_params(self) -> None:
        client = self._make_client()
        params = client._auth_params()
        assert params["access_token"] == ACCESS_TOKEN

    def test_auth_params_merges_extra(self) -> None:
        client = self._make_client()
        params = client._auth_params({"page": 2, "level": "error"})
        assert params["access_token"] == ACCESS_TOKEN
        assert params["page"] == 2
        assert params["level"] == "error"

    def test_base_url_default(self) -> None:
        client = self._make_client()
        assert client._base_url == "https://api.rollbar.com"

    def test_base_url_strips_trailing_slash(self) -> None:
        from client.http_client import RollbarHTTPClient
        client = RollbarHTTPClient(config={"access_token": ACCESS_TOKEN, "base_url": "https://api.rollbar.com/"})
        assert client._base_url == "https://api.rollbar.com"

    def test_no_bearer_header(self) -> None:
        """Rollbar uses query param auth, not Authorization header."""
        client = self._make_client()
        params = client._auth_params()
        # There should be no Authorization logic — only access_token in params
        assert "access_token" in params
        # Confirm no _make_headers method returning Bearer
        assert not hasattr(client, "_make_headers") or "Authorization" not in getattr(client, "_make_headers", lambda: {})()

    async def test_get_project_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"err": 0, "result": SAMPLE_PROJECT})
        result = await client.get_project()
        assert result["name"] == "my-backend"
        client._request.assert_called_once_with("GET", "/api/1/project/")

    async def test_get_project_users_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={
            "err": 0, "result": {"users": [{"id": 1, "username": "alice"}]}
        })
        result = await client.get_project_users()
        assert len(result) == 1
        assert result[0]["username"] == "alice"

    async def test_get_items_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={
            "err": 0, "result": {"items": [SAMPLE_ITEM], "total_count": 1}
        })
        result = await client.get_items(page=1)
        assert "result" in result
        client._request.assert_called_once_with("GET", "/api/1/items/", params={"page": 1})

    async def test_get_items_with_level_and_status(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"err": 0, "result": {"items": []}})
        await client.get_items(page=1, level="error", status="active")
        call_params = client._request.call_args[1]["params"]
        assert call_params.get("level") == "error"
        assert call_params.get("status") == "active"

    async def test_get_item_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"err": 0, "result": SAMPLE_ITEM})
        result = await client.get_item(987654)
        assert result["id"] == 987654

    async def test_get_occurrences_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={
            "err": 0, "result": {"instances": [SAMPLE_OCCURRENCE]}
        })
        result = await client.get_occurrences(page=1)
        assert "result" in result

    async def test_get_deploys_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={
            "err": 0, "result": {"deploys": [SAMPLE_DEPLOY]}
        })
        result = await client.get_deploys(page=1)
        assert "result" in result

    async def test_get_top_active_items(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"err": 0, "result": {"items": []}})
        result = await client.get_top_active_items()
        assert isinstance(result, dict)

    def test_raise_for_status_401(self) -> None:
        client = self._make_client()
        with pytest.raises(RollbarAuthError) as exc_info:
            client._raise_for_status(401, {"message": "Unauthorized"})
        assert exc_info.value.status_code == 401

    def test_raise_for_status_403(self) -> None:
        client = self._make_client()
        with pytest.raises(RollbarAuthError) as exc_info:
            client._raise_for_status(403, {"message": "Forbidden"})
        assert exc_info.value.status_code == 403

    def test_raise_for_status_404(self) -> None:
        client = self._make_client()
        with pytest.raises(RollbarNotFoundError):
            client._raise_for_status(404, {})

    def test_raise_for_status_429(self) -> None:
        client = self._make_client()
        with pytest.raises(RollbarRateLimitError) as exc_info:
            client._raise_for_status(429, {"message": "Too Many Requests"})
        assert exc_info.value.status_code == 429

    def test_raise_for_status_500(self) -> None:
        client = self._make_client()
        with pytest.raises(RollbarNetworkError) as exc_info:
            client._raise_for_status(500, {"message": "Internal Server Error"})
        assert exc_info.value.status_code == 500


# ══════════════════════════════════════════════════════════════════════════════
# install() (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestInstall:
    async def test_install_success(self) -> None:
        connector = _make_connector()
        connector.client.get_project = AsyncMock(return_value=SAMPLE_PROJECT)
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "my-backend" in result.message

    async def test_install_missing_access_token(self) -> None:
        connector = _make_connector(access_token="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "access_token" in result.message

    async def test_install_auth_error(self) -> None:
        connector = _make_connector()
        connector.client.get_project = AsyncMock(
            side_effect=RollbarAuthError("Unauthorized", status_code=401)
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        connector = _make_connector()
        connector.client.get_project = AsyncMock(
            side_effect=RollbarNetworkError("timeout")
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_install_sets_connector_id(self) -> None:
        connector = _make_connector()
        connector.client.get_project = AsyncMock(return_value=SAMPLE_PROJECT)
        result = await connector.install()
        assert result.connector_id == CONNECTOR_ID


# ══════════════════════════════════════════════════════════════════════════════
# health_check() (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    async def test_health_check_healthy(self) -> None:
        connector = _make_connector()
        connector.client.get_project = AsyncMock(return_value=SAMPLE_PROJECT)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "my-backend" in result.message

    async def test_health_check_missing_token(self) -> None:
        connector = _make_connector(access_token="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        connector = _make_connector()
        connector.client.get_project = AsyncMock(
            side_effect=RollbarAuthError("Unauthorized")
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        connector = _make_connector()
        connector.client.get_project = AsyncMock(
            side_effect=RollbarNetworkError("connection refused")
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_generic_error_degraded(self) -> None:
        connector = _make_connector()
        connector.client.get_project = AsyncMock(
            side_effect=Exception("unexpected error")
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ══════════════════════════════════════════════════════════════════════════════
# sync() (7 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestSync:
    async def test_sync_returns_sync_result(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(return_value={
            "result": {"items": [SAMPLE_ITEM]}
        })
        connector.client.get_deploys = AsyncMock(return_value={
            "result": {"deploys": [SAMPLE_DEPLOY]}
        })
        result = await connector.sync()
        from models import SyncResult
        assert isinstance(result, SyncResult)

    async def test_sync_counts_items_and_deploys(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(side_effect=[
            {"result": {"items": [SAMPLE_ITEM, SAMPLE_ITEM]}},
            {"result": {"items": []}},
        ])
        connector.client.get_deploys = AsyncMock(side_effect=[
            {"result": {"deploys": [SAMPLE_DEPLOY]}},
            {"result": {"deploys": []}},
        ])
        result = await connector.sync()
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.documents_failed == 0

    async def test_sync_completed_status_on_all_success(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(side_effect=[
            {"result": {"items": [SAMPLE_ITEM]}},
            {"result": {"items": []}},
        ])
        connector.client.get_deploys = AsyncMock(return_value={"result": {"deploys": []}})
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_partial_status_on_ingest_failure(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(side_effect=[
            {"result": {"items": [SAMPLE_ITEM]}},
            {"result": {"items": []}},
        ])
        connector.client.get_deploys = AsyncMock(return_value={"result": {"deploys": []}})

        async def _failing_ingest(doc, kb_id):  # type: ignore[override]
            raise RuntimeError("ingest failed")

        connector._ingest_document = _failing_ingest  # type: ignore[method-assign]
        result = await connector.sync(kb_id="kb_test")
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed > 0

    async def test_sync_failed_status_on_items_error(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(
            side_effect=RollbarError("API error")
        )
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_deploy_error_is_non_fatal(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(side_effect=[
            {"result": {"items": [SAMPLE_ITEM]}},
            {"result": {"items": []}},
        ])
        connector.client.get_deploys = AsyncMock(
            side_effect=RollbarError("deploys unavailable")
        )
        result = await connector.sync()
        # Items synced successfully; deploy failure is non-fatal
        assert result.documents_found >= 1
        assert result.documents_synced >= 1

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(side_effect=[
            {"result": {"items": [SAMPLE_ITEM]}},
            {"result": {"items": []}},
        ])
        connector.client.get_deploys = AsyncMock(return_value={"result": {"deploys": []}})
        ingest_calls: list = []

        async def _track_ingest(doc, kb_id):  # type: ignore[override]
            ingest_calls.append((doc, kb_id))

        connector._ingest_document = _track_ingest  # type: ignore[method-assign]
        await connector.sync(kb_id="kb_abc")
        assert len(ingest_calls) >= 1
        assert ingest_calls[0][1] == "kb_abc"


# ══════════════════════════════════════════════════════════════════════════════
# list_items (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestListItems:
    async def test_list_items_returns_documents(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(side_effect=[
            {"result": {"items": [SAMPLE_ITEM]}},
            {"result": {"items": []}},
        ])
        docs = await connector.list_items()
        assert len(docs) == 1
        assert docs[0].metadata["item_id"] == "987654"

    async def test_list_items_level_filter_passed_to_client(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(return_value={"result": {"items": []}})
        await connector.list_items(level="error")
        call_kwargs = connector.client.get_items.call_args
        assert call_kwargs.kwargs.get("level") == "error" or (call_kwargs.args and "error" in str(call_kwargs))

    async def test_list_items_status_filter_passed_to_client(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(return_value={"result": {"items": []}})
        await connector.list_items(status="active")
        call_kwargs = connector.client.get_items.call_args
        assert call_kwargs.kwargs.get("status") == "active" or (call_kwargs.args and "active" in str(call_kwargs))

    async def test_list_items_sets_connector_and_tenant_ids(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(side_effect=[
            {"result": {"items": [SAMPLE_ITEM]}},
            {"result": {"items": []}},
        ])
        docs = await connector.list_items()
        assert docs[0].connector_id == CONNECTOR_ID
        assert docs[0].tenant_id == TENANT_ID

    async def test_list_items_stops_on_empty_page(self) -> None:
        connector = _make_connector()
        connector.client.get_items = AsyncMock(return_value={"result": {"items": []}})
        docs = await connector.list_items()
        assert docs == []
        # Should stop after first empty page (only 1 call)
        assert connector.client.get_items.call_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# list_occurrences (3 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestListOccurrences:
    async def test_list_occurrences_returns_documents(self) -> None:
        connector = _make_connector()
        connector.client.get_occurrences = AsyncMock(side_effect=[
            {"result": {"instances": [SAMPLE_OCCURRENCE]}},
            {"result": {"instances": []}},
        ])
        docs = await connector.list_occurrences()
        assert len(docs) == 1
        assert docs[0].metadata["exc_class"] == "AttributeError"

    async def test_list_occurrences_sets_tenant_id(self) -> None:
        connector = _make_connector()
        connector.client.get_occurrences = AsyncMock(side_effect=[
            {"result": {"instances": [SAMPLE_OCCURRENCE]}},
            {"result": {"instances": []}},
        ])
        docs = await connector.list_occurrences()
        assert docs[0].tenant_id == TENANT_ID

    async def test_list_occurrences_empty(self) -> None:
        connector = _make_connector()
        connector.client.get_occurrences = AsyncMock(return_value={"result": {"instances": []}})
        docs = await connector.list_occurrences()
        assert docs == []


# ══════════════════════════════════════════════════════════════════════════════
# list_deploys (3 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestListDeploys:
    async def test_list_deploys_returns_documents(self) -> None:
        connector = _make_connector()
        connector.client.get_deploys = AsyncMock(side_effect=[
            {"result": {"deploys": [SAMPLE_DEPLOY]}},
            {"result": {"deploys": []}},
        ])
        docs = await connector.list_deploys()
        assert len(docs) == 1
        assert docs[0].metadata["environment"] == "production"

    async def test_list_deploys_sets_connector_id(self) -> None:
        connector = _make_connector()
        connector.client.get_deploys = AsyncMock(side_effect=[
            {"result": {"deploys": [SAMPLE_DEPLOY]}},
            {"result": {"deploys": []}},
        ])
        docs = await connector.list_deploys()
        assert docs[0].connector_id == CONNECTOR_ID

    async def test_list_deploys_empty(self) -> None:
        connector = _make_connector()
        connector.client.get_deploys = AsyncMock(return_value={"result": {"deploys": []}})
        docs = await connector.list_deploys()
        assert docs == []


# ══════════════════════════════════════════════════════════════════════════════
# get_item (3 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestGetItem:
    async def test_get_item_returns_raw_dict(self) -> None:
        connector = _make_connector()
        connector.client.get_item = AsyncMock(return_value=SAMPLE_ITEM)
        result = await connector.get_item(987654)
        assert result["id"] == 987654

    async def test_get_item_not_found_raises(self) -> None:
        connector = _make_connector()
        connector.client.get_item = AsyncMock(
            side_effect=RollbarNotFoundError("item", 99999)
        )
        with pytest.raises(RollbarNotFoundError):
            await connector.get_item(99999)

    async def test_get_item_passes_id_to_client(self) -> None:
        connector = _make_connector()
        connector.client.get_item = AsyncMock(return_value=SAMPLE_ITEM)
        await connector.get_item(987654)
        connector.client.get_item.assert_called_once_with(987654)
