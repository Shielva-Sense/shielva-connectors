"""Auth0 Management API v2 HTTP client — machine-to-machine client credentials."""

from __future__ import annotations

import time
from typing import Any

import httpx

from exceptions import (
    Auth0AuthError,
    Auth0Error,
    Auth0NetworkError,
    Auth0NotFoundError,
    Auth0RateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0
# Auth0 management tokens typically expire in 86400 s; refresh 60 s early.
_TOKEN_EXPIRY_BUFFER_S = 60


class Auth0HTTPClient:
    """Low-level async HTTP client for the Auth0 Management API v2.

    Authentication: machine-to-machine client credentials flow.
        POST https://{domain}/oauth/token
            grant_type=client_credentials
            client_id={client_id}
            client_secret={client_secret}
            audience=https://{domain}/api/v2/

    The management access token is cached and automatically refreshed before it
    expires (with a 60-second buffer).
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._domain: str = cfg.get("domain", "").strip().rstrip("/")
        self._client_id: str = cfg.get("client_id", "")
        self._client_secret: str = cfg.get("client_secret", "")

        self._base_url: str = f"https://{self._domain}/api/v2" if self._domain else ""
        self._token_url: str = f"https://{self._domain}/oauth/token" if self._domain else ""
        self._audience: str = f"https://{self._domain}/api/v2/" if self._domain else ""

        self._access_token: str = ""
        self._token_expires_at: float = 0.0

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        # Separate client for token endpoint (does not share base_url)
        self._token_client = httpx.AsyncClient(timeout=timeout)

    # ── Token management ──────────────────────────────────────────────────────

    async def authenticate(self) -> str:
        """Fetch a new management API access token using client credentials.

        Caches the token and returns it. Raises Auth0AuthError on failure.
        """
        if not self._domain or not self._client_id or not self._client_secret:
            raise Auth0AuthError(
                "domain, client_id and client_secret are all required",
                status_code=0,
                code="missing_credentials",
            )

        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "audience": self._audience,
        }
        try:
            resp = await self._token_client.post(self._token_url, json=payload)
        except httpx.TimeoutException as exc:
            raise Auth0NetworkError(f"Token request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise Auth0NetworkError(f"Network error during token fetch: {exc}") from exc

        if resp.status_code != 200:
            body: dict[str, Any] = {}
            try:
                body = resp.json()
            except Exception:
                pass
            err = body.get("error_description") or body.get("error") or f"HTTP {resp.status_code}"
            raise Auth0AuthError(
                f"Auth0 token request failed ({resp.status_code}): {err}",
                status_code=resp.status_code,
                code="token_error",
            )

        data: dict[str, Any] = resp.json()
        token: str = data.get("access_token", "")
        expires_in: int = int(data.get("expires_in", 86400))

        if not token:
            raise Auth0AuthError(
                "Auth0 returned empty access_token",
                status_code=200,
                code="empty_token",
            )

        self._access_token = token
        self._token_expires_at = time.monotonic() + expires_in - _TOKEN_EXPIRY_BUFFER_S
        return token

    def _is_token_valid(self) -> bool:
        return bool(self._access_token) and time.monotonic() < self._token_expires_at

    async def _ensure_token(self) -> str:
        if not self._is_token_valid():
            await self.authenticate()
        return self._access_token

    # ── Error mapping ─────────────────────────────────────────────────────────

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map Auth0 Management API HTTP error codes to typed exceptions."""
        err_msg: str = (
            body.get("message")
            or body.get("error_description")
            or body.get("error")
            or f"HTTP {status}"
        )
        if status in (401, 403):
            raise Auth0AuthError(
                f"Auth0 authentication failed ({status}): {err_msg}",
                status_code=status,
                code="unauthorized" if status == 401 else "forbidden",
            )
        if status == 404:
            raise Auth0NotFoundError("resource", err_msg)
        if status == 429:
            raise Auth0RateLimitError(f"Auth0 rate limit exceeded: {err_msg}")
        if status >= 500:
            raise Auth0NetworkError(
                f"Auth0 server error {status}: {err_msg}",
                status_code=status,
            )
        raise Auth0Error(f"Auth0 error {status}: {err_msg}", status_code=status)

    # ── Core request ──────────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute an authenticated HTTP request and return parsed JSON."""
        token = await self._ensure_token()
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise Auth0NetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise Auth0NetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass
        self._raise_for_status(response.status_code, body)

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(
        self,
        page: int = 0,
        per_page: int = 100,
        include_totals: bool = True,
        **extra: Any,
    ) -> dict[str, Any]:
        """GET /api/v2/users — page-based pagination.

        Returns dict with keys: users (list), start, limit, length, total (when include_totals=True).
        """
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "include_totals": "true" if include_totals else "false",
        }
        params.update(extra)
        result = await self._request("GET", "/users", params=params)
        if isinstance(result, list):
            # include_totals=false → raw list
            return {"users": result, "total": len(result), "start": page * per_page, "length": len(result)}
        return result  # type: ignore[return-value]

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """GET /api/v2/users/{id}."""
        result = await self._request("GET", f"/users/{user_id}")
        return result  # type: ignore[return-value]

    # ── Roles ─────────────────────────────────────────────────────────────────

    async def get_roles(
        self,
        page: int = 0,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /api/v2/roles — page-based pagination.

        Returns dict with keys: roles (list), start, limit, length, total.
        """
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "include_totals": "true",
        }
        result = await self._request("GET", "/roles", params=params)
        if isinstance(result, list):
            return {"roles": result, "total": len(result), "start": page * per_page, "length": len(result)}
        return result  # type: ignore[return-value]

    # ── Clients (Applications) ─────────────────────────────────────────────────

    async def get_clients(
        self,
        page: int = 0,
        per_page: int = 100,
        app_type: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/clients — page-based pagination.

        Args:
            app_type: optional filter e.g. "native", "spa", "regular_web", "non_interactive".
        Returns dict with keys: clients (list), total, start, length.
        """
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "include_totals": "true",
        }
        if app_type:
            params["app_type"] = app_type
        result = await self._request("GET", "/clients", params=params)
        if isinstance(result, list):
            return {"clients": result, "total": len(result), "start": page * per_page, "length": len(result)}
        return result  # type: ignore[return-value]

    # ── Connections ───────────────────────────────────────────────────────────

    async def get_connections(
        self,
        page: int = 0,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /api/v2/connections — page-based pagination.

        Returns dict with keys: connections (list), total, start, length.
        """
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "include_totals": "true",
        }
        result = await self._request("GET", "/connections", params=params)
        if isinstance(result, list):
            return {"connections": result, "total": len(result), "start": page * per_page, "length": len(result)}
        return result  # type: ignore[return-value]

    # ── Logs ──────────────────────────────────────────────────────────────────

    async def get_logs(
        self,
        page: int = 0,
        per_page: int = 100,
        from_: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/v2/logs — cursor-based or page-based.

        When from_ is provided (checkpoint ID), cursor-based mode is used (no page param).
        Otherwise falls back to page/per_page.

        Returns a list of log event objects.
        """
        params: dict[str, Any] = {"per_page": per_page}
        if from_:
            params["from"] = from_
        else:
            params["page"] = page
        result = await self._request("GET", "/logs", params=params)
        if isinstance(result, list):
            return result
        # Should not happen — /logs always returns a list
        return []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._token_client.aclose()

    async def __aenter__(self) -> Auth0HTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
