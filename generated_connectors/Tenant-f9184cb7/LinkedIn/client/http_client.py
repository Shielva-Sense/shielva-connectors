from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    LinkedInAuthError,
    LinkedInError,
    LinkedInNetworkError,
    LinkedInNotFoundError,
    LinkedInRateLimitError,
    LinkedInServerError,
)

LINKEDIN_BASE_URL = "https://api.linkedin.com/v2"
DEFAULT_TIMEOUT_S = 30.0
LINKEDIN_API_VERSION = "202401"


class LinkedInHTTPClient:
    """Low-level async HTTP client for the LinkedIn REST API v2."""

    def __init__(
        self,
        access_token: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._client = httpx.AsyncClient(
            base_url=LINKEDIN_BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-Restli-Protocol-Version": "2.0.0",
                "LinkedIn-Version": LINKEDIN_API_VERSION,
                "Content-Type": "application/json",
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute an HTTP request and handle LinkedIn-specific error responses."""
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise LinkedInNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise LinkedInNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        # Parse error body
        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        err_msg: str = (
            body.get("message")
            or body.get("serviceErrorCode", "")
            or response.text
            or "Unknown LinkedIn error"
        )

        if response.status_code == 401:
            raise LinkedInAuthError(
                f"Authentication failed: {err_msg}", 401, "unauthorized"
            )
        if response.status_code == 403:
            raise LinkedInAuthError(f"Forbidden: {err_msg}", 403, "forbidden")
        if response.status_code == 404:
            raise LinkedInNotFoundError("resource", path)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise LinkedInRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise LinkedInServerError(
                f"LinkedIn server error {response.status_code}: {err_msg}",
                response.status_code,
            )

        raise LinkedInError(
            f"LinkedIn error {response.status_code}: {err_msg}",
            response.status_code,
        )

    # ── Profile ───────────────────────────────────────────────────────────────

    async def get_profile(self) -> dict[str, Any]:
        """GET /me?projection=(id,firstName,lastName,profilePicture,headline)"""
        result = await self._request(
            "GET",
            "/me",
            params={"projection": "(id,firstName,lastName,profilePicture,headline)"},
        )
        return result  # type: ignore[return-value]

    async def get_email(self) -> dict[str, Any]:
        """GET /emailAddress?q=members&projection=(elements*(handle~))"""
        result = await self._request(
            "GET",
            "/emailAddress",
            params={"q": "members", "projection": "(elements*(handle~))"},
        )
        return result  # type: ignore[return-value]

    # ── Posts / Shares ────────────────────────────────────────────────────────

    async def list_posts(self, author_urn: str, count: int = 50) -> dict[str, Any]:
        """GET /shares?q=owners&owners={author_urn}&count={count}"""
        result = await self._request(
            "GET",
            "/shares",
            params={"q": "owners", "owners": author_urn, "count": count},
        )
        return result  # type: ignore[return-value]

    # ── Organizations ─────────────────────────────────────────────────────────

    async def get_organization(self, org_id: str) -> dict[str, Any]:
        """GET /organizations/{org_id}"""
        result = await self._request("GET", f"/organizations/{org_id}")
        return result  # type: ignore[return-value]

    async def list_organization_posts(self, org_urn: str, count: int = 50) -> dict[str, Any]:
        """GET /shares?q=owners&owners={org_urn}&count={count}"""
        result = await self._request(
            "GET",
            "/shares",
            params={"q": "owners", "owners": org_urn, "count": count},
        )
        return result  # type: ignore[return-value]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> LinkedInHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
