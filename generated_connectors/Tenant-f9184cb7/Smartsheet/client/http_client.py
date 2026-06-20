"""Smartsheet connector — REST HTTP client."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp

from exceptions import (
    SmartsheetAuthError,
    SmartsheetError,
    SmartsheetNetworkError,
    SmartsheetNotFoundError,
    SmartsheetRateLimitError,
)

_BASE_URL = "https://api.smartsheet.com/2.0"
_DEFAULT_TIMEOUT = 30.0


class SmartsheetHTTPClient:
    """Thin async HTTP wrapper for the Smartsheet REST API 2.0.

    All requests use:
        Authorization: Bearer {api_token}
        Content-Type:  application/json

    Pagination uses ``pageNumber`` / ``totalPages`` for sheets and reports,
    and a flat ``includeAll=true`` for workspaces and folders.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        base_url: str = _BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._config = config or {}
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _get_token(self) -> str:
        return self._config.get("api_token", "")

    def _build_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def _raise_for_status(self, status: int, body: Dict[str, Any]) -> None:
        """Map HTTP status codes to typed exceptions."""
        if status == 200 or status == 201:
            return
        error_code = body.get("errorCode", 0)
        message = body.get("message", f"HTTP {status}")

        if status == 401 or status == 403:
            raise SmartsheetAuthError(
                f"[smartsheet] HTTP {status} — {message} (errorCode={error_code})"
            )
        if status == 404:
            raise SmartsheetNotFoundError(
                f"[smartsheet] HTTP 404 — {message} (errorCode={error_code})"
            )
        if status == 429:
            raise SmartsheetRateLimitError(
                f"[smartsheet] HTTP 429 — rate limit exceeded: {message}"
            )
        if 400 <= status < 500:
            raise SmartsheetError(
                f"[smartsheet] HTTP {status} — {message} (errorCode={error_code})"
            )
        if status >= 500:
            raise SmartsheetNetworkError(
                f"[smartsheet] HTTP {status} — server error: {message}"
            )

    async def _get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        context: str = "get",
    ) -> Dict[str, Any]:
        """Execute a GET request against the Smartsheet API."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url,
                    headers=self._build_headers(),
                    params=params,
                ) as resp:
                    body: Dict[str, Any] = await resp.json(content_type=None)
                    self._raise_for_status(resp.status, body)
                    return body
        except (SmartsheetError,):
            raise
        except (aiohttp.ClientError, aiohttp.ServerTimeoutError) as exc:
            raise SmartsheetNetworkError(
                f"[{context}] Network error: {exc}"
            ) from exc

    # ── /users/me ─────────────────────────────────────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /users/me — returns the authenticated user object."""
        return await self._get("users/me", context="get_current_user")

    # ── /sheets ───────────────────────────────────────────────────────────────

    async def get_sheets(
        self,
        page: int = 1,
        page_size: int = 100,
        include_all: bool = False,
    ) -> Dict[str, Any]:
        """GET /sheets — paginated list of sheets.

        Returns dict with keys: ``data`` (list), ``totalPages``, ``pageNumber``,
        ``totalCount``, ``pageSize``.
        """
        params: Dict[str, Any] = {}
        if include_all:
            params["includeAll"] = "true"
        else:
            params["page"] = page
            params["pageSize"] = page_size
        return await self._get("sheets", params=params, context="get_sheets")

    async def get_sheet(
        self,
        sheet_id: int,
        include: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /sheets/{id} — single sheet with columns and rows."""
        params: Dict[str, Any] = {}
        if include:
            params["include"] = ",".join(include)
        return await self._get(f"sheets/{sheet_id}", params=params, context="get_sheet")

    async def get_rows(
        self,
        sheet_id: int,
        page: int = 1,
        page_size: int = 500,
    ) -> Dict[str, Any]:
        """GET /sheets/{id}/rows — paginated rows for a sheet.

        Returns dict with keys: ``data`` (list of row objects), ``totalPages``,
        ``pageNumber``.
        """
        params: Dict[str, Any] = {
            "page": page,
            "pageSize": page_size,
        }
        return await self._get(
            f"sheets/{sheet_id}/rows",
            params=params,
            context="get_rows",
        )

    # ── /workspaces ───────────────────────────────────────────────────────────

    async def get_workspaces(self) -> Dict[str, Any]:
        """GET /workspaces?includeAll=true — all workspaces.

        Returns dict with key ``data`` (list of workspace objects).
        """
        return await self._get(
            "workspaces",
            params={"includeAll": "true"},
            context="get_workspaces",
        )

    # ── /reports ──────────────────────────────────────────────────────────────

    async def get_reports(
        self,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """GET /reports — paginated list of reports.

        Returns dict with keys: ``data`` (list), ``totalPages``, ``pageNumber``.
        """
        params: Dict[str, Any] = {
            "page": page,
            "pageSize": page_size,
        }
        return await self._get("reports", params=params, context="get_reports")

    # ── /folders ──────────────────────────────────────────────────────────────

    async def get_folders(self, page: int = 1) -> Dict[str, Any]:
        """GET /home/folders — list of top-level folders.

        Returns dict with key ``data`` (list of folder objects).
        """
        params: Dict[str, Any] = {"page": page, "includeAll": "true"}
        return await self._get("home/folders", params=params, context="get_folders")
