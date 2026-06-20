from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    WorkableAuthError,
    WorkableError,
    WorkableNetworkError,
    WorkableNotFoundError,
    WorkableRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0
SPI_V3 = "/spi/v3"


class WorkableHTTPClient:
    """Low-level async HTTP client for the Workable REST API v3.

    Authentication: Bearer token via ``Authorization: Bearer {api_token}``.
    Base URL: ``https://{subdomain}.workable.com``.

    Pagination uses Workable's ``since_id`` cursor: the response body includes
    a ``paging`` object with a ``next`` URL when more pages exist.  The public
    list methods return ``(items, next_url_or_None)`` tuples so callers can
    iterate until exhausted.
    """

    def __init__(
        self,
        api_token: str,
        subdomain: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_token = api_token
        self._subdomain = subdomain
        self._base_url = f"https://{subdomain}.workable.com"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._api_token}"},
            )
        return self._session

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse
    ) -> Any:
        """Parse JSON body and raise the appropriate WorkableError on non-200."""
        status = response.status

        if status == 200:
            return await response.json()

        body: Any = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_msg = ""
        if isinstance(body, dict):
            err_msg = (
                body.get("error", "")
                or body.get("message", "")
                or str(body)
            )
        else:
            err_msg = str(body)

        if status in (401, 403):
            raise WorkableAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code=str(status),
            )
        if status == 404:
            raise WorkableNotFoundError("resource", err_msg or str(status))
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise WorkableRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise WorkableNetworkError(
                f"Workable API server error {status}: {err_msg}",
                status_code=status,
            )
        raise WorkableError(
            f"Workable API error {status}: {err_msg}",
            status_code=status,
            code=str(status),
        )

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform GET {base_url}{path} and return parsed JSON."""
        url = f"{self._base_url}{path}"
        session = self._get_session()
        try:
            async with session.get(url, params=params) as response:
                return await self._raise_for_status(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise WorkableNetworkError(f"Network error: {exc}") from exc
        except (
            WorkableAuthError,
            WorkableNetworkError,
            WorkableRateLimitError,
            WorkableNotFoundError,
            WorkableError,
        ):
            raise

    # ── Account ──────────────────────────────────────────────────────────────

    async def get_account(self) -> dict[str, Any]:
        """GET /spi/v3/accounts/{subdomain} — account info."""
        path = f"{SPI_V3}/accounts/{self._subdomain}"
        result = await self._get(path)
        return result if isinstance(result, dict) else {}

    # ── Jobs ─────────────────────────────────────────────────────────────────

    async def get_jobs(
        self,
        limit: int = 100,
        since_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /spi/v3/jobs — returns (jobs_list, next_url_or_None).

        Workable uses ``since_id`` cursor pagination.  The ``paging.next``
        field in the response body holds the URL of the next page when present.
        """
        path = f"{SPI_V3}/jobs"
        params: dict[str, Any] = {"limit": limit}
        if since_id is not None:
            params["since_id"] = since_id
        body = await self._get(path, params=params)
        jobs: list[dict[str, Any]] = []
        next_url: str | None = None
        if isinstance(body, dict):
            jobs = body.get("jobs", []) or []
            paging = body.get("paging") or {}
            next_url = paging.get("next") or None
        return jobs, next_url

    async def get_job(self, shortcode: str) -> dict[str, Any]:
        """GET /spi/v3/jobs/{shortcode} — single job detail."""
        path = f"{SPI_V3}/jobs/{shortcode}"
        result = await self._get(path)
        # Workable wraps single-resource responses in a top-level "job" key
        if isinstance(result, dict):
            return result.get("job", result)
        return {}

    # ── Candidates ───────────────────────────────────────────────────────────

    async def get_candidates(
        self,
        limit: int = 100,
        since_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /spi/v3/candidates — returns (candidates_list, next_url_or_None)."""
        path = f"{SPI_V3}/candidates"
        params: dict[str, Any] = {"limit": limit}
        if since_id is not None:
            params["since_id"] = since_id
        body = await self._get(path, params=params)
        candidates: list[dict[str, Any]] = []
        next_url: str | None = None
        if isinstance(body, dict):
            candidates = body.get("candidates", []) or []
            paging = body.get("paging") or {}
            next_url = paging.get("next") or None
        return candidates, next_url

    async def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        """GET /spi/v3/candidates/{candidate_id} — single candidate detail."""
        path = f"{SPI_V3}/candidates/{candidate_id}"
        result = await self._get(path)
        if isinstance(result, dict):
            return result.get("candidate", result)
        return {}

    # ── Stages ───────────────────────────────────────────────────────────────

    async def get_stages(self) -> list[dict[str, Any]]:
        """GET /spi/v3/stages — pipeline stages (no pagination)."""
        path = f"{SPI_V3}/stages"
        body = await self._get(path)
        if isinstance(body, dict):
            return body.get("stages", []) or []
        return []

    # ── Members ──────────────────────────────────────────────────────────────

    async def get_members(self) -> list[dict[str, Any]]:
        """GET /spi/v3/members — team members (no pagination)."""
        path = f"{SPI_V3}/members"
        body = await self._get(path)
        if isinstance(body, dict):
            return body.get("members", []) or []
        return []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> WorkableHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
