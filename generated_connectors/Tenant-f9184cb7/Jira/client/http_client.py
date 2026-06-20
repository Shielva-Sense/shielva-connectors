from __future__ import annotations

import base64
from typing import Any

import aiohttp

from exceptions import (
    JiraAuthError,
    JiraError,
    JiraNetworkError,
    JiraNotFoundError,
    JiraRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_FIELDS: str = (
    "summary,status,assignee,reporter,priority,created,updated,issuetype"
)


class JiraHTTPClient:
    """Low-level async HTTP client for the Jira REST API v3 and Agile API v1.

    Authentication: HTTP Basic Auth with ``email:api_token`` base64-encoded in
    the ``Authorization`` header. The domain must be the bare Atlassian
    subdomain (e.g. ``mycompany`` for ``mycompany.atlassian.net``).
    """

    def __init__(
        self,
        domain: str,
        email: str,
        api_token: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._domain = domain
        self._email = email
        self._api_token = api_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._base_url = f"https://{domain}/rest/api/3"
        self._agile_base_url = f"https://{domain}/rest/agile/1.0"
        self._session: aiohttp.ClientSession | None = None

    # ── Session & headers ─────────────────────────────────────────────────────

    def _build_auth_header(self) -> str:
        """Return the Basic Auth header value for email:api_token."""
        raw = f"{self._email}:{self._api_token}"
        encoded = base64.b64encode(raw.encode()).decode()
        return f"Basic {encoded}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._build_auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers=self._headers(),
            )
        return self._session

    # ── Core request helpers ──────────────────────────────────────────────────

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        session = self._get_session()
        try:
            async with session.get(url, params=params) as response:
                return await self._raise_for_status(response)
        except aiohttp.ServerTimeoutError as exc:
            raise JiraNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise JiraNetworkError(f"Connection error: {exc}") from exc
        except JiraError:
            raise
        except Exception as exc:
            raise JiraNetworkError(f"Network error: {exc}") from exc

    async def _post(self, url: str, json: Any = None) -> Any:
        session = self._get_session()
        try:
            async with session.post(url, json=json) as response:
                return await self._raise_for_status(response)
        except aiohttp.ServerTimeoutError as exc:
            raise JiraNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise JiraNetworkError(f"Connection error: {exc}") from exc
        except JiraError:
            raise
        except Exception as exc:
            raise JiraNetworkError(f"Network error: {exc}") from exc

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse
    ) -> Any:
        if response.status in (200, 201):
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}
        if response.status == 204:
            return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_messages: list[str] = body.get("errorMessages") or []
        err_msg: str = (
            err_messages[0]
            if err_messages
            else body.get("message", "") or f"HTTP {response.status}"
        )

        if response.status in (401, 403):
            raise JiraAuthError(
                f"Authentication failed: {err_msg}",
                response.status,
                "unauthorized" if response.status == 401 else "forbidden",
            )
        if response.status == 404:
            raise JiraNotFoundError("resource", str(response.url))
        if response.status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise JiraRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status >= 500:
            raise JiraNetworkError(
                f"Jira server error {response.status}: {err_msg}",
                status_code=response.status,
            )
        raise JiraError(
            f"Jira error {response.status}: {err_msg}",
            response.status,
        )

    # ── Auth probe ────────────────────────────────────────────────────────────

    async def get_myself(self) -> dict[str, Any]:
        """GET /rest/api/3/myself — verify credentials and return current user."""
        return await self._get(f"{self._base_url}/myself")

    # ── Projects ──────────────────────────────────────────────────────────────

    async def list_projects(
        self,
        max_results: int = 50,
        start_at: int = 0,
        expand: str = "lead,description",
    ) -> dict[str, Any]:
        """GET /rest/api/3/project/search — paginated project list."""
        return await self._get(
            f"{self._base_url}/project/search",
            params={
                "maxResults": max_results,
                "startAt": start_at,
                "expand": expand,
            },
        )

    async def get_project(self, project_key: str) -> dict[str, Any]:
        """GET /rest/api/3/project/{project_key} — single project by key."""
        return await self._get(f"{self._base_url}/project/{project_key}")

    # ── Issues ────────────────────────────────────────────────────────────────

    async def search_issues(
        self,
        jql: str = "",
        max_results: int = 100,
        start_at: int = 0,
        fields: str = DEFAULT_FIELDS,
    ) -> dict[str, Any]:
        """POST /rest/api/3/search — JQL-based issue search with pagination."""
        body: dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "startAt": start_at,
            "fields": fields.split(","),
        }
        return await self._post(f"{self._base_url}/search", json=body)

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        """GET /rest/api/3/issue/{issue_key} — fetch a single issue by key."""
        return await self._get(f"{self._base_url}/issue/{issue_key}")

    # ── Boards (Agile API) ────────────────────────────────────────────────────

    async def list_boards(
        self,
        project_key_or_id: str | None = None,
        max_results: int = 50,
        start_at: int = 0,
    ) -> dict[str, Any]:
        """GET /rest/agile/1.0/board — list all Agile boards."""
        params: dict[str, Any] = {
            "maxResults": max_results,
            "startAt": start_at,
        }
        if project_key_or_id:
            params["projectKeyOrId"] = project_key_or_id
        return await self._get(f"{self._agile_base_url}/board", params=params)

    # ── Sprints ───────────────────────────────────────────────────────────────

    async def list_sprints(
        self,
        board_id: int | str,
        max_results: int = 50,
        start_at: int = 0,
    ) -> dict[str, Any]:
        """GET /rest/agile/1.0/board/{board_id}/sprint — list sprints on a board."""
        return await self._get(
            f"{self._agile_base_url}/board/{board_id}/sprint",
            params={"maxResults": max_results, "startAt": start_at},
        )

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(
        self,
        max_results: int = 50,
        start_at: int = 0,
    ) -> list[dict[str, Any]]:
        """GET /rest/api/3/users/search — list all users."""
        result = await self._get(
            f"{self._base_url}/users/search",
            params={"maxResults": max_results, "startAt": start_at},
        )
        if isinstance(result, list):
            return result
        return []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> JiraHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
