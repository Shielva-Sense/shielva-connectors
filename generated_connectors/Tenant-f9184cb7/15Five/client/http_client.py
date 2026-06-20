from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    FifteenFiveAuthError,
    FifteenFiveError,
    FifteenFiveNetworkError,
    FifteenFiveNotFoundError,
    FifteenFiveRateLimitError,
)

BASE_URL: str = "https://my.15five.com"
DEFAULT_TIMEOUT_S: float = 30.0


class FifteenFiveHTTPClient:
    """Low-level async HTTP client for the 15Five REST API v1.

    Authentication: Bearer token in the Authorization header.
    All responses use DRF-style pagination: count, next, previous, results.
    All endpoint paths end with a trailing slash per 15Five's URL convention.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}{path}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=self._headers(api_key),
                    params=params,
                ) as response:
                    return await self._raise_for_status(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise FifteenFiveNetworkError(f"Network error: {exc}") from exc
        except (
            FifteenFiveError,
            FifteenFiveAuthError,
            FifteenFiveRateLimitError,
            FifteenFiveNotFoundError,
            FifteenFiveNetworkError,
        ):
            raise
        except Exception as exc:
            raise FifteenFiveNetworkError(f"Unexpected network error: {exc}") from exc

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_msg: str = (
            body.get("detail", "")
            or body.get("error", "")
            or body.get("message", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise FifteenFiveAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise FifteenFiveNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise FifteenFiveRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise FifteenFiveNetworkError(
                f"15Five server error {status}: {err_msg}",
                status_code=status,
            )
        raise FifteenFiveError(
            f"15Five error {status}: {err_msg}", status_code=status
        )

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(
        self,
        api_key: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """GET /api/public/v1/user/ — list all users (DRF pagination)."""
        return await self._request(
            "GET",
            "/api/public/v1/user/",
            api_key,
            params={"page": page, "page_size": page_size},
        )

    # ── Reports / Check-ins ───────────────────────────────────────────────────

    async def get_reports(
        self,
        api_key: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """GET /api/public/v1/report/ — list check-in reports (DRF pagination)."""
        return await self._request(
            "GET",
            "/api/public/v1/report/",
            api_key,
            params={"page": page, "page_size": page_size},
        )

    async def get_report(
        self,
        api_key: str,
        report_id: int | str,
    ) -> dict[str, Any]:
        """GET /api/public/v1/report/{report_id}/ — fetch a single report."""
        return await self._request(
            "GET",
            f"/api/public/v1/report/{report_id}/",
            api_key,
        )

    # ── Objectives (OKRs) ─────────────────────────────────────────────────────

    async def get_objectives(
        self,
        api_key: str,
        page: int = 1,
    ) -> dict[str, Any]:
        """GET /api/public/v1/objective/ — list OKRs (DRF pagination)."""
        return await self._request(
            "GET",
            "/api/public/v1/objective/",
            api_key,
            params={"page": page},
        )

    # ── 1-on-1 Meetings ───────────────────────────────────────────────────────

    async def get_meetings(
        self,
        api_key: str,
        page: int = 1,
    ) -> dict[str, Any]:
        """GET /api/public/v1/meeting/ — list 1-on-1 meetings (DRF pagination)."""
        return await self._request(
            "GET",
            "/api/public/v1/meeting/",
            api_key,
            params={"page": page},
        )

    # ── High Fives (Recognition) ──────────────────────────────────────────────

    async def get_high_fives(
        self,
        api_key: str,
        page: int = 1,
    ) -> dict[str, Any]:
        """GET /api/public/v1/highfive/ — list shoutouts/recognition (DRF pagination)."""
        return await self._request(
            "GET",
            "/api/public/v1/highfive/",
            api_key,
            params={"page": page},
        )

    # ── Groups (Teams) ────────────────────────────────────────────────────────

    async def get_groups(
        self,
        api_key: str,
        page: int = 1,
    ) -> dict[str, Any]:
        """GET /api/public/v1/group/ — list groups/teams (DRF pagination)."""
        return await self._request(
            "GET",
            "/api/public/v1/group/",
            api_key,
            params={"page": page},
        )
