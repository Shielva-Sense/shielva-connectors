from __future__ import annotations

import time
from typing import Any

import aiohttp

from exceptions import (
    PowerBIAuthError,
    PowerBIError,
    PowerBINetworkError,
    PowerBINotFoundError,
    PowerBIRateLimitError,
)

POWERBI_API_BASE = "https://api.powerbi.com"
OAUTH_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id_azure}/oauth2/v2.0/token"
DEFAULT_TIMEOUT_S = 30.0
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default offline_access"


class PowerBIHTTPClient:
    """
    Low-level async HTTP client for the Microsoft Power BI REST API v1.0.

    Uses aiohttp.ClientSession internally. Automatically refreshes the access
    token when it is near expiry (within 60 seconds of token_expires_at).
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._client_id: str = cfg.get("client_id", "")
        self._client_secret: str = cfg.get("client_secret", "")
        self._az_tenant_id: str = cfg.get("tenant_id_azure", "")
        self._access_token: str = cfg.get("access_token", "")
        self._refresh_token: str = cfg.get("refresh_token", "")
        self._token_expires_at: float = float(cfg.get("token_expires_at", 0))
        self._timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_S)
        self._session: aiohttp.ClientSession | None = None

    # ── Session management ────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ── Token refresh ─────────────────────────────────────────────────────────

    def _is_token_expired(self) -> bool:
        """True if no token yet or token expires within 60 seconds."""
        if not self._access_token:
            return True
        if self._token_expires_at == 0:
            return False  # no expiry info → assume valid
        return time.monotonic() >= self._token_expires_at - 60

    async def refresh_token(self) -> dict[str, Any]:
        """
        Exchange the refresh_token for a new access_token using the Microsoft identity token endpoint.
        Updates internal state and returns the raw token response dict.
        """
        if not self._refresh_token:
            raise PowerBIAuthError("No refresh_token available; re-authorize the connector.")
        if not self._az_tenant_id:
            raise PowerBIAuthError("tenant_id_azure is required to refresh the access token.")

        url = OAUTH_TOKEN_URL.format(tenant_id_azure=self._az_tenant_id)
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "scope": POWERBI_SCOPE,
        }
        session = self._get_session()
        try:
            async with session.post(url, data=data) as resp:
                body: dict[str, Any] = {}
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    pass
                if resp.status != 200:
                    err = body.get("error_description") or body.get("error") or f"HTTP {resp.status}"
                    raise PowerBIAuthError(f"Token refresh failed: {err}", resp.status)

                self._access_token = body.get("access_token", self._access_token)
                if "refresh_token" in body:
                    self._refresh_token = body["refresh_token"]
                expires_in = body.get("expires_in", 3600)
                self._token_expires_at = time.monotonic() + float(expires_in)
                return body
        except PowerBIAuthError:
            raise
        except aiohttp.ClientError as exc:
            raise PowerBINetworkError(f"Network error during token refresh: {exc}") from exc

    async def _ensure_token(self) -> None:
        """Refresh the access token if it is expired."""
        if self._is_token_expired():
            await self.refresh_token()

    # ── Low-level request ─────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._ensure_token()
        session = self._get_session()
        headers = self._auth_headers()
        try:
            async with session.request(
                method, url, headers=headers, params=params, json=json
            ) as resp:
                status = resp.status
                body: dict[str, Any] = {}
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    pass

                if status in (200, 201, 204):
                    return body

                self._raise_for_status(status, body)
                # unreachable — _raise_for_status always raises
                return body  # pragma: no cover
        except (PowerBIError,):
            raise
        except aiohttp.ClientConnectionError as exc:
            raise PowerBINetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise PowerBINetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise PowerBINetworkError(f"HTTP client error: {exc}") from exc

    @staticmethod
    def _raise_for_status(status: int, body: dict[str, Any]) -> None:
        """Map HTTP error codes to typed connector exceptions."""
        err_msg = (
            body.get("error", {}).get("message")
            if isinstance(body.get("error"), dict)
            else body.get("message")
            or f"HTTP {status}"
        ) or f"HTTP {status}"
        if status in (401, 403):
            raise PowerBIAuthError(
                f"Authentication failed ({status}): {err_msg}", status_code=status
            )
        if status == 404:
            raise PowerBINotFoundError(err_msg)
        if status == 429:
            raise PowerBIRateLimitError(f"Rate limited: {err_msg}")
        if status >= 500:
            raise PowerBINetworkError(
                f"Power BI server error {status}: {err_msg}", status_code=status
            )
        raise PowerBIError(f"Power BI error {status}: {err_msg}", status_code=status)

    # ── API URL builder ───────────────────────────────────────────────────────

    def _api_url(self, path: str) -> str:
        return f"{POWERBI_API_BASE}/{path.lstrip('/')}"

    # ── Power BI resource fetches ─────────────────────────────────────────────

    async def get_dashboards(self) -> list[dict[str, Any]]:
        """GET /v1.0/myorg/dashboards — returns list of dashboards in My Workspace."""
        url = self._api_url("v1.0/myorg/dashboards")
        result = await self._request("GET", url)
        return result.get("value", [])

    async def get_reports(self) -> list[dict[str, Any]]:
        """GET /v1.0/myorg/reports — returns list of reports in My Workspace."""
        url = self._api_url("v1.0/myorg/reports")
        result = await self._request("GET", url)
        return result.get("value", [])

    async def get_datasets(self) -> list[dict[str, Any]]:
        """GET /v1.0/myorg/datasets — returns list of datasets in My Workspace."""
        url = self._api_url("v1.0/myorg/datasets")
        result = await self._request("GET", url)
        return result.get("value", [])

    async def get_workspaces(self) -> list[dict[str, Any]]:
        """GET /v1.0/myorg/groups — returns list of workspaces (groups)."""
        url = self._api_url("v1.0/myorg/groups")
        result = await self._request("GET", url)
        return result.get("value", [])

    async def get_workspace_reports(self, workspace_id: str) -> list[dict[str, Any]]:
        """GET /v1.0/myorg/groups/{workspace_id}/reports — returns reports in a workspace."""
        url = self._api_url(f"v1.0/myorg/groups/{workspace_id}/reports")
        result = await self._request("GET", url)
        return result.get("value", [])

    async def get_workspace_dashboards(self, workspace_id: str) -> list[dict[str, Any]]:
        """GET /v1.0/myorg/groups/{workspace_id}/dashboards — returns dashboards in a workspace."""
        url = self._api_url(f"v1.0/myorg/groups/{workspace_id}/dashboards")
        result = await self._request("GET", url)
        return result.get("value", [])

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> PowerBIHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
