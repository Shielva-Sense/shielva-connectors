from __future__ import annotations

import re
from typing import Any

import aiohttp

from exceptions import (
    GreenhouseAuthError,
    GreenhouseError,
    GreenhouseNetworkError,
    GreenhouseNotFoundError,
    GreenhouseRateLimitError,
)

HARVEST_BASE_URL = "https://harvest.greenhouse.io/v1"
DEFAULT_TIMEOUT_S = 30.0


class GreenhouseHTTPClient:
    """Low-level async HTTP client for the Greenhouse Harvest API v1.

    Uses HTTP Basic Auth: api_key as the username, empty string as password.
    All methods return parsed JSON dicts/lists. Pagination via Link header
    ``rel="next"`` is handled by the paginated_list() helper.
    """

    def __init__(self, api_key: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                auth=aiohttp.BasicAuth(self._api_key, ""),
            )
        return self._session

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a request and return parsed JSON body.

        On non-200 responses, raises the appropriate GreenhouseError subclass.
        Returns a tuple (body, headers) only when called internally to expose
        Link headers; public methods call the plain variant.
        """
        session = self._get_session()
        try:
            async with session.request(method, url, params=params) as response:
                status = response.status

                if status == 200:
                    return await response.json()

                body: Any = {}
                try:
                    body = await response.json()
                except Exception:
                    pass

                # Greenhouse error body format: {"message": "..."} or {"errors": [...]}
                err_msg = ""
                if isinstance(body, dict):
                    err_msg = (
                        body.get("message", "")
                        or "; ".join(
                            e.get("message", "") if isinstance(e, dict) else str(e)
                            for e in body.get("errors", [])
                        )
                        or str(body)
                    )
                else:
                    err_msg = str(body)

                if status in (401, 403):
                    raise GreenhouseAuthError(
                        f"Authentication failed ({status}): {err_msg}",
                        status_code=status,
                        code=str(status),
                    )
                if status == 404:
                    raise GreenhouseNotFoundError("resource", err_msg or str(status))
                if status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise GreenhouseRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )
                if status >= 500:
                    raise GreenhouseNetworkError(
                        f"Greenhouse API server error {status}: {err_msg}",
                        status_code=status,
                    )
                raise GreenhouseError(
                    f"Greenhouse API error {status}: {err_msg}",
                    status_code=status,
                    code=str(status),
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise GreenhouseNetworkError(f"Network error: {exc}") from exc
        except (
            GreenhouseAuthError,
            GreenhouseNetworkError,
            GreenhouseRateLimitError,
            GreenhouseNotFoundError,
            GreenhouseError,
        ):
            raise

    async def _request_with_headers(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """Like _request but returns (body, response_headers) for Link pagination."""
        session = self._get_session()
        try:
            async with session.request(method, url, params=params) as response:
                status = response.status
                resp_headers = dict(response.headers)

                if status == 200:
                    body = await response.json()
                    return body, resp_headers

                body_raw: Any = {}
                try:
                    body_raw = await response.json()
                except Exception:
                    pass

                err_msg = ""
                if isinstance(body_raw, dict):
                    err_msg = (
                        body_raw.get("message", "")
                        or "; ".join(
                            e.get("message", "") if isinstance(e, dict) else str(e)
                            for e in body_raw.get("errors", [])
                        )
                        or str(body_raw)
                    )
                else:
                    err_msg = str(body_raw)

                if status in (401, 403):
                    raise GreenhouseAuthError(
                        f"Authentication failed ({status}): {err_msg}",
                        status_code=status,
                        code=str(status),
                    )
                if status == 404:
                    raise GreenhouseNotFoundError("resource", err_msg or str(status))
                if status == 429:
                    retry_after = float(resp_headers.get("Retry-After", "0"))
                    raise GreenhouseRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )
                if status >= 500:
                    raise GreenhouseNetworkError(
                        f"Greenhouse API server error {status}: {err_msg}",
                        status_code=status,
                    )
                raise GreenhouseError(
                    f"Greenhouse API error {status}: {err_msg}",
                    status_code=status,
                    code=str(status),
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise GreenhouseNetworkError(f"Network error: {exc}") from exc
        except (
            GreenhouseAuthError,
            GreenhouseNetworkError,
            GreenhouseRateLimitError,
            GreenhouseNotFoundError,
            GreenhouseError,
        ):
            raise

    @staticmethod
    def _parse_next_link(link_header: str) -> str | None:
        """Extract the URL for rel="next" from a Link header value.

        Example: <https://harvest.greenhouse.io/v1/jobs?page=2>; rel="next"
        """
        if not link_header:
            return None
        for part in link_header.split(","):
            part = part.strip()
            match = re.match(r'<([^>]+)>;\s*rel="next"', part)
            if match:
                return match.group(1)
        return None

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def list_jobs(
        self, per_page: int = 100, page: int = 1
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /jobs — returns (jobs_list, next_url_or_None)."""
        url = f"{HARVEST_BASE_URL}/jobs"
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        body, headers = await self._request_with_headers("GET", url, params=params)
        next_url = self._parse_next_link(headers.get("Link", ""))
        return body if isinstance(body, list) else [], next_url

    async def get_job(self, job_id: int | str) -> dict[str, Any]:
        """GET /jobs/{id}."""
        url = f"{HARVEST_BASE_URL}/jobs/{job_id}"
        result = await self._request("GET", url)
        return result if isinstance(result, dict) else {}

    # ── Candidates ───────────────────────────────────────────────────────────

    async def list_candidates(
        self, per_page: int = 100, page: int = 1
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /candidates — returns (candidates_list, next_url_or_None)."""
        url = f"{HARVEST_BASE_URL}/candidates"
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        body, headers = await self._request_with_headers("GET", url, params=params)
        next_url = self._parse_next_link(headers.get("Link", ""))
        return body if isinstance(body, list) else [], next_url

    async def get_candidate(self, candidate_id: int | str) -> dict[str, Any]:
        """GET /candidates/{id}."""
        url = f"{HARVEST_BASE_URL}/candidates/{candidate_id}"
        result = await self._request("GET", url)
        return result if isinstance(result, dict) else {}

    # ── Applications ─────────────────────────────────────────────────────────

    async def list_applications(
        self, per_page: int = 100, page: int = 1
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /applications — returns (applications_list, next_url_or_None)."""
        url = f"{HARVEST_BASE_URL}/applications"
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        body, headers = await self._request_with_headers("GET", url, params=params)
        next_url = self._parse_next_link(headers.get("Link", ""))
        return body if isinstance(body, list) else [], next_url

    async def get_application(self, application_id: int | str) -> dict[str, Any]:
        """GET /applications/{id}."""
        url = f"{HARVEST_BASE_URL}/applications/{application_id}"
        result = await self._request("GET", url)
        return result if isinstance(result, dict) else {}

    # ── Departments ──────────────────────────────────────────────────────────

    async def list_departments(self) -> list[dict[str, Any]]:
        """GET /departments — returns full list (no pagination for departments)."""
        url = f"{HARVEST_BASE_URL}/departments"
        result = await self._request("GET", url)
        return result if isinstance(result, list) else []

    # ── Offices ──────────────────────────────────────────────────────────────

    async def list_offices(self) -> list[dict[str, Any]]:
        """GET /offices — returns full list (no pagination for offices)."""
        url = f"{HARVEST_BASE_URL}/offices"
        result = await self._request("GET", url)
        return result if isinstance(result, list) else []

    # ── Users ────────────────────────────────────────────────────────────────

    async def list_users(
        self, per_page: int = 500, page: int = 1
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /users — returns (users_list, next_url_or_None)."""
        url = f"{HARVEST_BASE_URL}/users"
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        body, headers = await self._request_with_headers("GET", url, params=params)
        next_url = self._parse_next_link(headers.get("Link", ""))
        return body if isinstance(body, list) else [], next_url

    # ── Current User (Health check / Auth validation) ────────────────────────

    async def get_current_user(self) -> dict[str, Any]:
        """GET /users/current_user — returns the authenticated user's profile.

        Used for health check and credential validation.
        """
        url = f"{HARVEST_BASE_URL}/users/current_user"
        result = await self._request("GET", url)
        return result if isinstance(result, dict) else {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> GreenhouseHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
