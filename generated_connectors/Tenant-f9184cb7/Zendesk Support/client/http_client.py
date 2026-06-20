from __future__ import annotations

import base64
from typing import Any

import aiohttp

from exceptions import (
    ZendeskAuthError,
    ZendeskError,
    ZendeskNetworkError,
    ZendeskNotFoundError,
    ZendeskRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
ZENDESK_API_VERSION: str = "v2"


def _build_base_url(subdomain: str) -> str:
    return f"https://{subdomain}.zendesk.com/api/{ZENDESK_API_VERSION}"


def _make_auth_header(email: str, api_token: str) -> str:
    """Return a Basic Authorization header value for Zendesk token auth."""
    credentials = f"{email}/token:{api_token}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


class ZendeskHTTPClient:
    """Low-level async HTTP client for the Zendesk Support REST API v2."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(
        self,
        method: str,
        url: str,
        auth_header: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise ZendeskNetworkError(f"Network error: {exc}") from exc
        except (ZendeskError, ZendeskAuthError, ZendeskRateLimitError, ZendeskNotFoundError, ZendeskNetworkError):
            raise
        except Exception as exc:
            raise ZendeskNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        status = response.status

        if status == 200:
            return await response.json()

        # Attempt to read error body
        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        err_msg: str = (
            body.get("description", "")
            or body.get("error", "")
            or body.get("message", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise ZendeskAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise ZendeskNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise ZendeskRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise ZendeskNetworkError(
                f"Zendesk server error {status}: {err_msg}",
                status_code=status,
            )
        raise ZendeskError(f"Zendesk error {status}: {err_msg}", status_code=status)

    # ── Auth ─────────────────────────────────────────────────────────────────

    async def get_current_user(
        self, subdomain: str, email: str, api_token: str
    ) -> dict[str, Any]:
        """GET /users/me.json — verify credentials and return agent info."""
        url = f"{_build_base_url(subdomain)}/users/me.json"
        auth = _make_auth_header(email, api_token)
        return await self._request("GET", url, auth)

    # ── Tickets ──────────────────────────────────────────────────────────────

    async def list_tickets(
        self,
        subdomain: str,
        email: str,
        api_token: str,
        page: int = 1,
        per_page: int = 100,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        updated_after: str | None = None,
    ) -> dict[str, Any]:
        """GET /tickets.json — paginated ticket listing."""
        url = f"{_build_base_url(subdomain)}/tickets.json"
        auth = _make_auth_header(email, api_token)
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
        if updated_after:
            params["updated_after"] = updated_after
        return await self._request("GET", url, auth, params=params)

    async def get_ticket(
        self, subdomain: str, email: str, api_token: str, ticket_id: int
    ) -> dict[str, Any]:
        """GET /tickets/{id}.json — fetch a single ticket."""
        url = f"{_build_base_url(subdomain)}/tickets/{ticket_id}.json"
        auth = _make_auth_header(email, api_token)
        return await self._request("GET", url, auth)

    async def list_ticket_comments(
        self, subdomain: str, email: str, api_token: str, ticket_id: int
    ) -> dict[str, Any]:
        """GET /tickets/{id}/comments.json — fetch all comments for a ticket."""
        url = f"{_build_base_url(subdomain)}/tickets/{ticket_id}/comments.json"
        auth = _make_auth_header(email, api_token)
        return await self._request("GET", url, auth)

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(
        self,
        subdomain: str,
        email: str,
        api_token: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /users.json — paginated user listing."""
        url = f"{_build_base_url(subdomain)}/users.json"
        auth = _make_auth_header(email, api_token)
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", url, auth, params=params)

    async def get_user(
        self, subdomain: str, email: str, api_token: str, user_id: int
    ) -> dict[str, Any]:
        """GET /users/{id}.json — fetch a single user."""
        url = f"{_build_base_url(subdomain)}/users/{user_id}.json"
        auth = _make_auth_header(email, api_token)
        return await self._request("GET", url, auth)

    # ── Organizations ─────────────────────────────────────────────────────────

    async def list_organizations(
        self,
        subdomain: str,
        email: str,
        api_token: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /organizations.json — paginated organization listing."""
        url = f"{_build_base_url(subdomain)}/organizations.json"
        auth = _make_auth_header(email, api_token)
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", url, auth, params=params)

    # ── Macros ────────────────────────────────────────────────────────────────

    async def list_macros(
        self,
        subdomain: str,
        email: str,
        api_token: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /macros.json — paginated macro listing."""
        url = f"{_build_base_url(subdomain)}/macros.json"
        auth = _make_auth_header(email, api_token)
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", url, auth, params=params)
