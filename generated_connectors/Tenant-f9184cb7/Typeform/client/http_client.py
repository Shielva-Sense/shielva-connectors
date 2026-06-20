from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    TypeformAuthError,
    TypeformError,
    TypeformNetworkError,
    TypeformNotFoundError,
    TypeformRateLimitError,
)

TYPEFORM_BASE_URL: str = "https://api.typeform.com"
DEFAULT_TIMEOUT_S: float = 30.0


class TypeformHTTPClient:
    """Low-level async HTTP client for the Typeform API v1.

    All requests pass the access token as a Bearer Authorization header.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{TYPEFORM_BASE_URL}{path}"
        headers = self._make_headers(access_token)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise TypeformNetworkError(f"Network error: {exc}") from exc
        except (
            TypeformError,
            TypeformAuthError,
            TypeformRateLimitError,
            TypeformNotFoundError,
            TypeformNetworkError,
        ):
            raise
        except Exception as exc:
            raise TypeformNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            try:
                return await response.json()
            except Exception:
                return {}

        # Attempt to read error body
        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        return self._raise_for_status(status, body, response)

    def _raise_for_status(
        self,
        status: int,
        body: dict[str, Any],
        response: aiohttp.ClientResponse | None = None,
    ) -> dict[str, Any]:
        """Raise the appropriate exception for non-2xx HTTP status codes."""
        err_msg: str = (
            body.get("description", "")
            or body.get("error", "")
            or body.get("message", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise TypeformAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise TypeformNotFoundError("resource", err_msg)
        if status == 429:
            retry_after: float = 0.0
            if response is not None:
                retry_after = float(response.headers.get("Retry-After", "0"))
            raise TypeformRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise TypeformNetworkError(
                f"Typeform server error {status}: {err_msg}",
                status_code=status,
            )
        raise TypeformError(f"Typeform error {status}: {err_msg}", status_code=status)

    # ── Me / Auth ─────────────────────────────────────────────────────────────

    async def get_me(self, access_token: str) -> dict[str, Any]:
        """GET /me — verify credentials and return account info."""
        return await self._request("GET", "/me", access_token)

    # ── Forms ─────────────────────────────────────────────────────────────────

    async def list_forms(
        self,
        access_token: str,
        workspace_id: str | None = None,
        page: int = 1,
        page_size: int = 200,
        search: str | None = None,
    ) -> dict[str, Any]:
        """GET /forms — paginated form listing."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if workspace_id:
            params["workspace_id"] = workspace_id
        if search:
            params["search"] = search
        return await self._request("GET", "/forms", access_token, params=params)

    async def get_form(self, access_token: str, form_id: str) -> dict[str, Any]:
        """GET /forms/{form_id} — fetch a single form definition."""
        return await self._request("GET", f"/forms/{form_id}", access_token)

    # ── Responses ─────────────────────────────────────────────────────────────

    async def get_responses(
        self,
        access_token: str,
        form_id: str,
        page_size: int = 1000,
        before: str | None = None,
        after: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """GET /forms/{form_id}/responses — cursor-paginated responses."""
        params: dict[str, Any] = {"page_size": page_size}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        return await self._request(
            "GET", f"/forms/{form_id}/responses", access_token, params=params
        )

    # ── Workspaces ────────────────────────────────────────────────────────────

    async def list_workspaces(
        self,
        access_token: str,
        page: int = 1,
        page_size: int = 200,
    ) -> dict[str, Any]:
        """GET /workspaces — paginated workspace listing."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        return await self._request("GET", "/workspaces", access_token, params=params)

    async def get_workspace(
        self, access_token: str, workspace_id: str
    ) -> dict[str, Any]:
        """GET /workspaces/{workspace_id} — fetch a single workspace."""
        return await self._request(
            "GET", f"/workspaces/{workspace_id}", access_token
        )

    # ── Insights ──────────────────────────────────────────────────────────────

    async def get_insights(
        self, access_token: str, form_id: str
    ) -> dict[str, Any]:
        """GET /insights/{form_id}/summary — form insights summary."""
        return await self._request(
            "GET", f"/insights/{form_id}/summary", access_token
        )
