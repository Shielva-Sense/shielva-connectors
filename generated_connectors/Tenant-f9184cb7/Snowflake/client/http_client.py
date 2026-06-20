from __future__ import annotations

import time
from typing import Any

import httpx

from exceptions import (
    SnowflakeAuthError,
    SnowflakeError,
    SnowflakeNetworkError,
    SnowflakeNotFoundError,
    SnowflakeQueryError,
    SnowflakeRateLimitError,
    SnowflakeServerError,
)

DEFAULT_TIMEOUT_S = 30.0
SESSION_TOKEN_TTL_S = 3600  # 1 hour — conservative; Snowflake tokens expire after 4h


class SnowflakeHTTPClient:
    """
    Low-level async HTTP client for the Snowflake SQL API v2 and REST API.

    Handles:
    - Username/password authentication via POST /session/v1/login-request
    - Session token management with TTL-based expiry and re-authentication
    - GET /api/v2/databases, /api/v2/databases/{db}/schemas, /api/v2/databases/{db}/schemas/{schema}/tables
    - POST /api/v2/statements  (async SQL statement execution)
    - GET  /api/v2/statements/{handle}  (poll statement result)
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._account: str = cfg.get("account", "").strip()
        self._username: str = cfg.get("username", "").strip()
        self._password: str = cfg.get("password", "").strip()
        self._warehouse: str = cfg.get("warehouse", "").strip()
        self._database: str = cfg.get("database", "").strip()
        self._schema: str = cfg.get("schema", "").strip()
        self._role: str = cfg.get("role", "").strip()

        self._base_url: str = f"https://{self._account}.snowflakecomputing.com"
        self._session_token: str = ""
        self._token_acquired_at: float = 0.0

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    # ── Session management ────────────────────────────────────────────────────

    def _is_token_expired(self) -> bool:
        """Return True when the session token is absent or older than SESSION_TOKEN_TTL_S."""
        if not self._session_token:
            return True
        return (time.monotonic() - self._token_acquired_at) >= SESSION_TOKEN_TTL_S

    async def authenticate(self) -> str:
        """
        POST /session/v1/login-request with username/password.
        Returns the session token and caches it internally.
        Raises SnowflakeAuthError on invalid credentials or missing config.
        """
        if not self._account or not self._username or not self._password:
            raise SnowflakeAuthError(
                "account, username, and password are required for Snowflake authentication",
                code="missing_credentials",
            )

        payload: dict[str, Any] = {
            "data": {
                "CLIENT_APP_ID": "shielva-connector",
                "CLIENT_APP_VERSION": "1.0.0",
                "LOGIN_NAME": self._username,
                "PASSWORD": self._password,
            }
        }
        if self._warehouse:
            payload["data"]["WAREHOUSE_NAME"] = self._warehouse
        if self._role:
            payload["data"]["ROLE_NAME"] = self._role

        try:
            response = await self._client.post(
                "/session/v1/login-request",
                json=payload,
                params={"warehouse": self._warehouse} if self._warehouse else {},
            )
        except httpx.TimeoutException as exc:
            raise SnowflakeNetworkError(f"Authentication request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise SnowflakeNetworkError(f"Network error during authentication: {exc}") from exc

        self._raise_for_status(response.status_code, self._safe_json(response))

        body = self._safe_json(response)
        success: bool = body.get("success", False)
        if not success:
            message: str = body.get("message", "Snowflake authentication failed")
            raise SnowflakeAuthError(message, status_code=401, code="auth_failed")

        data: dict[str, Any] = body.get("data", {}) or {}
        token: str = data.get("token", "") or ""
        if not token:
            raise SnowflakeAuthError(
                "Snowflake login response did not include a session token",
                status_code=401,
                code="no_token",
            )

        self._session_token = token
        self._token_acquired_at = time.monotonic()
        return token

    async def _ensure_authenticated(self) -> None:
        """Re-authenticate if the session token is absent or expired."""
        if self._is_token_expired():
            await self.authenticate()

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f'Snowflake Token="{self._session_token}"',
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Core request helpers ───────────────────────────────────────────────────

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        try:
            result = response.json()
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}

    def _raise_for_status(self, status_code: int, body: dict[str, Any]) -> None:
        """Map Snowflake HTTP status codes to typed exceptions."""
        if status_code in (200, 201, 202):
            return

        message: str = (
            body.get("message")
            or body.get("error")
            or body.get("data", {}).get("message", "")
            or f"Snowflake API error {status_code}"
        )

        if status_code in (401, 403):
            raise SnowflakeAuthError(message, status_code=status_code, code="unauthorized")
        if status_code == 404:
            raise SnowflakeNotFoundError("resource", message)
        if status_code == 429:
            raise SnowflakeRateLimitError(f"Rate limited: {message}")
        if status_code >= 500:
            raise SnowflakeServerError(
                f"Snowflake server error {status_code}: {message}",
                status_code=status_code,
                code="server_error",
            )
        raise SnowflakeError(
            f"Snowflake error {status_code}: {message}",
            status_code=status_code,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        """Execute an authenticated HTTP request."""
        await self._ensure_authenticated()

        try:
            response = await self._client.request(
                method,
                path,
                headers=self._auth_headers(),
                json=json,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise SnowflakeNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise SnowflakeNetworkError(f"Network error: {exc}") from exc

        # Handle token expiry: re-authenticate once and retry
        if response.status_code in (401, 403) and retry_auth:
            self._session_token = ""  # force re-auth
            await self._ensure_authenticated()
            try:
                response = await self._client.request(
                    method,
                    path,
                    headers=self._auth_headers(),
                    json=json,
                    params=params,
                )
            except httpx.TimeoutException as exc:
                raise SnowflakeNetworkError(f"Request timed out on retry: {exc}") from exc
            except httpx.NetworkError as exc:
                raise SnowflakeNetworkError(f"Network error on retry: {exc}") from exc

        body = self._safe_json(response)
        self._raise_for_status(response.status_code, body)
        return body

    # ── Database listing ───────────────────────────────────────────────────────

    async def get_databases(self) -> dict[str, Any]:
        """GET /api/v2/databases — list all accessible databases."""
        return await self._request("GET", "/api/v2/databases")

    async def get_schemas(self, database: str) -> dict[str, Any]:
        """GET /api/v2/databases/{database}/schemas — list schemas in a database."""
        return await self._request("GET", f"/api/v2/databases/{database}/schemas")

    async def get_tables(self, database: str, schema: str) -> dict[str, Any]:
        """GET /api/v2/databases/{database}/schemas/{schema}/tables — list tables in a schema."""
        return await self._request(
            "GET", f"/api/v2/databases/{database}/schemas/{schema}/tables"
        )

    # ── SQL statement execution ───────────────────────────────────────────────

    async def execute_statement(
        self,
        sql: str,
        warehouse: str | None = None,
        database: str | None = None,
        schema: str | None = None,
        role: str | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """
        POST /api/v2/statements — submit a SQL statement for execution.

        Returns the immediate response (may be inline results or a statement handle
        for async polling when execution takes > a few seconds).
        """
        payload: dict[str, Any] = {
            "statement": sql,
            "timeout": timeout,
        }

        resolved_warehouse = warehouse or self._warehouse
        resolved_database = database or self._database
        resolved_schema = schema or self._schema
        resolved_role = role or self._role

        if resolved_warehouse:
            payload["warehouse"] = resolved_warehouse
        if resolved_database:
            payload["database"] = resolved_database
        if resolved_schema:
            payload["schema"] = resolved_schema
        if resolved_role:
            payload["role"] = resolved_role

        return await self._request("POST", "/api/v2/statements", json=payload)

    async def get_statement_result(self, statement_handle: str) -> dict[str, Any]:
        """
        GET /api/v2/statements/{statementHandle} — poll for async statement result.

        Returns the statement status and result data when available.
        """
        return await self._request("GET", f"/api/v2/statements/{statement_handle}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SnowflakeHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
