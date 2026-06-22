from __future__ import annotations

import base64
import time
from typing import Any

import httpx

from exceptions import (
    ZoomAuthError,
    ZoomError,
    ZoomNetworkError,
    ZoomNotFoundError,
    ZoomRateLimitError,
    ZoomServerError,
)

ZOOM_BASE_URL = "https://api.zoom.us/v2"
ZOOM_OAUTH_TOKEN_URL = "https://zoom.us/oauth/token"
DEFAULT_TIMEOUT_S = 30.0


class ZoomHTTPClient:
    """Low-level async HTTP client for the Zoom REST API v2.

    Uses Server-to-Server OAuth (account_credentials grant) — no user redirect needed.
    The access token is obtained via ``get_token()`` and cached in ``config``.
    It is refreshed automatically when expired.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._config: dict[str, Any] = config if config is not None else {}
        self._account_id: str = self._config.get("account_id", "")
        self._client_id: str = self._config.get("client_id", "")
        self._client_secret: str = self._config.get("client_secret", "")
        self._access_token: str = self._config.get("access_token", "")
        self._token_expires_at: float = self._config.get("token_expires_at", 0.0)
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=ZOOM_BASE_URL,
            timeout=timeout,
        )

    # ── Token management ─────────────────────────────────────────────────────

    async def get_token(self) -> str:
        """Exchange account credentials for a Bearer token (S2S OAuth).

        POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id={account_id}
        BasicAuth: client_id:client_secret

        Returns the access_token string and stores it in the config dict.
        """
        raw = f"{self._client_id}:{self._client_secret}"
        basic = "Basic " + base64.b64encode(raw.encode()).decode()
        try:
            resp = await self._client.post(
                ZOOM_OAUTH_TOKEN_URL,
                params={
                    "grant_type": "account_credentials",
                    "account_id": self._account_id,
                },
                headers={"Authorization": basic},
            )
        except httpx.TimeoutException as exc:
            raise ZoomNetworkError(f"Token request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise ZoomNetworkError(f"Network error during token exchange: {exc}") from exc

        if resp.status_code not in (200, 201):
            body: dict[str, Any] = {}
            try:
                body = resp.json()
            except Exception:
                pass
            err = body.get("reason") or body.get("message") or resp.text or "Token exchange failed"
            raise ZoomAuthError(f"Token exchange failed: {err}", resp.status_code)

        data: dict[str, Any] = resp.json()
        token: str = data.get("access_token", "")
        expires_in: int = int(data.get("expires_in", 3600))
        self._access_token = token
        self._token_expires_at = time.monotonic() + expires_in - 60  # 60s safety margin
        # Propagate into config so callers can persist it
        self._config["access_token"] = token
        self._config["token_expires_at"] = self._token_expires_at
        return token

    def _is_token_expired(self) -> bool:
        if not self._access_token:
            return True
        return time.monotonic() >= self._token_expires_at

    async def _auth_header(self) -> str:
        if self._is_token_expired():
            await self.get_token()
        return f"Bearer {self._access_token}"

    # ── Core request ─────────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        auth = await self._auth_header()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = auth
        headers.setdefault("Content-Type", "application/json")
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise ZoomNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise ZoomNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        err_msg = (
            body.get("message")
            or body.get("error")
            or response.text
            or "Unknown Zoom error"
        )
        err_code = body.get("code", "")
        err_code_str = str(err_code) if err_code else ""

        if response.status_code == 401:
            raise ZoomAuthError(
                f"Authentication failed: {err_msg}", 401, err_code_str
            )
        if response.status_code == 403:
            raise ZoomAuthError(f"Forbidden: {err_msg}", 403, err_code_str)
        if response.status_code == 404:
            raise ZoomNotFoundError(body.get("message", "resource"), str(path))
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise ZoomRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise ZoomServerError(
                f"Zoom server error {response.status_code}: {err_msg}",
                response.status_code,
            )

        raise ZoomError(
            f"Zoom error {response.status_code}: {err_msg}",
            response.status_code,
            err_code_str,
        )

    # ── Account / health ─────────────────────────────────────────────────────

    async def get_account_info(self) -> dict[str, Any]:
        """Probe endpoint: GET /accounts/me — used for health check."""
        return await self._request("GET", "/accounts/me")

    # ── Users ────────────────────────────────────────────────────────────────

    async def get_users(
        self,
        status: str = "active",
        page_size: int = 300,
        next_page_token: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "status": status,
            "page_size": page_size,
        }
        if next_page_token:
            params["next_page_token"] = next_page_token
        return await self._request("GET", "/users", params=params)

    # ── Meetings ─────────────────────────────────────────────────────────────

    async def get_meetings(
        self,
        user_id: str = "me",
        type: str = "scheduled",
        page_size: int = 300,
        next_page_token: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "type": type,
            "page_size": page_size,
        }
        if next_page_token:
            params["next_page_token"] = next_page_token
        return await self._request("GET", f"/users/{user_id}/meetings", params=params)

    # ── Recordings ───────────────────────────────────────────────────────────

    async def get_recordings(
        self,
        user_id: str = "me",
        from_date: str = "",
        to_date: str = "",
        page_size: int = 300,
        next_page_token: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if next_page_token:
            params["next_page_token"] = next_page_token
        return await self._request("GET", f"/users/{user_id}/recordings", params=params)

    # ── Webinars ─────────────────────────────────────────────────────────────

    async def get_webinars(
        self,
        user_id: str = "me",
        page_size: int = 300,
        next_page_token: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if next_page_token:
            params["next_page_token"] = next_page_token
        return await self._request("GET", f"/users/{user_id}/webinars", params=params)

    # ── Legacy aliases kept for backward compat with existing tests ──────────

    async def get_me(self) -> dict[str, Any]:
        """Alias of get_account_info for backward compatibility."""
        return await self.get_account_info()

    async def list_meetings(
        self,
        user_id: str = "me",
        meeting_type: str = "scheduled",
        page_size: int = 300,
        next_page_token: str = "",
    ) -> dict[str, Any]:
        return await self.get_meetings(
            user_id=user_id,
            type=meeting_type,
            page_size=page_size,
            next_page_token=next_page_token,
        )

    async def list_recordings(
        self,
        user_id: str = "me",
        from_date: str = "",
        to_date: str = "",
        page_size: int = 300,
        next_page_token: str = "",
    ) -> dict[str, Any]:
        return await self.get_recordings(
            user_id=user_id,
            from_date=from_date,
            to_date=to_date,
            page_size=page_size,
            next_page_token=next_page_token,
        )

    async def list_webinars(
        self,
        user_id: str = "me",
        page_size: int = 300,
        next_page_token: str = "",
    ) -> dict[str, Any]:
        return await self.get_webinars(
            user_id=user_id,
            page_size=page_size,
            next_page_token=next_page_token,
        )

    async def get_meeting(self, meeting_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/meetings/{meeting_id}")

    async def get_recording(self, meeting_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/meetings/{meeting_id}/recordings")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ZoomHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
