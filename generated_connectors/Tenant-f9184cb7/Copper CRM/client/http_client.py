"""Copper CRM HTTP client.

All list / search operations use POST because Copper's API requires a JSON body
for pagination parameters even when conceptually "listing" resources.
"""

from __future__ import annotations

import json
from typing import Any

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False

from ..exceptions import (
    CopperAuthError,
    CopperError,
    CopperNetworkError,
    CopperNotFoundError,
    CopperRateLimitError,
)

_BASE_URL = "https://api.copper.com/developer_api/v1/"


class CopperHTTPClient:
    """Async HTTP client for the Copper Developer API v1.

    Copper quirks handled here:
    - Three mandatory headers on every request (AccessToken, Application, UserEmail).
    - Content-Type: application/json even for GET requests (Copper docs require it).
    - All list / search endpoints are POST with a JSON body containing pagination.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._api_key: str = self._config.get("api_key", "")
        self._user_email: str = self._config.get("user_email", "")
        self._base_url: str = self._config.get("base_url", _BASE_URL).rstrip("/") + "/"
        self._session: "aiohttp.ClientSession | None" = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def _get_headers(self) -> dict[str, str]:
        return {
            "X-PW-AccessToken": self._api_key,
            "X-PW-Application": "developer_api",
            "X-PW-UserEmail": self._user_email,
            "Content-Type": "application/json",
        }

    async def _get_session(self) -> "aiohttp.ClientSession":
        if not _AIOHTTP_AVAILABLE:
            raise CopperNetworkError("aiohttp is not installed")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._get_headers())
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Status → exception mapping
    # ------------------------------------------------------------------

    def _raise_for_status(self, status: int, body: str | dict[str, Any] | None = None) -> None:
        """Raise the appropriate CopperError based on HTTP status code."""
        if status < 400:
            return
        if isinstance(body, dict):
            message = body.get("message") or body.get("error") or str(body)
        else:
            message = str(body) if body else ""

        if status == 401 or status == 403:
            raise CopperAuthError(message or "Authentication failed")
        if status == 404:
            raise CopperNotFoundError(message or "Resource not found")
        if status == 429:
            raise CopperRateLimitError(message or "Rate limit exceeded")
        raise CopperError(message or f"HTTP {status}", status_code=status)

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str) -> dict[str, Any]:
        """Perform an authenticated GET request."""
        session = await self._get_session()
        url = self._base_url + path.lstrip("/")
        try:
            async with session.get(url) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = await resp.text()
                self._raise_for_status(status, body)
                return body if isinstance(body, dict) else {"result": body}
        except (CopperError,):
            raise
        except Exception as exc:
            raise CopperNetworkError(str(exc)) from exc

    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        """Perform an authenticated POST request."""
        session = await self._get_session()
        url = self._base_url + path.lstrip("/")
        try:
            async with session.post(url, data=json.dumps(payload or {})) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = await resp.text()
                self._raise_for_status(status, body)
                return body  # type: ignore[return-value]
        except (CopperError,):
            raise
        except Exception as exc:
            raise CopperNetworkError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def get_account(self) -> dict[str, Any]:
        """GET /account — used for health checks and credential validation."""
        return await self._get("account")

    async def search_people(
        self,
        page_number: int = 1,
        page_size: int = 200,
    ) -> list[dict[str, Any]]:
        """POST /people/search — list/search people with pagination."""
        result = await self._post(
            "people/search",
            {"page_number": page_number, "page_size": page_size},
        )
        return result if isinstance(result, list) else []

    async def search_companies(
        self,
        page_number: int = 1,
        page_size: int = 200,
    ) -> list[dict[str, Any]]:
        """POST /companies/search — list/search companies with pagination."""
        result = await self._post(
            "companies/search",
            {"page_number": page_number, "page_size": page_size},
        )
        return result if isinstance(result, list) else []

    async def search_opportunities(
        self,
        page_number: int = 1,
        page_size: int = 200,
    ) -> list[dict[str, Any]]:
        """POST /opportunities/search — list/search opportunities with pagination."""
        result = await self._post(
            "opportunities/search",
            {"page_number": page_number, "page_size": page_size},
        )
        return result if isinstance(result, list) else []

    async def search_tasks(
        self,
        page_number: int = 1,
        page_size: int = 200,
    ) -> list[dict[str, Any]]:
        """POST /tasks/search — list/search tasks with pagination."""
        result = await self._post(
            "tasks/search",
            {"page_number": page_number, "page_size": page_size},
        )
        return result if isinstance(result, list) else []

    async def get_person(self, person_id: int) -> dict[str, Any]:
        """GET /people/{id} — fetch a single person by ID."""
        return await self._get(f"people/{person_id}")

    async def get_company(self, company_id: int) -> dict[str, Any]:
        """GET /companies/{id} — fetch a single company by ID."""
        return await self._get(f"companies/{company_id}")

    async def get_opportunity(self, opportunity_id: int) -> dict[str, Any]:
        """GET /opportunities/{id} — fetch a single opportunity by ID."""
        return await self._get(f"opportunities/{opportunity_id}")

    async def get_task(self, task_id: int) -> dict[str, Any]:
        """GET /tasks/{id} — fetch a single task by ID."""
        return await self._get(f"tasks/{task_id}")

    async def list_activity_types(self) -> list[dict[str, Any]]:
        """GET /activity_types — fetch all activity types."""
        result = await self._get("activity_types")
        # Copper returns {"user": [...], "system": [...]} shape
        if isinstance(result, dict):
            user_types = result.get("user", [])
            system_types = result.get("system", [])
            return (user_types or []) + (system_types or [])
        return []
