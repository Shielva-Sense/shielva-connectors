"""Low-level async HTTP client for the Basecamp 4 REST API.

Authentication: OAuth 2.0 Bearer token sent in every request.
User-Agent: Required by 37signals — must identify the application and contact.
Pagination: Link header with rel="next" RFC 5988 pattern.
Account ID: Fetched from https://launchpad.37signals.com/authorization.json
            after OAuth; stored in config as "account_id".
"""
from __future__ import annotations

import re
from typing import Any

import aiohttp

from exceptions import (
    BasecampAuthError,
    BasecampError,
    BasecampNetworkError,
    BasecampNotFoundError,
    BasecampRateLimitError,
)

AUTH_URL: str = "https://launchpad.37signals.com/authorization.json"
API_BASE_TEMPLATE: str = "https://3.basecampapi.com/{account_id}"
USER_AGENT: str = "Shielva (contact@shielva.ai)"
DEFAULT_TIMEOUT_S: float = 30.0

# Regex to extract the next-page URL from Link header
_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _parse_next_link(link_header: str) -> str | None:
    """Extract the next-page URL from a RFC 5988 Link header, or None."""
    m = _LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None


class BasecampHTTPClient:
    """Async HTTP client for the Basecamp 4 REST API.

    All public methods return deserialized Python objects (list or dict).
    Pagination is handled automatically via Link headers — callers receive
    the full page list from list_* helpers (they collect all pages).
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._config: dict[str, Any] = config or {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _access_token(self) -> str:
        return str(self._config.get("access_token", "")).strip()

    def _account_id(self) -> str:
        return str(self._config.get("account_id", "")).strip()

    def _base_url(self) -> str:
        account_id = self._account_id()
        return API_BASE_TEMPLATE.format(account_id=account_id)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def _raise_for_status(self, status: int, body: Any) -> None:
        """Map HTTP error codes to typed Basecamp exceptions."""
        if status in (401, 403):
            msg = _extract_error_message(body) or f"HTTP {status}: Unauthorized"
            raise BasecampAuthError(msg, status_code=status)
        if status == 404:
            raise BasecampNotFoundError("resource", "unknown")
        if status == 429:
            retry_after: float = 0.0
            if isinstance(body, dict):
                retry_after = float(body.get("retry_after", 0))
            raise BasecampRateLimitError(
                "Rate limit exceeded", retry_after=retry_after
            )
        if status >= 500:
            msg = _extract_error_message(body) or f"Basecamp server error {status}"
            raise BasecampNetworkError(msg, status_code=status)
        msg = _extract_error_message(body) or f"HTTP {status}"
        raise BasecampError(msg, status_code=status)

    async def _get(self, url: str) -> Any:
        """Perform a single GET request to an absolute URL."""
        session = self._get_session()
        try:
            async with session.get(url, headers=self._headers()) as response:
                if response.status in (200, 201):
                    body = await response.json(content_type=None)
                    return body, response.headers.get("Link", "")
                if response.status == 204:
                    return {}, ""
                body_raw: Any = {}
                try:
                    body_raw = await response.json(content_type=None)
                except Exception:
                    pass
                self._raise_for_status(response.status, body_raw)
        except BasecampError:
            raise
        except aiohttp.ClientConnectorError as exc:
            raise BasecampNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise BasecampNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise BasecampNetworkError(f"Network error: {exc}") from exc
        return {}, ""  # unreachable but satisfies type checker

    async def _get_all_pages(self, first_url: str) -> list[Any]:
        """Follow Link rel="next" pagination and collect all items."""
        results: list[Any] = []
        url: str | None = first_url
        while url:
            data, link_header = await self._get(url)
            if isinstance(data, list):
                results.extend(data)
            elif isinstance(data, dict):
                # Basecamp sometimes returns a single dict — wrap it
                results.append(data)
            url = _parse_next_link(link_header) if link_header else None
        return results

    # ── Auth ─────────────────────────────────────────────────────────────────

    async def get_authorization(self) -> dict[str, Any]:
        """GET https://launchpad.37signals.com/authorization.json

        Returns identity + list of Basecamp accounts accessible with the
        current token. This is the canonical way to discover account_id.
        """
        data, _ = await self._get(AUTH_URL)
        if isinstance(data, dict):
            return data
        return {}

    # ── Projects ─────────────────────────────────────────────────────────────

    async def get_projects(self) -> list[dict[str, Any]]:
        """GET /projects.json — all active projects (auto-paginated)."""
        url = f"{self._base_url()}/projects.json"
        items = await self._get_all_pages(url)
        return [i for i in items if isinstance(i, dict)]

    async def get_project(self, project_id: int | str) -> dict[str, Any]:
        """GET /projects/{project_id}.json — single project details."""
        url = f"{self._base_url()}/projects/{project_id}.json"
        data, _ = await self._get(url)
        if isinstance(data, dict):
            return data
        return {}

    # ── To-do lists ──────────────────────────────────────────────────────────

    async def get_todo_lists(self, project_id: int | str) -> list[dict[str, Any]]:
        """GET /buckets/{project_id}/todolists.json — all to-do lists in a project."""
        url = f"{self._base_url()}/buckets/{project_id}/todolists.json"
        items = await self._get_all_pages(url)
        return [i for i in items if isinstance(i, dict)]

    # ── To-dos ───────────────────────────────────────────────────────────────

    async def get_todos(
        self, project_id: int | str, todolist_id: int | str
    ) -> list[dict[str, Any]]:
        """GET /buckets/{project_id}/todolists/{todolist_id}/todos.json

        Returns all to-do items in the given list (auto-paginated).
        """
        url = (
            f"{self._base_url()}/buckets/{project_id}"
            f"/todolists/{todolist_id}/todos.json"
        )
        items = await self._get_all_pages(url)
        return [i for i in items if isinstance(i, dict)]

    # ── Messages ─────────────────────────────────────────────────────────────

    async def get_messages(self, project_id: int | str) -> list[dict[str, Any]]:
        """GET /buckets/{project_id}/messages.json — all messages in a project."""
        url = f"{self._base_url()}/buckets/{project_id}/messages.json"
        items = await self._get_all_pages(url)
        return [i for i in items if isinstance(i, dict)]

    # ── Documents ────────────────────────────────────────────────────────────

    async def get_documents(self, project_id: int | str) -> list[dict[str, Any]]:
        """GET /buckets/{project_id}/vaults/{vault_id}/documents.json

        Basecamp documents live inside a Vault. The vault_id is discovered
        by scanning the project's dock for the "vault" app. If no vault is
        found, an empty list is returned.
        """
        project = await self.get_project(project_id)
        dock = project.get("dock", [])
        vault_id: int | None = None
        for tool in dock:
            if isinstance(tool, dict) and tool.get("name") == "vault":
                vault_id = tool.get("id")
                break
        if vault_id is None:
            return []
        url = (
            f"{self._base_url()}/buckets/{project_id}"
            f"/vaults/{vault_id}/documents.json"
        )
        items = await self._get_all_pages(url)
        return [i for i in items if isinstance(i, dict)]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "BasecampHTTPClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


def _extract_error_message(body: Any) -> str:
    """Pull a human-readable error string from a Basecamp error response."""
    if isinstance(body, dict):
        return str(body.get("error", body.get("message", body.get("description", ""))))
    if isinstance(body, str):
        return body
    return ""
