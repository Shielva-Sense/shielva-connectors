from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    LookerAuthError,
    LookerError,
    LookerNetworkError,
    LookerNotFoundError,
    LookerRateLimitError,
)

LOOKER_API_VERSION = "4.0"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_LOOK_LIMIT = 500


class LookerHTTPClient:
    """Low-level async HTTP client for the Looker REST API 4.0.

    Authentication uses OAuth2 client credentials: POST /api/4.0/login with
    form-encoded client_id + client_secret to obtain a Bearer access token.
    """

    def __init__(
        self,
        config: dict[str, Any],
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        base_url: str = config.get("base_url", "").rstrip("/")
        self._base_url: str = base_url
        self._client_id: str = config.get("client_id", "")
        self._client_secret: str = config.get("client_secret", "")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._access_token: str = ""

    def _api_url(self, path: str) -> str:
        return f"{self._base_url}/api/{LOOKER_API_VERSION}{path}"

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> Any:
        """Execute an HTTP request and return parsed JSON (or {} on 204)."""
        url = self._api_url(path)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=self._auth_headers(),
                    params=params,
                    data=data,  # form-encoded for login
                ) as response:
                    return await self._raise_for_status(response)
        except (LookerError,):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise LookerNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            raise LookerNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise LookerNetworkError(f"Network error: {exc}") from exc

    async def _raise_for_status(self, response: aiohttp.ClientResponse) -> Any:
        status = response.status
        if status in (200, 201, 204):
            if status == 204:
                return {}
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        body: Any = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        # Looker REST API error responses are JSON with a "message" field or plain text
        if isinstance(body, dict):
            err_msg = body.get("message", str(body) or "Unknown Looker error")
            err_code = str(body.get("documentation_url", ""))
        else:
            err_msg = str(body) if body else "Unknown Looker error"
            err_code = ""

        if status == 401:
            raise LookerAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if status == 403:
            raise LookerAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if status == 404:
            raise LookerNotFoundError(err_code or "resource", err_msg)
        if status == 429:
            retry_after_raw = response.headers.get("Retry-After", "0")
            try:
                retry_after = float(retry_after_raw)
            except ValueError:
                retry_after = 0.0
            raise LookerRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if status >= 500:
            raise LookerError(
                f"Looker server error {status}: {err_msg}",
                status,
                err_code,
            )
        raise LookerError(
            f"Looker error {status}: {err_msg}",
            status,
            err_code,
        )

    # ── Authentication ────────────────────────────────────────────────────────

    async def login(self) -> dict[str, Any]:
        """POST /api/4.0/login with form-encoded client_id + client_secret.

        Stores the access_token on the client for subsequent Bearer auth calls.
        Response: {"access_token": "...", "token_type": "Bearer", "expires_in": 3600}
        """
        form_data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        result: dict[str, Any] = await self._request(
            "POST",
            "/login",
            data=form_data,
        )
        self._access_token = result.get("access_token", "")
        return result

    # ── LookML Models ─────────────────────────────────────────────────────────

    async def get_all_lookml_models(self) -> list[dict[str, Any]]:
        """GET /api/4.0/lookml_models — return all LookML models."""
        result = await self._request("GET", "/lookml_models")
        if isinstance(result, list):
            return result
        return []

    # ── Looks ─────────────────────────────────────────────────────────────────

    async def get_all_looks(self, limit: int = DEFAULT_LOOK_LIMIT) -> list[dict[str, Any]]:
        """GET /api/4.0/looks — return all saved Looks."""
        result = await self._request(
            "GET",
            "/looks",
            params={"limit": limit},
        )
        if isinstance(result, list):
            return result
        return []

    async def get_look(self, look_id: int | str) -> dict[str, Any]:
        """GET /api/4.0/looks/{look_id} — return a single Look by ID."""
        result = await self._request("GET", f"/looks/{look_id}")
        if isinstance(result, dict):
            return result
        return {}

    # ── Dashboards ────────────────────────────────────────────────────────────

    async def get_all_dashboards(self) -> list[dict[str, Any]]:
        """GET /api/4.0/dashboards — return all dashboards."""
        result = await self._request("GET", "/dashboards")
        if isinstance(result, list):
            return result
        return []

    async def get_dashboard(self, dashboard_id: int | str) -> dict[str, Any]:
        """GET /api/4.0/dashboards/{dashboard_id} — return a single dashboard."""
        result = await self._request("GET", f"/dashboards/{dashboard_id}")
        if isinstance(result, dict):
            return result
        return {}

    # ── Explores ──────────────────────────────────────────────────────────────

    async def get_all_explores(self, lookml_model_name: str) -> list[dict[str, Any]]:
        """GET /api/4.0/lookml_models/{lookml_model_name}/explores — list explores."""
        result = await self._request(
            "GET",
            f"/lookml_models/{lookml_model_name}/explores",
        )
        if isinstance(result, list):
            return result
        return []

    # ── Run Look ──────────────────────────────────────────────────────────────

    async def run_look(
        self,
        look_id: int | str,
        result_format: str = "json",
        limit: int = 100,
    ) -> Any:
        """GET /api/4.0/looks/{look_id}/run/{result_format} — execute and return data."""
        return await self._request(
            "GET",
            f"/looks/{look_id}/run/{result_format}",
            params={"limit": limit},
        )

    # ── Current User ──────────────────────────────────────────────────────────

    async def get_user_me(self) -> dict[str, Any]:
        """GET /api/4.0/user — return the current authenticated user."""
        result = await self._request("GET", "/user")
        if isinstance(result, dict):
            return result
        return {}

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def access_token(self) -> str:
        return self._access_token
