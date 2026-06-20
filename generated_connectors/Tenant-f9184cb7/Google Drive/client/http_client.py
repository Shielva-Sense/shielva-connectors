"""All Google Drive API HTTP calls — zero business logic, zero normalization."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp

from exceptions import (
    GoogleDriveAuthError,
    GoogleDriveError,
    GoogleDriveNetworkError,
    GoogleDriveNotFoundError,
    GoogleDriveRateLimitError,
)

_DRIVE_BASE = "https://www.googleapis.com/drive/v3"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Default file fields for list/get operations
_DEFAULT_FILE_FIELDS = (
    "nextPageToken,files(id,name,mimeType,size,createdTime,modifiedTime,"
    "webViewLink,parents,owners,shared,starred)"
)


class GoogleDriveHTTPClient:
    """Thin async HTTP client for the Google Drive REST API v3.

    All methods accept an *access_token* and return raw response dicts.
    Retry logic is handled by the caller via helpers/utils.with_retry().
    """

    def __init__(self, base_url: str = _DRIVE_BASE) -> None:
        self._base_url = base_url.rstrip("/")

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse, body: Dict[str, Any]
    ) -> None:
        """Map HTTP error codes to connector exceptions.

        Signature: (response, body) — body is already-parsed JSON dict.
        Callers must parse the body before calling this method.
        """
        status = response.status
        if status < 400:
            return

        error_obj = body.get("error", {})
        if isinstance(error_obj, dict):
            message = error_obj.get("message", "") or str(body)
        else:
            message = str(error_obj) or str(body)

        if status in (401, 403):
            raise GoogleDriveAuthError(
                f"{status} {'Unauthorized' if status == 401 else 'Forbidden'}: {message}",
                status_code=status,
            )
        if status == 404:
            raise GoogleDriveNotFoundError("resource", message or "unknown")
        if status == 429:
            raise GoogleDriveRateLimitError(
                f"429 Rate limit exceeded: {message}",
                retry_after=float(response.headers.get("Retry-After", "5")),
            )
        if status >= 500:
            raise GoogleDriveNetworkError(
                f"HTTP {status} server error: {message}",
                status_code=status,
            )
        raise GoogleDriveError(
            f"HTTP {status}: {message}",
            status_code=status,
        )

    async def _get_json(
        self,
        access_token: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """GET a JSON endpoint with auth header."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=self._auth_headers(access_token), params=params
                ) as resp:
                    try:
                        body: Dict[str, Any] = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    await self._raise_for_status(resp, body)
                    return body
        except (GoogleDriveAuthError, GoogleDriveError):
            raise
        except aiohttp.ClientConnectionError as exc:
            raise GoogleDriveNetworkError(f"Connection error{': ' + context if context else ''}: {exc}") from exc
        except aiohttp.ClientTimeout as exc:
            raise GoogleDriveNetworkError(f"Timeout{': ' + context if context else ''}: {exc}") from exc

    async def get_about(self, access_token: str) -> Dict[str, Any]:
        """GET /about?fields=user,storageQuota — returns authenticated user info + quota."""
        url = f"{self._base_url}/about"
        params = {"fields": "user,storageQuota"}
        return await self._get_json(access_token, url, params, "get_about")

    async def list_files(
        self,
        access_token: str,
        page_size: int = 100,
        query: Optional[str] = None,
        page_token: Optional[str] = None,
        fields: str = _DEFAULT_FILE_FIELDS,
    ) -> Dict[str, Any]:
        """GET /files — list files with metadata and optional query filter."""
        url = f"{self._base_url}/files"
        params: Dict[str, Any] = {
            "pageSize": page_size,
            "fields": fields,
        }
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        return await self._get_json(access_token, url, params, "list_files")

    async def list_folders(
        self,
        access_token: str,
        page_size: int = 100,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /files?q=mimeType='application/vnd.google-apps.folder' — list folders only."""
        return await self.list_files(
            access_token,
            page_size=page_size,
            query="mimeType='application/vnd.google-apps.folder'",
            page_token=page_token,
        )

    async def get_file(
        self, access_token: str, file_id: str
    ) -> Dict[str, Any]:
        """GET /files/{file_id}?fields=* — fetch full file metadata."""
        url = f"{self._base_url}/files/{file_id}"
        params = {"fields": "*"}
        return await self._get_json(access_token, url, params, f"get_file({file_id})")

    async def search_files(
        self,
        access_token: str,
        query: str,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """GET /files?q=<query> — search files by query string."""
        return await self.list_files(
            access_token,
            page_size=page_size,
            query=query,
        )

    async def list_drives(
        self,
        access_token: str,
        page_size: int = 100,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /drives — list shared drives accessible to the authenticated user."""
        url = f"{self._base_url}/drives"
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        return await self._get_json(access_token, url, params, "list_drives")

    async def get_permissions(
        self, access_token: str, file_id: str
    ) -> Dict[str, Any]:
        """GET /files/{file_id}/permissions — list permissions for a file."""
        url = f"{self._base_url}/files/{file_id}/permissions"
        return await self._get_json(access_token, url, context=f"get_permissions({file_id})")

    async def export_file(
        self, access_token: str, file_id: str, mime_type: str
    ) -> bytes:
        """GET /files/{file_id}/export?mimeType={mime_type} — export a Google Docs file."""
        url = f"{self._base_url}/files/{file_id}/export"
        params = {"mimeType": mime_type}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=self._auth_headers(access_token), params=params
                ) as resp:
                    try:
                        body: Dict[str, Any] = await resp.json(content_type=None) if resp.content_type and "json" in resp.content_type else {}
                    except Exception:
                        body = {}
                    await self._raise_for_status(resp, body)
                    return await resp.read()
        except (GoogleDriveAuthError, GoogleDriveError):
            raise
        except Exception as exc:
            raise GoogleDriveNetworkError(f"export_file({file_id}): {exc}") from exc

    async def exchange_code_for_token(
        self,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str = "",
    ) -> Dict[str, Any]:
        """POST to token URL to exchange an auth code for access + refresh tokens."""
        payload: Dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        return await self.post_form_data(url=_TOKEN_URL, payload=payload, context="exchange_code_for_token")

    async def refresh_access_token(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> Dict[str, Any]:
        """POST to token URL to refresh an access token using a refresh token."""
        payload: Dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        return await self.post_form_data(url=_TOKEN_URL, payload=payload, context="refresh_access_token")

    async def post_form_data(
        self,
        url: str,
        payload: Dict[str, str],
        context: str = "post_form_data",
    ) -> Dict[str, Any]:
        """Generic POST of form-encoded data to any URL — returns parsed JSON.

        Used by connector.py to exchange and renew OAuth tokens.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload) as resp:
                    try:
                        body: Dict[str, Any] = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    await self._raise_for_status(resp, body)
                    return body
        except (GoogleDriveAuthError, GoogleDriveError):
            raise
        except Exception as exc:
            raise GoogleDriveNetworkError(f"{context}: {exc}") from exc
