from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import aiohttp

from exceptions import (
    DatabricksAuthError,
    DatabricksError,
    DatabricksNetworkError,
    DatabricksNotFoundError,
    DatabricksRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0


class DatabricksHTTPClient:
    """Low-level async HTTP client for the Databricks REST API 2.0/2.1.

    Sends an Authorization: Bearer {token} header on every request.
    Base URL is the customer workspace URL (e.g. https://adb-123456.azuredatabricks.net).
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._token: str = cfg.get("token", "")
        workspace_url: str = cfg.get("workspace_url", "").rstrip("/")
        # Ensure workspace URL has a scheme
        if workspace_url and not workspace_url.startswith("http"):
            workspace_url = f"https://{workspace_url}"
        self._workspace_url: str = workspace_url
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._token}",
                },
            )
        return self._session

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map non-2xx HTTP status codes to typed Databricks exceptions."""
        err_msg = body.get(
            "message",
            body.get("error", body.get("error_code", f"Databricks error {status}")),
        )
        if isinstance(err_msg, list):
            err_msg = "; ".join(str(e) for e in err_msg)
        err_msg = str(err_msg)

        if status in (401, 403):
            raise DatabricksAuthError(
                f"Authentication failed: {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise DatabricksNotFoundError("resource", "unknown")
        if status == 429:
            raise DatabricksRateLimitError(f"Rate limited: {err_msg}")
        if status >= 500:
            raise DatabricksNetworkError(
                f"Databricks server error {status}: {err_msg}",
                status_code=status,
            )
        raise DatabricksError(
            f"Databricks error {status}: {err_msg}", status_code=status
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to the Databricks REST API.

        Returns parsed JSON. Raises typed DatabricksError subclasses on non-2xx.
        """
        url = f"{self._workspace_url}/{path.lstrip('/')}"
        session = self._get_session()
        try:
            async with session.request(
                method, url, params=params, json=json
            ) as response:
                if response.status in (200, 201, 202, 204):
                    if response.status == 204 or response.content_length == 0:
                        return {}
                    return await response.json(content_type=None)

                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    try:
                        text = await response.text()
                        body = {"message": text}
                    except Exception:
                        pass

                self._raise_for_status(response.status, body)
        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise DatabricksNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise DatabricksNetworkError(f"Network error: {exc}") from exc
        except (DatabricksError, DatabricksNetworkError):
            raise
        except Exception as exc:
            raise DatabricksNetworkError(f"Unexpected network error: {exc}") from exc

    # ── Authentication / Identity ─────────────────────────────────────────────

    async def get_current_user(self) -> dict[str, Any]:
        """GET /api/2.0/preview/scim/v2/Me — retrieve the authenticated user."""
        result = await self._request("GET", "/api/2.0/preview/scim/v2/Me")
        return result if isinstance(result, dict) else {}

    # ── Clusters ──────────────────────────────────────────────────────────────

    async def list_clusters(self) -> dict[str, Any]:
        """GET /api/2.0/clusters/list — list all clusters in the workspace."""
        result = await self._request("GET", "/api/2.0/clusters/list")
        return result if isinstance(result, dict) else {}

    async def get_cluster(self, cluster_id: str) -> dict[str, Any]:
        """GET /api/2.0/clusters/get — retrieve a single cluster by ID."""
        result = await self._request(
            "GET", "/api/2.0/clusters/get", params={"cluster_id": cluster_id}
        )
        return result if isinstance(result, dict) else {}

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def list_jobs(
        self, limit: int = 25, offset: int = 0
    ) -> dict[str, Any]:
        """GET /api/2.1/jobs/list — list jobs with pagination.

        Args:
            limit:  Number of jobs per page (max 100).
            offset: Offset for pagination.

        Returns:
            Dict with 'jobs' list and 'has_more' flag.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        result = await self._request("GET", "/api/2.1/jobs/list", params=params)
        return result if isinstance(result, dict) else {}

    # ── Notebooks / Workspace ────────────────────────────────────────────────

    async def list_notebooks(
        self, path: str = "/", recursive: bool = False
    ) -> dict[str, Any]:
        """GET /api/2.0/workspace/list — list workspace objects at the given path.

        Args:
            path:      Absolute workspace path (default root '/').
            recursive: If True, include subdirectory contents.

        Returns:
            Dict with 'objects' list of workspace items.
        """
        params: dict[str, Any] = {"path": path}
        if recursive:
            params["recursive"] = "true"
        result = await self._request("GET", "/api/2.0/workspace/list", params=params)
        return result if isinstance(result, dict) else {}

    # ── MLflow — Experiments ──────────────────────────────────────────────────

    async def list_experiments(self) -> dict[str, Any]:
        """GET /api/2.0/mlflow/experiments/search — list all MLflow experiments."""
        result = await self._request("GET", "/api/2.0/mlflow/experiments/search")
        return result if isinstance(result, dict) else {}

    # ── MLflow — Registered Models ────────────────────────────────────────────

    async def list_models(self) -> dict[str, Any]:
        """GET /api/2.0/mlflow/registered-models/list — list all registered models."""
        result = await self._request(
            "GET", "/api/2.0/mlflow/registered-models/list"
        )
        return result if isinstance(result, dict) else {}

    # ── SQL Warehouses ────────────────────────────────────────────────────────

    async def list_sql_warehouses(self) -> dict[str, Any]:
        """GET /api/2.0/sql/warehouses — list all SQL warehouses."""
        result = await self._request("GET", "/api/2.0/sql/warehouses")
        return result if isinstance(result, dict) else {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> DatabricksHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
