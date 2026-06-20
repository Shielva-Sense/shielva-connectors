from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    DropboxAuthError,
    DropboxError,
    DropboxNetworkError,
    DropboxNotFoundError,
    DropboxRateLimitError,
)

DROPBOX_API_BASE = "https://api.dropboxapi.com/2"
DEFAULT_TIMEOUT_S = 30.0


class DropboxHTTPClient:
    """Low-level async HTTP client for the Dropbox API v2.

    All Dropbox API v2 calls use POST with a JSON body to
    https://api.dropboxapi.com/2/<endpoint>.
    """

    def __init__(
        self,
        access_token: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._client = httpx.AsyncClient(
            base_url=DROPBOX_API_BASE,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )

    async def _request(
        self, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST to a Dropbox API v2 endpoint with an optional JSON body."""
        try:
            response = await self._client.post(path, json=body or {})
        except httpx.TimeoutException as exc:
            raise DropboxNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise DropboxNetworkError(f"Network error: {exc}") from exc

        if response.status_code == 200:
            if not response.content:
                return {}
            return response.json()

        # Parse error body
        err_body: dict[str, Any] = {}
        try:
            err_body = response.json()
        except Exception:
            pass

        err_summary = (
            err_body.get("error_summary")
            or err_body.get("error", {}).get(".tag", "")
            or response.text
            or "Unknown Dropbox error"
        )
        err_tag = err_body.get("error", {}).get(".tag", "")

        if response.status_code == 401:
            raise DropboxAuthError(
                f"Authentication failed: {err_summary}", 401, "invalid_access_token"
            )
        if response.status_code == 403:
            raise DropboxAuthError(
                f"Forbidden: {err_summary}", 403, "forbidden"
            )
        if response.status_code == 409:
            # Dropbox encodes not-found as 409 with tag "not_found" or "path/not_found"
            if "not_found" in err_tag or "not_found" in err_summary:
                raise DropboxNotFoundError("path", err_summary)
            raise DropboxError(
                f"Dropbox conflict/path error: {err_summary}", 409, err_tag
            )
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise DropboxRateLimitError(f"Rate limited: {err_summary}", retry_after)
        if response.status_code >= 500:
            raise DropboxError(
                f"Dropbox server error {response.status_code}: {err_summary}",
                response.status_code,
                "server_error",
            )

        raise DropboxError(
            f"Dropbox error {response.status_code}: {err_summary}",
            response.status_code,
            err_tag,
        )

    # ── Auth / account ────────────────────────────────────────────────────────

    async def get_current_account(self) -> dict[str, Any]:
        """POST /users/get_current_account — returns account info.

        Used for health_check().  Dropbox v2: body must be null / empty.
        """
        return await self._request("/users/get_current_account", None)

    # ── Folder listing ────────────────────────────────────────────────────────

    async def list_folder(
        self, path: str = "", recursive: bool = False, limit: int = 200
    ) -> dict[str, Any]:
        """POST /files/list_folder."""
        body: dict[str, Any] = {
            "path": path,
            "recursive": recursive,
            "limit": limit,
            "include_media_info": False,
            "include_deleted": False,
            "include_has_explicit_shared_members": False,
        }
        return await self._request("/files/list_folder", body)

    async def list_folder_continue(self, cursor: str) -> dict[str, Any]:
        """POST /files/list_folder/continue — paginate with cursor."""
        return await self._request("/files/list_folder/continue", {"cursor": cursor})

    # ── Metadata ─────────────────────────────────────────────────────────────

    async def get_metadata(self, path: str) -> dict[str, Any]:
        """POST /files/get_metadata."""
        body: dict[str, Any] = {
            "path": path,
            "include_media_info": False,
            "include_deleted": False,
            "include_has_explicit_shared_members": False,
        }
        return await self._request("/files/get_metadata", body)

    # ── Search ────────────────────────────────────────────────────────────────

    async def search_files(
        self, query: str, max_results: int = 100
    ) -> dict[str, Any]:
        """POST /files/search_v2."""
        body: dict[str, Any] = {
            "query": query,
            "options": {
                "max_results": max_results,
                "file_status": "active",
            },
        }
        return await self._request("/files/search_v2", body)

    # ── Sharing ───────────────────────────────────────────────────────────────

    async def list_shared_links(
        self,
        path: str | None = None,
        cursor: str | None = None,
        direct_only: bool = True,
    ) -> dict[str, Any]:
        """POST /sharing/list_shared_links."""
        body: dict[str, Any] = {"direct_only": direct_only}
        if path is not None:
            body["path"] = path
        if cursor is not None:
            body["cursor"] = cursor
        return await self._request("/sharing/list_shared_links", body)

    # ── Space / team ──────────────────────────────────────────────────────────

    async def get_space_usage(self) -> dict[str, Any]:
        """POST /users/get_space_usage — returns allocation and used bytes."""
        return await self._request("/users/get_space_usage", None)

    async def list_team_members(
        self, cursor: str | None = None, limit: int = 300
    ) -> dict[str, Any]:
        """POST /team/members/list_v2 — team apps only."""
        body: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            body["cursor"] = cursor
        return await self._request("/team/members/list_v2", body)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> DropboxHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
