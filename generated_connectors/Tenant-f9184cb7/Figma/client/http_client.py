"""Figma connector — Figma REST API HTTP client."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp

from exceptions import (
    FigmaAuthError,
    FigmaError,
    FigmaNetworkError,
    FigmaNotFoundError,
    FigmaRateLimitError,
)

_FIGMA_BASE = "https://api.figma.com/v1"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_PAGE_SIZE = 100


class FigmaHTTPClient:
    """Thin async HTTP wrapper for the Figma REST API.

    Authentication is via Personal Access Token in the X-Figma-Token header.
    All endpoints are under https://api.figma.com/v1/.
    """

    def __init__(
        self,
        base_url: str = _FIGMA_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._config: Dict[str, Any] = config or {}

    def _pat(self) -> str:
        """Return the Personal Access Token from config.

        Supports both ``api_key`` (canonical) and ``personal_access_token``
        (legacy alias) — checked in that priority order.
        """
        return (
            self._config.get("api_key")
            or self._config.get("personal_access_token")
            or ""
        )

    def _auth_headers(self) -> Dict[str, str]:
        """Build request headers using X-Figma-Token PAT authentication."""
        return {
            "X-Figma-Token": self._pat(),
            "Content-Type": "application/json",
        }

    def _raise_for_status(self, status: int, body: Dict[str, Any], context: str) -> None:
        """Map HTTP status codes to typed Figma exceptions."""
        if status == 200:
            return
        if status in (401, 403):
            message = body.get("message", body.get("err", "Unauthorized"))
            raise FigmaAuthError(f"[{context}] Auth error ({status}): {message}")
        if status == 404:
            message = body.get("message", body.get("err", "Not found"))
            raise FigmaNotFoundError(f"[{context}] Not found (404): {message}")
        if status == 429:
            raise FigmaRateLimitError(f"[{context}] Rate limited (429)")
        if status >= 500:
            message = body.get("message", body.get("err", f"HTTP {status}"))
            raise FigmaNetworkError(f"[{context}] Server error ({status}): {message}")
        message = body.get("message", body.get("err", f"HTTP {status}"))
        raise FigmaError(f"[{context}] Figma API error ({status}): {message}")

    # ── low-level request helper ──────────────────────────────────────────────

    async def _get(self, url: str, context: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Perform a GET request, parse JSON, and raise typed exceptions."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._auth_headers(), params=params or {}) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise FigmaNetworkError(f"[{context}] Network error: {exc}") from exc
        # Figma sometimes wraps a 403 inside a 200 with an "err" field
        if isinstance(body, dict) and body.get("status") == 403:
            raise FigmaAuthError(f"[{context}] Auth error (200/403): {body.get('err', 'forbidden')}")
        self._raise_for_status(status, body, context)
        return body

    # ── public API methods ────────────────────────────────────────────────────

    async def get_me(self) -> Dict[str, Any]:
        """GET /me — retrieve the authenticated user's information."""
        return await self._get(f"{self._base_url}/me", "get_me")

    # Canonical name per spec
    async def list_projects(self, team_id: str) -> Dict[str, Any]:
        """GET /teams/{team_id}/projects — list all projects for a team."""
        return await self._get(f"{self._base_url}/teams/{team_id}/projects", "list_projects")

    # Legacy alias kept for backward compatibility
    async def get_team_projects(self, team_id: str) -> Dict[str, Any]:
        """Alias for list_projects — kept for backward compatibility."""
        return await self.list_projects(team_id)

    # Canonical name per spec
    async def list_files(self, project_id: str) -> Dict[str, Any]:
        """GET /projects/{project_id}/files — list all files in a project."""
        return await self._get(f"{self._base_url}/projects/{project_id}/files", "list_files")

    # Legacy alias kept for backward compatibility
    async def get_project_files(self, project_id: str) -> Dict[str, Any]:
        """Alias for list_files — kept for backward compatibility."""
        return await self.list_files(project_id)

    async def get_file(self, file_key: str) -> Dict[str, Any]:
        """GET /files/{file_key} — retrieve a Figma file's full document tree."""
        return await self._get(f"{self._base_url}/files/{file_key}", "get_file")

    async def get_file_nodes(self, file_key: str, node_ids: List[str]) -> Dict[str, Any]:
        """GET /files/{file_key}/nodes?ids={comma_separated} — fetch specific nodes."""
        ids_param = ",".join(node_ids)
        return await self._get(
            f"{self._base_url}/files/{file_key}/nodes",
            "get_file_nodes",
            params={"ids": ids_param},
        )

    async def get_file_comments(self, file_key: str) -> Dict[str, Any]:
        """GET /files/{file_key}/comments — list all comments on a file."""
        return await self._get(f"{self._base_url}/files/{file_key}/comments", "get_file_comments")

    async def get_file_versions(self, file_key: str) -> Dict[str, Any]:
        """GET /files/{file_key}/versions — list version history of a file."""
        return await self._get(f"{self._base_url}/files/{file_key}/versions", "get_file_versions")

    async def get_team_components(
        self,
        team_id: str,
        page_size: int = _DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /teams/{team_id}/components — list published components for a team.

        Supports cursor-based pagination via the ``cursor`` query param.
        """
        params: Dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        return await self._get(
            f"{self._base_url}/teams/{team_id}/components",
            "get_team_components",
            params=params,
        )

    async def get_team_styles(
        self,
        team_id: str,
        page_size: int = _DEFAULT_PAGE_SIZE,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /teams/{team_id}/styles — list published styles for a team.

        Supports cursor-based pagination via the ``cursor`` query param.
        """
        params: Dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        return await self._get(
            f"{self._base_url}/teams/{team_id}/styles",
            "get_team_styles",
            params=params,
        )

    async def get_component(self, key: str) -> Dict[str, Any]:
        """GET /components/{key} — retrieve a single published component by key."""
        return await self._get(f"{self._base_url}/components/{key}", "get_component")
