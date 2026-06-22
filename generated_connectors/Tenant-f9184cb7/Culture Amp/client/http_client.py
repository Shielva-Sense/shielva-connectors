from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    CultureAmpAuthError,
    CultureAmpError,
    CultureAmpNetworkError,
    CultureAmpNotFoundError,
    CultureAmpRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
CULTURE_AMP_BASE_URL: str = "https://api.cultureamp.com"


class CultureAmpHTTPClient:
    """Low-level async HTTP client for the Culture Amp REST API.

    Authenticates with Bearer token via the Authorization header.
    All requests accept and expect JSON responses.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_headers(self, api_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        api_token: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{CULTURE_AMP_BASE_URL}{path}"
        headers = self._make_headers(api_token)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                ) as response:
                    return await self._raise_for_status(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise CultureAmpNetworkError(f"Network error: {exc}") from exc
        except (
            CultureAmpError,
            CultureAmpAuthError,
            CultureAmpRateLimitError,
            CultureAmpNotFoundError,
            CultureAmpNetworkError,
        ):
            raise
        except Exception as exc:
            raise CultureAmpNetworkError(f"Unexpected network error: {exc}") from exc

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        # Attempt to read error body
        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_msg: str = (
            body.get("error", "")
            or body.get("message", "")
            or body.get("description", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise CultureAmpAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise CultureAmpNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise CultureAmpRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise CultureAmpNetworkError(
                f"Culture Amp server error {status}: {err_msg}",
                status_code=status,
            )
        raise CultureAmpError(
            f"Culture Amp error {status}: {err_msg}", status_code=status
        )

    # ── Me / Health ───────────────────────────────────────────────────────────

    async def get_me(self, api_token: str) -> dict[str, Any]:
        """GET /v1/me — return current authenticated user info."""
        return await self._request("GET", "/v1/me", api_token)

    # ── Surveys ───────────────────────────────────────────────────────────────

    async def get_surveys(
        self, api_token: str, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v1/surveys — list engagement surveys."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", "/v1/surveys", api_token, params=params)

    async def get_survey(self, api_token: str, survey_id: str | int) -> dict[str, Any]:
        """GET /v1/surveys/{survey_id} — get a single survey."""
        return await self._request("GET", f"/v1/surveys/{survey_id}", api_token)

    # ── Employees ─────────────────────────────────────────────────────────────

    async def get_employees(
        self, api_token: str, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v1/employees — list employees."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", "/v1/employees", api_token, params=params)

    # ── Goals ─────────────────────────────────────────────────────────────────

    async def get_goals(
        self, api_token: str, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v1/goals — list performance goals."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", "/v1/goals", api_token, params=params)

    # ── Performance Reviews ───────────────────────────────────────────────────

    async def get_reviews(
        self, api_token: str, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v1/performance/reviews — list performance reviews."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request(
            "GET", "/v1/performance/reviews", api_token, params=params
        )

    # ── Groups ────────────────────────────────────────────────────────────────

    async def get_groups(self, api_token: str, page: int = 1) -> dict[str, Any]:
        """GET /v1/groups — list departments/teams."""
        params: dict[str, Any] = {"page": page}
        return await self._request("GET", "/v1/groups", api_token, params=params)
