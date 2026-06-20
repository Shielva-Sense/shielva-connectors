"""Unit tests for DatabricksConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AUTH_TYPE, CONNECTOR_TYPE, DatabricksConnector
from exceptions import (
    DatabricksAuthError,
    DatabricksError,
    DatabricksNetworkError,
    DatabricksNotFoundError,
    DatabricksRateLimitError,
)
from helpers.utils import (
    _stable_id,
    normalize_cluster,
    normalize_job,
    normalize_model,
    normalize_notebook,
    with_retry,
)
from models import (
    AuthStatus,
    ClusterState,
    ConnectorDocument,
    ConnectorHealth,
    JobRunStatus,
    SyncStatus,
)

TENANT_ID = "tenant_databricks_test"
CONNECTOR_ID = "conn_databricks_test_001"
VALID_TOKEN = "dapiabcdef1234567890abcdef1234567890"
VALID_WORKSPACE_URL = "https://adb-123456789012345.1.azuredatabricks.net"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_ME_RESPONSE: dict = {
    "id": "9876543210",
    "userName": "alice@example.com",
    "displayName": "Alice Engineer",
    "emails": [{"value": "alice@example.com", "primary": True}],
}

SAMPLE_CLUSTER: dict = {
    "cluster_id": "0101-123456-abc12345",
    "cluster_name": "ML Training Cluster",
    "state": "RUNNING",
    "spark_version": "13.3.x-scala2.12",
    "node_type_id": "Standard_DS3_v2",
    "num_workers": 4,
    "creator_user_name": "alice@example.com",
    "cluster_source": "UI",
    "autotermination_minutes": 120,
}

SAMPLE_CLUSTER_2: dict = {
    "cluster_id": "0202-654321-xyz98765",
    "cluster_name": "Analytics Cluster",
    "state": "TERMINATED",
    "spark_version": "12.2.x-scala2.12",
    "node_type_id": "Standard_DS4_v2",
    "num_workers": 2,
    "creator_user_name": "bob@example.com",
    "cluster_source": "JOB",
    "autotermination_minutes": 60,
}

SAMPLE_CLUSTERS_RESPONSE: dict = {
    "clusters": [SAMPLE_CLUSTER, SAMPLE_CLUSTER_2],
}

SAMPLE_JOB: dict = {
    "job_id": 100,
    "creator_user_name": "alice@example.com",
    "created_time": 1700000000000,
    "settings": {
        "name": "ETL Pipeline",
        "schedule": {
            "quartz_cron_expression": "0 0 9 * * ?",
            "timezone_id": "America/New_York",
        },
    },
}

SAMPLE_JOB_2: dict = {
    "job_id": 200,
    "creator_user_name": "bob@example.com",
    "created_time": 1710000000000,
    "settings": {
        "name": "ML Training Job",
    },
}

SAMPLE_JOBS_RESPONSE: dict = {
    "jobs": [SAMPLE_JOB, SAMPLE_JOB_2],
    "has_more": False,
}

SAMPLE_NOTEBOOK: dict = {
    "object_id": 55001,
    "object_type": "NOTEBOOK",
    "path": "/Shared/ETL/data_pipeline",
    "language": "PYTHON",
}

SAMPLE_NOTEBOOK_2: dict = {
    "object_id": 55002,
    "object_type": "NOTEBOOK",
    "path": "/Shared/ML/training_script",
    "language": "SCALA",
}

SAMPLE_DIRECTORY: dict = {
    "object_id": 44001,
    "object_type": "DIRECTORY",
    "path": "/Shared/Archive",
}

SAMPLE_NOTEBOOKS_RESPONSE: dict = {
    "objects": [SAMPLE_NOTEBOOK, SAMPLE_NOTEBOOK_2, SAMPLE_DIRECTORY],
}

SAMPLE_EXPERIMENT: dict = {
    "experiment_id": "1234567890",
    "name": "/Users/alice@example.com/MLflow Experiment",
    "artifact_location": "dbfs:/databricks/mlflow-tracking/1234567890",
    "lifecycle_stage": "active",
    "last_update_time": 1720000000000,
    "creation_time": 1710000000000,
}

SAMPLE_EXPERIMENTS_RESPONSE: dict = {
    "experiments": [SAMPLE_EXPERIMENT],
}

SAMPLE_MODEL: dict = {
    "name": "revenue-forecasting-model",
    "description": "LSTM-based revenue forecasting model",
    "creation_timestamp": 1700000000000,
    "last_updated_timestamp": 1720000000000,
    "user_id": "alice@example.com",
    "latest_versions": [
        {"version": "3", "status": "READY"},
        {"version": "2", "status": "ARCHIVED"},
    ],
}

SAMPLE_MODEL_2: dict = {
    "name": "churn-prediction-model",
    "description": "Customer churn classifier",
    "creation_timestamp": 1705000000000,
    "last_updated_timestamp": 1718000000000,
    "user_id": "bob@example.com",
    "latest_versions": [
        {"version": "1", "status": "READY"},
    ],
}

SAMPLE_MODELS_RESPONSE: dict = {
    "registered_models": [SAMPLE_MODEL, SAMPLE_MODEL_2],
}

SAMPLE_WAREHOUSE: dict = {
    "id": "wh-001abc",
    "name": "Shared SQL Warehouse",
    "cluster_size": "Small",
    "state": "RUNNING",
    "creator_name": "alice@example.com",
}

SAMPLE_WAREHOUSES_RESPONSE: dict = {
    "warehouses": [SAMPLE_WAREHOUSE],
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1 — Exception hierarchy (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_databricks_error_base(self) -> None:
        exc = DatabricksError("something broke", status_code=500, code="server_error")
        assert str(exc) == "something broke"
        assert exc.message == "something broke"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_databricks_auth_error_is_databricks_error(self) -> None:
        exc = DatabricksAuthError("forbidden", status_code=403, code="auth_error")
        assert isinstance(exc, DatabricksError)
        assert exc.status_code == 403

    def test_databricks_network_error(self) -> None:
        exc = DatabricksNetworkError("connection refused")
        assert isinstance(exc, DatabricksError)
        assert "connection" in str(exc)

    def test_databricks_not_found_error(self) -> None:
        exc = DatabricksNotFoundError("cluster", "0101-bad-id")
        assert isinstance(exc, DatabricksError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "0101-bad-id" in str(exc)

    def test_databricks_rate_limit_error(self) -> None:
        exc = DatabricksRateLimitError("too many requests", retry_after=60.0)
        assert isinstance(exc, DatabricksError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 60.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2 — Models & enums (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_cluster_state_enum(self) -> None:
        assert ClusterState.RUNNING == "RUNNING"
        assert ClusterState.TERMINATED == "TERMINATED"
        assert ClusterState.ERROR == "ERROR"

    def test_job_run_status_enum(self) -> None:
        assert JobRunStatus.SUCCESS == "SUCCESS"
        assert JobRunStatus.FAILED == "FAILED"
        assert JobRunStatus.CANCELED == "CANCELED"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test Doc",
            content="content here",
            connector_id="conn1",
            tenant_id="tenant1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 3 — Normalize functions (12 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeFunctions:
    # ── normalize_cluster ──────────────────────────────────────────────────────

    def test_normalize_cluster_basic(self) -> None:
        doc = normalize_cluster(
            SAMPLE_CLUSTER, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID
        )
        assert isinstance(doc, ConnectorDocument)
        assert "ML Training Cluster" in doc.title
        assert "0101-123456-abc12345" in doc.content
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_normalize_cluster_stable_id(self) -> None:
        doc1 = normalize_cluster(SAMPLE_CLUSTER)
        doc2 = normalize_cluster(SAMPLE_CLUSTER)
        assert doc1.source_id == doc2.source_id
        assert len(doc1.source_id) == 16

    def test_normalize_cluster_stable_id_matches_formula(self) -> None:
        expected = _stable_id("cluster", SAMPLE_CLUSTER["cluster_id"])
        doc = normalize_cluster(SAMPLE_CLUSTER)
        assert doc.source_id == expected

    def test_normalize_cluster_metadata_state(self) -> None:
        doc = normalize_cluster(SAMPLE_CLUSTER)
        assert doc.metadata["state"] == "RUNNING"
        assert doc.metadata["cluster_id"] == "0101-123456-abc12345"
        assert doc.metadata["num_workers"] == 4

    def test_normalize_cluster_missing_fields(self) -> None:
        doc = normalize_cluster({})
        assert doc.title == "Databricks cluster: Unnamed Cluster"
        assert len(doc.source_id) == 16

    # ── normalize_job ──────────────────────────────────────────────────────────

    def test_normalize_job_basic(self) -> None:
        doc = normalize_job(SAMPLE_JOB, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "ETL Pipeline" in doc.title
        assert "100" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_normalize_job_stable_id(self) -> None:
        doc1 = normalize_job(SAMPLE_JOB)
        doc2 = normalize_job(SAMPLE_JOB)
        assert doc1.source_id == doc2.source_id
        assert len(doc1.source_id) == 16

    def test_normalize_job_stable_id_matches_formula(self) -> None:
        expected = _stable_id("job", str(SAMPLE_JOB["job_id"]))
        doc = normalize_job(SAMPLE_JOB)
        assert doc.source_id == expected

    def test_normalize_job_metadata(self) -> None:
        doc = normalize_job(SAMPLE_JOB)
        assert doc.metadata["job_id"] == 100
        assert doc.metadata["name"] == "ETL Pipeline"
        assert doc.metadata["cron_expression"] == "0 0 9 * * ?"

    # ── normalize_notebook ────────────────────────────────────────────────────

    def test_normalize_notebook_basic(self) -> None:
        doc = normalize_notebook(
            SAMPLE_NOTEBOOK, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID
        )
        assert isinstance(doc, ConnectorDocument)
        assert "data_pipeline" in doc.title
        assert "/Shared/ETL/data_pipeline" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_normalize_notebook_stable_id(self) -> None:
        doc1 = normalize_notebook(SAMPLE_NOTEBOOK)
        doc2 = normalize_notebook(SAMPLE_NOTEBOOK)
        assert doc1.source_id == doc2.source_id
        assert len(doc1.source_id) == 16

    def test_normalize_notebook_stable_id_matches_formula(self) -> None:
        expected = _stable_id("notebook", SAMPLE_NOTEBOOK["path"])
        doc = normalize_notebook(SAMPLE_NOTEBOOK)
        assert doc.source_id == expected

    def test_normalize_notebook_metadata_path(self) -> None:
        doc = normalize_notebook(SAMPLE_NOTEBOOK)
        assert doc.metadata["path"] == "/Shared/ETL/data_pipeline"
        assert doc.metadata["language"] == "PYTHON"
        assert doc.metadata["object_type"] == "NOTEBOOK"

    # ── normalize_model ───────────────────────────────────────────────────────

    def test_normalize_model_basic(self) -> None:
        doc = normalize_model(
            SAMPLE_MODEL, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID
        )
        assert isinstance(doc, ConnectorDocument)
        assert "revenue-forecasting-model" in doc.title
        assert "revenue-forecasting-model" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_normalize_model_stable_id(self) -> None:
        doc1 = normalize_model(SAMPLE_MODEL)
        doc2 = normalize_model(SAMPLE_MODEL)
        assert doc1.source_id == doc2.source_id
        assert len(doc1.source_id) == 16

    def test_normalize_model_stable_id_matches_formula(self) -> None:
        expected = _stable_id("model", SAMPLE_MODEL["name"])
        doc = normalize_model(SAMPLE_MODEL)
        assert doc.source_id == expected

    def test_normalize_model_metadata_name(self) -> None:
        doc = normalize_model(SAMPLE_MODEL)
        assert doc.metadata["name"] == "revenue-forecasting-model"
        assert doc.metadata["user_id"] == "alice@example.com"
        assert len(doc.metadata["latest_versions"]) == 2

    def test_normalize_model_missing_fields(self) -> None:
        doc = normalize_model({})
        assert doc.title == "Databricks ML model: Unnamed Model"
        assert len(doc.source_id) == 16


# ═══════════════════════════════════════════════════════════════════════════════
# 4 — with_retry (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_retry_succeeds_first_attempt(self) -> None:
        mock_fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock_fn, max_attempts=3)
        assert result == {"ok": True}
        assert mock_fn.call_count == 1

    async def test_retry_succeeds_on_second_attempt(self) -> None:
        call_count = 0

        async def flaky() -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise DatabricksNetworkError("transient")
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(flaky, max_attempts=3)
        assert result == {"ok": True}
        assert call_count == 2

    async def test_retry_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=DatabricksNetworkError("always fails"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(DatabricksNetworkError):
                await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 3

    async def test_auth_error_not_retried(self) -> None:
        mock_fn = AsyncMock(side_effect=DatabricksAuthError("forbidden"))
        with pytest.raises(DatabricksAuthError):
            await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    async def test_rate_limit_retried_with_backoff(self) -> None:
        call_count = 0

        async def rate_limited() -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise DatabricksRateLimitError("slow down", retry_after=0.0)
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(rate_limited, max_attempts=3)
        assert result == {"ok": True}

    async def test_retry_passes_args_to_fn(self) -> None:
        mock_fn = AsyncMock(return_value={"jobs": []})
        await with_retry(mock_fn, "arg1", key="value")
        mock_fn.assert_called_once_with("arg1", key="value")


# ═══════════════════════════════════════════════════════════════════════════════
# 5 — HTTP client (mocked) (14 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDatabricksHTTPClient:
    def _make_client(
        self,
        workspace_url: str = VALID_WORKSPACE_URL,
        token: str = VALID_TOKEN,
    ) -> "DatabricksHTTPClient":
        from client.http_client import DatabricksHTTPClient
        return DatabricksHTTPClient(
            config={"workspace_url": workspace_url, "token": token}
        )

    def test_workspace_url_stored(self) -> None:
        client = self._make_client()
        assert client._workspace_url == VALID_WORKSPACE_URL

    def test_bearer_token_stored(self) -> None:
        client = self._make_client()
        assert client._token == VALID_TOKEN

    async def test_bearer_header_in_session(self) -> None:
        """Verify Authorization: Bearer header is injected into the aiohttp session."""
        client = self._make_client()
        try:
            session = client._get_session()
            headers = dict(session.headers)
            assert headers.get("Authorization") == f"Bearer {VALID_TOKEN}"
        finally:
            await client.aclose()

    async def test_get_current_user(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
        result = await client.get_current_user()
        assert result["userName"] == "alice@example.com"
        client._request.assert_called_once_with("GET", "/api/2.0/preview/scim/v2/Me")

    async def test_list_clusters(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_CLUSTERS_RESPONSE)
        result = await client.list_clusters()
        assert "clusters" in result
        client._request.assert_called_once_with("GET", "/api/2.0/clusters/list")

    async def test_get_cluster_by_id(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_CLUSTER)
        result = await client.get_cluster("0101-123456-abc12345")
        assert result["cluster_id"] == "0101-123456-abc12345"
        client._request.assert_called_once_with(
            "GET",
            "/api/2.0/clusters/get",
            params={"cluster_id": "0101-123456-abc12345"},
        )

    async def test_list_jobs(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_JOBS_RESPONSE)
        result = await client.list_jobs(limit=25, offset=0)
        assert "jobs" in result
        client._request.assert_called_once_with(
            "GET", "/api/2.1/jobs/list", params={"limit": 25, "offset": 0}
        )

    async def test_list_notebooks(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_NOTEBOOKS_RESPONSE)
        result = await client.list_notebooks(path="/Shared")
        assert "objects" in result
        client._request.assert_called_once_with(
            "GET",
            "/api/2.0/workspace/list",
            params={"path": "/Shared"},
        )

    async def test_list_experiments(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_EXPERIMENTS_RESPONSE)
        result = await client.list_experiments()
        assert "experiments" in result
        client._request.assert_called_once_with(
            "GET", "/api/2.0/mlflow/experiments/search"
        )

    async def test_list_models(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_MODELS_RESPONSE)
        result = await client.list_models()
        assert "registered_models" in result
        client._request.assert_called_once_with(
            "GET", "/api/2.0/mlflow/registered-models/list"
        )

    async def test_list_sql_warehouses(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_WAREHOUSES_RESPONSE)
        result = await client.list_sql_warehouses()
        assert "warehouses" in result
        client._request.assert_called_once_with("GET", "/api/2.0/sql/warehouses")

    async def test_raise_for_status_401_auth_error(self) -> None:
        from client.http_client import DatabricksHTTPClient
        client = DatabricksHTTPClient(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": "bad"}
        )
        with pytest.raises(DatabricksAuthError):
            client._raise_for_status(401, {"message": "Unauthorized"})

    async def test_raise_for_status_403_auth_error(self) -> None:
        from client.http_client import DatabricksHTTPClient
        client = DatabricksHTTPClient(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": "bad"}
        )
        with pytest.raises(DatabricksAuthError):
            client._raise_for_status(403, {"message": "Forbidden"})

    async def test_raise_for_status_404_not_found(self) -> None:
        from client.http_client import DatabricksHTTPClient
        client = DatabricksHTTPClient(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": "bad"}
        )
        with pytest.raises(DatabricksNotFoundError):
            client._raise_for_status(404, {})

    async def test_raise_for_status_429_rate_limit(self) -> None:
        from client.http_client import DatabricksHTTPClient
        client = DatabricksHTTPClient(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": "bad"}
        )
        with pytest.raises(DatabricksRateLimitError):
            client._raise_for_status(429, {"message": "Too many requests"})

    async def test_raise_for_status_500_network_error(self) -> None:
        from client.http_client import DatabricksHTTPClient
        client = DatabricksHTTPClient(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": "bad"}
        )
        with pytest.raises(DatabricksNetworkError):
            client._raise_for_status(500, {"message": "Internal Server Error"})


# ═══════════════════════════════════════════════════════════════════════════════
# 6 — install() (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    def _make_connector(
        self,
        workspace_url: str = VALID_WORKSPACE_URL,
        token: str = VALID_TOKEN,
    ) -> DatabricksConnector:
        return DatabricksConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"workspace_url": workspace_url, "token": token},
        )

    async def test_install_success(self) -> None:
        connector = self._make_connector()
        connector._make_client = MagicMock(
            return_value=MagicMock(
                get_current_user=AsyncMock(return_value=SAMPLE_ME_RESPONSE),
                aclose=AsyncMock(),
            )
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Databricks" in result.message

    async def test_install_missing_workspace_url(self) -> None:
        connector = self._make_connector(workspace_url="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "workspace_url" in result.message

    async def test_install_missing_token(self) -> None:
        connector = self._make_connector(token="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "token" in result.message

    async def test_install_invalid_credentials(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_current_user=AsyncMock(
                side_effect=DatabricksAuthError("Invalid token")
            ),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_current_user=AsyncMock(
                side_effect=DatabricksNetworkError("timeout")
            ),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 7 — health_check() (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    def _make_connector(
        self,
        workspace_url: str = VALID_WORKSPACE_URL,
        token: str = VALID_TOKEN,
    ) -> DatabricksConnector:
        return DatabricksConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"workspace_url": workspace_url, "token": token},
        )

    async def test_health_check_healthy(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_current_user=AsyncMock(return_value=SAMPLE_ME_RESPONSE),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.email == "alice@example.com"

    async def test_health_check_missing_workspace_url(self) -> None:
        connector = self._make_connector(workspace_url="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_missing_token(self) -> None:
        connector = self._make_connector(token="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_failure(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_current_user=AsyncMock(
                side_effect=DatabricksAuthError("token expired")
            ),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_degraded(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_current_user=AsyncMock(
                side_effect=DatabricksNetworkError("connection reset")
            ),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 8 — sync() (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _make_connector(self) -> DatabricksConnector:
        return DatabricksConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"workspace_url": VALID_WORKSPACE_URL, "token": VALID_TOKEN},
        )

    async def test_sync_all_resources_success(self) -> None:
        connector = self._make_connector()
        connector.list_clusters = AsyncMock(
            return_value=[SAMPLE_CLUSTER, SAMPLE_CLUSTER_2]
        )
        connector.list_jobs = AsyncMock(return_value=[SAMPLE_JOB])
        connector.list_notebooks = AsyncMock(return_value=[SAMPLE_NOTEBOOK])
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert result.documents_failed == 0

    async def test_sync_with_kb_id(self) -> None:
        connector = self._make_connector()
        connector.list_clusters = AsyncMock(return_value=[SAMPLE_CLUSTER])
        connector.list_jobs = AsyncMock(return_value=[])
        connector.list_notebooks = AsyncMock(return_value=[])
        connector._ingest_document = AsyncMock()
        result = await connector.sync(kb_id="kb_test_databricks")
        connector._ingest_document.assert_called_once()
        assert result.documents_synced == 1

    async def test_sync_no_data_returns_partial(self) -> None:
        connector = self._make_connector()
        connector.list_clusters = AsyncMock(return_value=[])
        connector.list_jobs = AsyncMock(return_value=[])
        connector.list_notebooks = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_clusters_failure_non_fatal(self) -> None:
        connector = self._make_connector()
        connector.list_clusters = AsyncMock(
            side_effect=DatabricksError("clusters unavailable")
        )
        connector.list_jobs = AsyncMock(return_value=[SAMPLE_JOB])
        connector.list_notebooks = AsyncMock(return_value=[])
        result = await connector.sync()
        # job still synced
        assert result.documents_synced >= 1

    async def test_sync_partial_on_failed_normalization(self) -> None:
        connector = self._make_connector()
        # Inject a bad cluster (missing cluster_id causes no issue, but let's test failed count)
        connector.list_clusters = AsyncMock(return_value=[SAMPLE_CLUSTER])
        connector.list_jobs = AsyncMock(return_value=[])
        connector.list_notebooks = AsyncMock(return_value=[])

        original_normalize = normalize_cluster

        def bad_normalize(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise ValueError("normalization failed")

        with patch("connector.normalize_cluster", side_effect=bad_normalize):
            result = await connector.sync()
        assert result.documents_failed == 1
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_multiple_clusters(self) -> None:
        connector = self._make_connector()
        connector.list_clusters = AsyncMock(
            return_value=[SAMPLE_CLUSTER, SAMPLE_CLUSTER_2]
        )
        connector.list_jobs = AsyncMock(return_value=[])
        connector.list_notebooks = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_notebooks_and_jobs(self) -> None:
        connector = self._make_connector()
        connector.list_clusters = AsyncMock(return_value=[])
        connector.list_jobs = AsyncMock(return_value=[SAMPLE_JOB, SAMPLE_JOB_2])
        connector.list_notebooks = AsyncMock(
            return_value=[SAMPLE_NOTEBOOK, SAMPLE_NOTEBOOK_2]
        )
        result = await connector.sync()
        assert result.documents_found == 4
        assert result.documents_synced == 4

    async def test_sync_all_resources_fail_returns_partial(self) -> None:
        connector = self._make_connector()
        connector.list_clusters = AsyncMock(
            side_effect=DatabricksError("err")
        )
        connector.list_jobs = AsyncMock(side_effect=DatabricksError("err"))
        connector.list_notebooks = AsyncMock(side_effect=DatabricksError("err"))
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════════════
# 9 — list methods (7 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    def _make_connector(self) -> DatabricksConnector:
        return DatabricksConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"workspace_url": VALID_WORKSPACE_URL, "token": VALID_TOKEN},
        )

    async def test_list_clusters_returns_list(self) -> None:
        connector = self._make_connector()
        connector.client.list_clusters = AsyncMock(
            return_value=SAMPLE_CLUSTERS_RESPONSE
        )
        result = await connector.list_clusters()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["cluster_id"] == "0101-123456-abc12345"

    async def test_list_jobs_single_page(self) -> None:
        connector = self._make_connector()
        connector.client.list_jobs = AsyncMock(return_value=SAMPLE_JOBS_RESPONSE)
        result = await connector.list_jobs()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["job_id"] == 100

    async def test_list_jobs_stops_when_no_more(self) -> None:
        connector = self._make_connector()
        # has_more = False means no second page
        connector.client.list_jobs = AsyncMock(
            return_value={"jobs": [SAMPLE_JOB], "has_more": False}
        )
        result = await connector.list_jobs()
        assert connector.client.list_jobs.call_count == 1
        assert len(result) == 1

    async def test_list_notebooks_filters_notebooks_only(self) -> None:
        """list_notebooks() should return only NOTEBOOK objects, not DIRECTORY."""
        connector = self._make_connector()
        connector.client.list_notebooks = AsyncMock(
            return_value=SAMPLE_NOTEBOOKS_RESPONSE
        )
        result = await connector.list_notebooks()
        assert isinstance(result, list)
        # SAMPLE_NOTEBOOKS_RESPONSE has 2 notebooks + 1 directory
        assert len(result) == 2
        assert all(nb.get("object_type") == "NOTEBOOK" for nb in result)

    async def test_list_experiments(self) -> None:
        connector = self._make_connector()
        connector.client.list_experiments = AsyncMock(
            return_value=SAMPLE_EXPERIMENTS_RESPONSE
        )
        result = await connector.list_experiments()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["experiment_id"] == "1234567890"

    async def test_list_models(self) -> None:
        connector = self._make_connector()
        connector.client.list_models = AsyncMock(return_value=SAMPLE_MODELS_RESPONSE)
        result = await connector.list_models()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "revenue-forecasting-model"

    async def test_list_sql_warehouses(self) -> None:
        connector = self._make_connector()
        connector.client.list_sql_warehouses = AsyncMock(
            return_value=SAMPLE_WAREHOUSES_RESPONSE
        )
        result = await connector.list_sql_warehouses()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "wh-001abc"


# ═══════════════════════════════════════════════════════════════════════════════
# 10 — connector constants & module-level attributes (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "databricks"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_class_attributes(self) -> None:
        assert DatabricksConnector.CONNECTOR_TYPE == "databricks"
        assert DatabricksConnector.AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# 11 — stable ID helper (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStableId:
    def test_stable_id_length(self) -> None:
        result = _stable_id("cluster", "0101-123456-abc12345")
        assert len(result) == 16

    def test_stable_id_deterministic(self) -> None:
        a = _stable_id("cluster", "0101-123456-abc12345")
        b = _stable_id("cluster", "0101-123456-abc12345")
        assert a == b

    def test_stable_id_differs_by_prefix(self) -> None:
        cluster_id = _stable_id("cluster", "123")
        job_id = _stable_id("job", "123")
        notebook_id = _stable_id("notebook", "123")
        model_id = _stable_id("model", "123")
        assert cluster_id != job_id
        assert job_id != notebook_id
        assert notebook_id != model_id


# ═══════════════════════════════════════════════════════════════════════════════
# 12 — lifecycle & config (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    async def test_connector_aclose(self) -> None:
        connector = DatabricksConnector(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": VALID_TOKEN}
        )
        connector.client.aclose = AsyncMock()
        await connector.aclose()
        connector.client.aclose.assert_called_once()

    async def test_connector_context_manager(self) -> None:
        connector = DatabricksConnector(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": VALID_TOKEN}
        )
        connector.client.aclose = AsyncMock()
        async with connector as ctx:
            assert ctx is connector
        connector.client.aclose.assert_called_once()

    def test_connector_stores_workspace_url(self) -> None:
        connector = DatabricksConnector(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": VALID_TOKEN}
        )
        assert VALID_WORKSPACE_URL in connector._workspace_url

    def test_connector_stores_token(self) -> None:
        connector = DatabricksConnector(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": VALID_TOKEN}
        )
        assert connector._token == VALID_TOKEN


# ═══════════════════════════════════════════════════════════════════════════════
# 13 — HTTP client lifecycle (2 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientLifecycle:
    async def test_http_client_aclose(self) -> None:
        from client.http_client import DatabricksHTTPClient
        client = DatabricksHTTPClient(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": VALID_TOKEN}
        )
        _ = client._get_session()
        await client.aclose()
        assert client._session is None or client._session.closed

    async def test_http_client_context_manager(self) -> None:
        from client.http_client import DatabricksHTTPClient
        async with DatabricksHTTPClient(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": VALID_TOKEN}
        ) as client:
            assert client is not None
        assert client._session is None or client._session.closed


# ═══════════════════════════════════════════════════════════════════════════════
# 14 — Edge cases & additional coverage (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_normalize_notebook_name_derived_from_path(self) -> None:
        """Name should be the last path segment."""
        nb = {"path": "/Users/alice/my_analysis", "object_type": "NOTEBOOK"}
        doc = normalize_notebook(nb)
        assert "my_analysis" in doc.title

    def test_normalize_job_without_settings(self) -> None:
        """Job with no settings dict should not raise."""
        job = {"job_id": 999}
        doc = normalize_job(job)
        assert doc.metadata["job_id"] == 999
        assert len(doc.source_id) == 16

    def test_normalize_cluster_terminated_state(self) -> None:
        cluster = dict(SAMPLE_CLUSTER_2)
        doc = normalize_cluster(cluster)
        assert doc.metadata["state"] == "TERMINATED"

    async def test_list_jobs_pagination_continues_when_has_more(self) -> None:
        """Verify pagination fetches next page when has_more=True."""
        connector = DatabricksConnector(
            config={"workspace_url": VALID_WORKSPACE_URL, "token": VALID_TOKEN}
        )
        call_count = 0

        async def mock_list_jobs(limit: int, offset: int) -> dict:
            nonlocal call_count
            call_count += 1
            if offset == 0:
                return {"jobs": [SAMPLE_JOB], "has_more": True}
            return {"jobs": [SAMPLE_JOB_2], "has_more": False}

        connector.client.list_jobs = mock_list_jobs
        result = await connector.list_jobs(limit=1)
        assert call_count == 2
        assert len(result) == 2
