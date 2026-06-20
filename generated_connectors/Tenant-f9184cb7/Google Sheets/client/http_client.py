from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    GoogleSheetsAuthError,
    GoogleSheetsError,
    GoogleSheetsNetworkError,
    GoogleSheetsNotFoundError,
    GoogleSheetsRateLimitError,
)

SHEETS_BASE_URL = "https://sheets.googleapis.com/v4"
DRIVE_BASE_URL = "https://www.googleapis.com/drive/v3"
OAUTH_BASE_URL = "https://www.googleapis.com"
DEFAULT_TIMEOUT_S = 30.0


class GoogleSheetsHTTPClient:
    """Low-level async HTTP client for the Google Sheets and Drive REST APIs."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth_header(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    async def _request(
        self,
        method: str,
        url: str,
        access_token: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._get_session()
        headers = self._auth_header(access_token)
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
            ) as response:
                status = response.status

                if status == 200:
                    return await response.json()  # type: ignore[no-any-return]

                body: dict[str, Any] = {}
                try:
                    body = await response.json()
                except Exception:
                    pass

                error = body.get("error", {})
                if isinstance(error, dict):
                    err_msg = error.get("message", "") or str(body)
                    err_code = str(error.get("code", status))
                else:
                    err_msg = str(error)
                    err_code = str(status)

                if status in (401, 403):
                    raise GoogleSheetsAuthError(
                        f"Authentication failed ({status}): {err_msg}",
                        status_code=status,
                        code=err_code,
                    )
                if status == 404:
                    raise GoogleSheetsNotFoundError("resource", err_code)
                if status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise GoogleSheetsRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )
                if status >= 500:
                    raise GoogleSheetsNetworkError(
                        f"Google API server error {status}: {err_msg}",
                        status_code=status,
                    )
                raise GoogleSheetsError(
                    f"Google API error {status}: {err_msg}",
                    status_code=status,
                    code=err_code,
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise GoogleSheetsNetworkError(f"Network error: {exc}") from exc
        except (
            GoogleSheetsAuthError,
            GoogleSheetsNetworkError,
            GoogleSheetsRateLimitError,
            GoogleSheetsNotFoundError,
            GoogleSheetsError,
        ):
            raise

    # ── Sheets API ────────────────────────────────────────────────────────────

    async def get_spreadsheet(
        self, access_token: str, spreadsheet_id: str
    ) -> dict[str, Any]:
        """GET /v4/spreadsheets/{id} — metadata + sheet list."""
        url = f"{SHEETS_BASE_URL}/spreadsheets/{spreadsheet_id}"
        return await self._request("GET", url, access_token)

    async def get_values(
        self,
        access_token: str,
        spreadsheet_id: str,
        range_: str,
    ) -> dict[str, Any]:
        """GET /v4/spreadsheets/{id}/values/{range} — cell values."""
        url = f"{SHEETS_BASE_URL}/spreadsheets/{spreadsheet_id}/values/{range_}"
        return await self._request("GET", url, access_token)

    async def get_spreadsheet_values_batch(
        self,
        access_token: str,
        spreadsheet_id: str,
        ranges: list[str],
    ) -> dict[str, Any]:
        """GET /v4/spreadsheets/{id}/values:batchGet — multiple ranges at once."""
        url = f"{SHEETS_BASE_URL}/spreadsheets/{spreadsheet_id}/values:batchGet"
        params: dict[str, Any] = {"ranges": ranges}
        session = self._get_session()
        headers = self._auth_header(access_token)
        # batchGet uses repeated 'ranges' query params — aiohttp supports list values
        try:
            async with session.get(url, headers=headers, params=params) as response:
                status = response.status
                if status == 200:
                    return await response.json()  # type: ignore[no-any-return]
                body: dict[str, Any] = {}
                try:
                    body = await response.json()
                except Exception:
                    pass
                error = body.get("error", {})
                err_msg = (
                    error.get("message", str(body))
                    if isinstance(error, dict)
                    else str(error)
                )
                err_code = str(
                    error.get("code", status) if isinstance(error, dict) else status
                )
                if status in (401, 403):
                    raise GoogleSheetsAuthError(
                        f"Authentication failed ({status}): {err_msg}",
                        status_code=status,
                        code=err_code,
                    )
                if status == 404:
                    raise GoogleSheetsNotFoundError("spreadsheet", spreadsheet_id)
                if status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise GoogleSheetsRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )
                if status >= 500:
                    raise GoogleSheetsNetworkError(
                        f"Google API server error {status}: {err_msg}",
                        status_code=status,
                    )
                raise GoogleSheetsError(
                    f"Google API error {status}: {err_msg}",
                    status_code=status,
                    code=err_code,
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise GoogleSheetsNetworkError(f"Network error: {exc}") from exc

    # ── Drive API ─────────────────────────────────────────────────────────────

    async def list_spreadsheets(
        self,
        access_token: str,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """GET Drive files filtered to Google Sheets mime type."""
        url = f"{DRIVE_BASE_URL}/files"
        params: dict[str, Any] = {
            "q": "mimeType='application/vnd.google-apps.spreadsheet'",
            "pageSize": page_size,
            "fields": "nextPageToken,files(id,name,modifiedTime,webViewLink,owners)",
        }
        if page_token:
            params["pageToken"] = page_token
        return await self._request("GET", url, access_token, params=params)

    # ── OAuth userinfo ────────────────────────────────────────────────────────

    async def get_userinfo(self, access_token: str) -> dict[str, Any]:
        """GET https://www.googleapis.com/oauth2/v2/userinfo — verify token + get email."""
        url = f"{OAUTH_BASE_URL}/oauth2/v2/userinfo"
        return await self._request("GET", url, access_token)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> GoogleSheetsHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
