from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    LatticeAuthError,
    LatticeError,
    LatticeNetworkError,
    LatticeNotFoundError,
    LatticeRateLimitError,
)

LATTICE_BASE_URL: str = "https://api.latticehq.com"
DEFAULT_TIMEOUT_S: float = 30.0


class LatticeHTTPClient:
    """Low-level async HTTP client for the Lattice REST API.

    Authenticates every request with:
        Authorization: Bearer {api_token}

    All responses are JSON-parsed and mapped to typed LatticeError
    subclasses on non-2xx status codes.
    """

    def __init__(
        self,
        api_token: str = "",
        base_url: str = LATTICE_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_token = api_token
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                ) as response:
                    return await self._raise_for_status(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise LatticeNetworkError(f"Network error: {exc}") from exc
        except (
            LatticeError,
            LatticeAuthError,
            LatticeRateLimitError,
            LatticeNotFoundError,
            LatticeNetworkError,
        ):
            raise
        except Exception as exc:
            raise LatticeNetworkError(f"Unexpected network error: {exc}") from exc

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
            body.get("error", "")
            or body.get("message", "")
            or body.get("detail", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise LatticeAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise LatticeNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise LatticeRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise LatticeNetworkError(
                f"Lattice server error {status}: {err_msg}",
                status_code=status,
            )
        raise LatticeError(
            f"Lattice error {status}: {err_msg}", status_code=status
        )

    # ── Users (Employees) ─────────────────────────────────────────────────────

    async def get_users(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v1/users — list all employees/users."""
        return await self._request(
            "GET",
            "/v1/users",
            params={"page": page, "per_page": per_page},
        )

    async def get_user(self, user_id: str | int) -> dict[str, Any]:
        """GET /v1/users/{user_id} — fetch a single user."""
        return await self._request("GET", f"/v1/users/{user_id}")

    # ── Departments ───────────────────────────────────────────────────────────

    async def get_departments(self, page: int = 1) -> dict[str, Any]:
        """GET /v1/departments — list all departments."""
        return await self._request(
            "GET",
            "/v1/departments",
            params={"page": page},
        )

    # ── Goals (OKRs) ──────────────────────────────────────────────────────────

    async def get_goals(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v1/goals — list all goals/OKRs."""
        return await self._request(
            "GET",
            "/v1/goals",
            params={"page": page, "per_page": per_page},
        )

    # ── Performance Reviews ───────────────────────────────────────────────────

    async def get_reviews(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v1/reviews — list all performance reviews."""
        return await self._request(
            "GET",
            "/v1/reviews",
            params={"page": page, "per_page": per_page},
        )

    # ── Feedback ──────────────────────────────────────────────────────────────

    async def get_feedback(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v1/feedback — list public praise / feedback."""
        return await self._request(
            "GET",
            "/v1/feedback",
            params={"page": page, "per_page": per_page},
        )

    # ── 1-on-1s ───────────────────────────────────────────────────────────────

    async def get_one_on_ones(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /v1/one-on-ones — list 1-on-1 meetings."""
        return await self._request(
            "GET",
            "/v1/one-on-ones",
            params={"page": page, "per_page": per_page},
        )
