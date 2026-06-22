from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    TableauAuthError,
    TableauError,
    TableauNetworkError,
    TableauNotFoundError,
    TableauRateLimitError,
)

TABLEAU_API_VERSION = "3.21"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_PAGE_SIZE = 100


class TableauHTTPClient:
    """Low-level async HTTP client for the Tableau REST API."""

    def __init__(
        self,
        server_url: str,
        pat_name: str = "",
        pat_secret: str = "",
        site_name: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._pat_name = pat_name
        self._pat_secret = pat_secret
        self._site_name = site_name
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._token: str = ""
        self._site_id: str = ""
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if self._token:
                headers["X-Tableau-Auth"] = self._token
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=self._timeout,
            )
        return self._session

    def _base_url(self) -> str:
        return f"{self._server_url}/api/{TABLEAU_API_VERSION}"

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._token:
            headers["X-Tableau-Auth"] = self._token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url()}{path}"
        try:
            async with aiohttp.ClientSession(
                timeout=self._timeout,
            ) as session:
                async with session.request(
                    method,
                    url,
                    headers=self._auth_headers(),
                    json=json,
                    params=params,
                ) as response:
                    return await self._raise_for_status(response)
        except (TableauError,):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise TableauNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            raise TableauNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise TableauNetworkError(f"Network error: {exc}") from exc

    async def _raise_for_status(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        status = response.status
        if status in (200, 201, 204):
            if status == 204:
                return {}
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        # Tableau REST API wraps errors in {"error": {"summary": "...", "detail": "..."}}
        err_obj = body.get("error", {})
        err_msg = err_obj.get("detail", err_obj.get("summary", str(body) or "Unknown Tableau error"))
        err_code = str(err_obj.get("code", ""))

        if status == 401:
            raise TableauAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if status == 403:
            raise TableauAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if status == 404:
            raise TableauNotFoundError(err_code or "resource", err_msg)
        if status == 429:
            retry_after_raw = response.headers.get("Retry-After", "0")
            try:
                retry_after = float(retry_after_raw)
            except ValueError:
                retry_after = 0.0
            raise TableauRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if status >= 500:
            raise TableauError(
                f"Tableau server error {status}: {err_msg}",
                status,
                err_code,
            )

        raise TableauError(
            f"Tableau error {status}: {err_msg}",
            status,
            err_code,
        )

    # ── Authentication ────────────────────────────────────────────────────────

    async def sign_in(self) -> dict[str, Any]:
        """POST /api/3.21/auth/signin with PAT credentials.

        Stores the auth token and site_id on the client for subsequent calls.
        """
        site_content_url = self._site_name if self._site_name else ""
        payload: dict[str, Any] = {
            "credentials": {
                "personalAccessTokenName": self._pat_name,
                "personalAccessTokenSecret": self._pat_secret,
                "site": {
                    "contentUrl": site_content_url,
                },
            }
        }
        result = await self._request(
            "POST",
            "/auth/signin",
            json=payload,
        )
        credentials = result.get("credentials", {})
        self._token = credentials.get("token", "")
        site = credentials.get("site", {})
        self._site_id = site.get("id", "")
        return result

    async def sign_out(self) -> dict[str, Any]:
        """POST /api/3.21/auth/signout — invalidates the current auth token."""
        result = await self._request("POST", "/auth/signout")
        self._token = ""
        self._site_id = ""
        return result

    # ── Sites ─────────────────────────────────────────────────────────────────

    async def get_sites(self) -> dict[str, Any]:
        """GET /api/3.21/sites — list all sites the user can access."""
        return await self._request("GET", "/sites")

    # ── Workbooks ─────────────────────────────────────────────────────────────

    async def get_workbooks(
        self,
        site_id: str,
        page_number: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """GET /api/3.21/sites/{site_id}/workbooks — list workbooks (paginated)."""
        return await self._request(
            "GET",
            f"/sites/{site_id}/workbooks",
            params={"pageNumber": page_number, "pageSize": page_size},
        )

    # ── Views ─────────────────────────────────────────────────────────────────

    async def get_views(
        self,
        site_id: str,
        page_number: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """GET /api/3.21/sites/{site_id}/views — list views (paginated)."""
        return await self._request(
            "GET",
            f"/sites/{site_id}/views",
            params={"pageNumber": page_number, "pageSize": page_size},
        )

    # ── Datasources ───────────────────────────────────────────────────────────

    async def get_datasources(
        self,
        site_id: str,
        page_number: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """GET /api/3.21/sites/{site_id}/datasources — list datasources (paginated)."""
        return await self._request(
            "GET",
            f"/sites/{site_id}/datasources",
            params={"pageNumber": page_number, "pageSize": page_size},
        )

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(
        self,
        site_id: str,
        page_number: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """GET /api/3.21/sites/{site_id}/users — list users (paginated)."""
        return await self._request(
            "GET",
            f"/sites/{site_id}/users",
            params={"pageNumber": page_number, "pageSize": page_size},
        )

    # ── Projects ──────────────────────────────────────────────────────────────

    async def get_projects(self, site_id: str) -> dict[str, Any]:
        """GET /api/3.21/sites/{site_id}/projects — list all projects."""
        return await self._request("GET", f"/sites/{site_id}/projects")

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def token(self) -> str:
        return self._token

    @property
    def site_id(self) -> str:
        return self._site_id

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> TableauHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
