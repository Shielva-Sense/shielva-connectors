from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    SmartRecruitersAuthError,
    SmartRecruitersError,
    SmartRecruitersNetworkError,
    SmartRecruitersNotFoundError,
    SmartRecruitersRateLimitError,
)

SR_BASE_URL = "https://api.smartrecruiters.com"
DEFAULT_TIMEOUT_S = 30.0


class SmartRecruitersHTTPClient:
    """Low-level async HTTP client for the SmartRecruiters REST API v1.

    Authenticates using the ``X-SmartToken`` header (api_token).
    Pagination uses ``limit`` + ``offset`` query params; responses carry
    ``totalFound`` and ``items`` at the top level.
    """

    def __init__(self, api_token: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._api_token = api_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._company_id: str = ""

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"X-SmartToken": self._api_token},
            )
        return self._session

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a request and return parsed JSON body.

        Raises the appropriate SmartRecruitersError subclass on non-2xx responses.
        """
        session = self._get_session()
        try:
            async with session.request(method, url, params=params) as response:
                status = response.status

                if status in (200, 201):
                    return await response.json()

                body: Any = {}
                try:
                    body = await response.json()
                except Exception:
                    pass

                # SmartRecruiters error format: {"message": "...", "errors": [...]}
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
                    raise SmartRecruitersAuthError(
                        f"Authentication failed ({status}): {err_msg}",
                        status_code=status,
                        code=str(status),
                    )
                if status == 404:
                    raise SmartRecruitersNotFoundError("resource", err_msg or str(status))
                if status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise SmartRecruitersRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )
                if status >= 500:
                    raise SmartRecruitersNetworkError(
                        f"SmartRecruiters API server error {status}: {err_msg}",
                        status_code=status,
                    )
                raise SmartRecruitersError(
                    f"SmartRecruiters API error {status}: {err_msg}",
                    status_code=status,
                    code=str(status),
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise SmartRecruitersNetworkError(f"Network error: {exc}") from exc
        except (
            SmartRecruitersAuthError,
            SmartRecruitersNetworkError,
            SmartRecruitersRateLimitError,
            SmartRecruitersNotFoundError,
            SmartRecruitersError,
        ):
            raise

    async def _raise_for_status(self, response: aiohttp.ClientResponse) -> Any:
        """Parse response and raise appropriate error for non-2xx status codes."""
        status = response.status
        if status in (200, 201):
            return await response.json()

        body: Any = {}
        try:
            body = await response.json()
        except Exception:
            pass

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
            raise SmartRecruitersAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code=str(status),
            )
        if status == 404:
            raise SmartRecruitersNotFoundError("resource", err_msg or str(status))
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise SmartRecruitersRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise SmartRecruitersNetworkError(
                f"SmartRecruiters API server error {status}: {err_msg}",
                status_code=status,
            )
        raise SmartRecruitersError(
            f"SmartRecruiters API error {status}: {err_msg}",
            status_code=status,
            code=str(status),
        )

    # ── Company ───────────────────────────────────────────────────────────────

    async def get_company(self) -> dict[str, Any]:
        """GET /v1/companies/me — returns company info and serves as health check."""
        url = f"{SR_BASE_URL}/v1/companies/me"
        result = await self._request("GET", url)
        data: dict[str, Any] = result if isinstance(result, dict) else {}
        # Cache company_id for subsequent job listing calls
        if data.get("id"):
            self._company_id = str(data["id"])
        return data

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def get_jobs(
        self,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
    ) -> dict[str, Any]:
        """GET /v1/companies/{company_id}/postings — paginated with limit/offset.

        Returns raw response dict with ``totalFound`` and ``items``.
        Requires company_id to be cached (call get_company() first).
        """
        if not self._company_id:
            company = await self.get_company()
            self._company_id = str(company.get("id", ""))

        url = f"{SR_BASE_URL}/v1/companies/{self._company_id}/postings"
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        result = await self._request("GET", url, params=params)
        return result if isinstance(result, dict) else {"totalFound": 0, "items": []}

    async def get_job(self, job_id: str) -> dict[str, Any]:
        """GET /v1/companies/{company_id}/postings/{job_id}."""
        if not self._company_id:
            company = await self.get_company()
            self._company_id = str(company.get("id", ""))

        url = f"{SR_BASE_URL}/v1/companies/{self._company_id}/postings/{job_id}"
        result = await self._request("GET", url)
        return result if isinstance(result, dict) else {}

    # ── Candidates ────────────────────────────────────────────────────────────

    async def get_candidates(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /v1/candidates — paginated with limit/offset.

        Returns raw response dict with ``totalFound`` and ``items``.
        """
        url = f"{SR_BASE_URL}/v1/candidates"
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        result = await self._request("GET", url, params=params)
        return result if isinstance(result, dict) else {"totalFound": 0, "items": []}

    async def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        """GET /v1/candidates/{candidate_id}."""
        url = f"{SR_BASE_URL}/v1/candidates/{candidate_id}"
        result = await self._request("GET", url)
        return result if isinstance(result, dict) else {}

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /v1/users — paginated with limit/offset.

        Returns raw response dict with ``totalFound`` and ``items``.
        """
        url = f"{SR_BASE_URL}/v1/users"
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        result = await self._request("GET", url, params=params)
        return result if isinstance(result, dict) else {"totalFound": 0, "items": []}

    # ── Departments ───────────────────────────────────────────────────────────

    async def get_departments(self) -> list[dict[str, Any]]:
        """GET /v1/configuration/departments — returns full list (no pagination)."""
        url = f"{SR_BASE_URL}/v1/configuration/departments"
        result = await self._request("GET", url)
        if isinstance(result, list):
            return result
        # Some SmartRecruiters endpoints wrap in {"content": [...]}
        if isinstance(result, dict):
            items = result.get("content", result.get("items", []))
            return items if isinstance(items, list) else []
        return []

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> SmartRecruitersHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
