"""Low-level async HTTP client for the Wrike REST API v4."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import aiohttp

from exceptions import (
    WrikeAuthError,
    WrikeError,
    WrikeNetworkError,
    WrikeNotFoundError,
    WrikeRateLimitError,
)

BASE_URL: str = "https://www.wrike.com/api/v4"
TOKEN_URL: str = "https://login.wrike.com/oauth2/token"
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_PAGE_SIZE: int = 1000


class WrikeHTTPClient:
    """Low-level async HTTP client for the Wrike REST API v4.

    Authentication uses Bearer token sent in the Authorization header.
    Wrike wraps all successful responses in ``{"kind": "...", "data": [...]}``;
    this client returns the raw response body (not unwrapped) so callers can
    access both ``data`` and pagination cursors (``nextPageToken``).

    Token refresh is handled automatically when ``refresh_token`` is present in
    the config and a 401 response is received.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._config: dict[str, Any] = config or {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _access_token(self) -> str:
        return self._config.get("access_token", "") or ""

    def _headers(self) -> dict[str, str]:
        token = self._access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _raise_for_status(self, status: int, body: dict[str, Any], path: str) -> None:
        """Map Wrike HTTP status codes to typed exceptions."""
        error_description: str = body.get("errorDescription", "") or ""
        error_code: str = body.get("error", "") or f"HTTP {status}"
        message = error_description or error_code

        if status in (401, 403):
            raise WrikeAuthError(
                f"Wrike authentication failed: {message}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise WrikeNotFoundError("resource", path)
        if status == 429:
            raise WrikeRateLimitError(f"Wrike rate limited: {message}")
        if status >= 500:
            raise WrikeNetworkError(
                f"Wrike server error {status}: {message}",
                status_code=status,
            )
        raise WrikeError(
            f"Wrike error {status}: {message}",
            status_code=status,
            code=error_code,
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        _retry_on_401: bool = True,
    ) -> dict[str, Any]:
        """Execute an HTTP request against the Wrike API.

        On a 401 the client attempts one token refresh (if refresh_token is
        configured) before raising WrikeAuthError.
        """
        url = f"{BASE_URL}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json,
            ) as response:
                if response.status in (200, 201):
                    body = await response.json(content_type=None)
                    return body if isinstance(body, dict) else {"data": body}
                if response.status == 204:
                    return {}

                body_raw: dict[str, Any] = {}
                try:
                    body_raw = await response.json(content_type=None)
                except Exception:
                    pass

                # Attempt token refresh on 401 if refresh_token available
                if response.status == 401 and _retry_on_401:
                    refresh_token = self._config.get("refresh_token", "")
                    if refresh_token:
                        try:
                            refreshed = await self.refresh_access_token()
                            self._config["access_token"] = refreshed.get(
                                "access_token", self._config["access_token"]
                            )
                            if "refresh_token" in refreshed:
                                self._config["refresh_token"] = refreshed["refresh_token"]
                            return await self._request(
                                method, path, params=params, json=json,
                                _retry_on_401=False,
                            )
                        except WrikeAuthError:
                            pass

                self._raise_for_status(response.status, body_raw, path)
                # unreachable — _raise_for_status always raises
                raise WrikeError(f"Unexpected status {response.status}")

        except (WrikeError,):
            raise
        except aiohttp.ClientConnectorError as exc:
            raise WrikeNetworkError(f"Wrike connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise WrikeNetworkError(f"Wrike request timed out: {exc}") from exc
        except Exception as exc:
            raise WrikeNetworkError(f"Wrike network error: {exc}") from exc

    # ── Auth / token ──────────────────────────────────────────────────────────

    async def refresh_access_token(self) -> dict[str, Any]:
        """POST to the Wrike token endpoint to exchange refresh_token for a new access_token.

        Returns the full token response dict (access_token, refresh_token, etc.).
        """
        client_id = self._config.get("client_id", "")
        client_secret = self._config.get("client_secret", "")
        refresh_token = self._config.get("refresh_token", "")

        if not refresh_token:
            raise WrikeAuthError(
                "Cannot refresh token: refresh_token is not configured",
                code="no_refresh_token",
            )

        session = self._get_session()
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
        try:
            async with session.post(TOKEN_URL, data=data) as response:
                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass
                if response.status == 200:
                    return body
                raise WrikeAuthError(
                    f"Token refresh failed: {body.get('error_description', body.get('error', f'HTTP {response.status}'))}",
                    status_code=response.status,
                    code="token_refresh_failed",
                )
        except (WrikeError,):
            raise
        except Exception as exc:
            raise WrikeNetworkError(f"Token refresh network error: {exc}") from exc

    # ── Contacts / users ──────────────────────────────────────────────────────

    async def get_contacts(self, me: bool = False) -> dict[str, Any]:
        """GET /contacts[?me=true] — list contacts or current user.

        Returns ``{"kind": "contacts", "data": [...]}``.
        """
        params: dict[str, Any] = {}
        if me:
            params["me"] = "true"
        return await self._request("GET", "/contacts", params=params)

    async def get_users(self) -> dict[str, Any]:
        """GET /contacts — returns all users (contacts).

        Alias of get_contacts() for semantic clarity.
        """
        return await self._request("GET", "/contacts")

    # ── Folders ───────────────────────────────────────────────────────────────

    async def get_folders(self) -> dict[str, Any]:
        """GET /folders — list all folders in all accounts.

        Returns ``{"kind": "folders", "data": [...]}``.
        """
        return await self._request("GET", "/folders")

    async def get_folder(self, folder_id: str) -> dict[str, Any]:
        """GET /folders/{folder_id} — fetch a single folder."""
        return await self._request("GET", f"/folders/{folder_id}")

    # ── Tasks ─────────────────────────────────────────────────────────────────

    async def get_tasks(
        self,
        folder_id: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        next_page_token: str | None = None,
    ) -> dict[str, Any]:
        """GET /tasks or /folders/{folder_id}/tasks — list tasks.

        Args:
            folder_id: Scope tasks to a specific folder/project. If None, lists
                       all tasks across all accessible spaces.
            page_size: Number of tasks per page (Wrike max 1000).
            next_page_token: Pagination cursor from a previous response's
                             ``nextPageToken`` field.

        Returns the raw response dict which includes ``data`` (list of tasks)
        and optionally ``nextPageToken`` for pagination.
        """
        params: dict[str, Any] = {"pageSize": page_size}
        if next_page_token:
            params["nextPageToken"] = next_page_token

        path = f"/folders/{folder_id}/tasks" if folder_id else "/tasks"
        return await self._request("GET", path, params=params)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """GET /tasks/{task_id} — fetch a single task by ID.

        Returns ``{"kind": "tasks", "data": [task]}``.
        """
        return await self._request("GET", f"/tasks/{task_id}")

    # ── Comments ──────────────────────────────────────────────────────────────

    async def get_comments(
        self,
        next_page_token: str | None = None,
    ) -> dict[str, Any]:
        """GET /comments — list all comments in all accounts.

        Args:
            next_page_token: Pagination cursor from a previous response.

        Returns ``{"kind": "comments", "data": [...]}``.
        """
        params: dict[str, Any] = {}
        if next_page_token:
            params["nextPageToken"] = next_page_token
        return await self._request("GET", "/comments", params=params)

    async def get_task_comments(self, task_id: str) -> dict[str, Any]:
        """GET /tasks/{task_id}/comments — list comments for a specific task."""
        return await self._request("GET", f"/tasks/{task_id}/comments")

    # ── Timelogs ─────────────────────────────────────────────────────────────

    async def get_timelogs(self) -> dict[str, Any]:
        """GET /timelogs — list all timelogs in all accounts.

        Returns ``{"kind": "timelogs", "data": [...]}``.
        """
        return await self._request("GET", "/timelogs")

    # ── Workflows ─────────────────────────────────────────────────────────────

    async def get_workflows(self) -> dict[str, Any]:
        """GET /workflows — list all workflows.

        Returns ``{"kind": "workflows", "data": [...]}``.
        """
        return await self._request("GET", "/workflows")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> WrikeHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
