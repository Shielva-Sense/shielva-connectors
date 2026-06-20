from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import aiohttp

from exceptions import (
    CalendlyAuthError,
    CalendlyError,
    CalendlyNetworkError,
    CalendlyNotFoundError,
    CalendlyRateLimitError,
)

CALENDLY_API_BASE: str = "https://api.calendly.com"
DEFAULT_TIMEOUT_S: float = 30.0


def _extract_uuid(uri: str) -> str:
    """Extract the last path segment (UUID) from a Calendly URI.

    Calendly uses full URIs like:
      https://api.calendly.com/scheduled_events/ABC123-...
    The UUID is the last path component.
    """
    return urlparse(uri).path.rstrip("/").split("/")[-1]


class CalendlyHTTPClient:
    """Low-level async HTTP client for the Calendly REST API v2."""

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
        url = f"{CALENDLY_API_BASE}{path}"
        headers = self._make_headers(access_token)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise CalendlyNetworkError(f"Network error: {exc}") from exc
        except (
            CalendlyError,
            CalendlyAuthError,
            CalendlyRateLimitError,
            CalendlyNotFoundError,
            CalendlyNetworkError,
        ):
            raise
        except Exception as exc:
            raise CalendlyNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status == 200:
            return await response.json()

        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        err_msg: str = (
            body.get("message", "")
            or body.get("title", "")
            or body.get("details", "")
            or f"HTTP {status}"
        )
        if isinstance(err_msg, list):
            err_msg = "; ".join(str(e) for e in err_msg)

        if status in (401, 403):
            raise CalendlyAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise CalendlyNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise CalendlyRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise CalendlyNetworkError(
                f"Calendly server error {status}: {err_msg}",
                status_code=status,
            )
        raise CalendlyError(
            f"Calendly error {status}: {err_msg}", status_code=status
        )

    # ── Current user ─────────────────────────────────────────────────────────

    async def get_current_user(self, access_token: str) -> dict[str, Any]:
        """GET /users/me — return the authenticated user resource."""
        return await self._request("GET", "/users/me", access_token)

    # ── Organization ──────────────────────────────────────────────────────────

    async def get_user_organization(
        self, access_token: str, organization_uri: str
    ) -> dict[str, Any]:
        """GET /organizations/{uuid} — fetch organization details."""
        uuid = _extract_uuid(organization_uri)
        return await self._request("GET", f"/organizations/{uuid}", access_token)

    # ── Event types ───────────────────────────────────────────────────────────

    async def list_event_types(
        self,
        access_token: str,
        user_uri: str,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """GET /event_types?user={user_uri} — list event types for a user."""
        params: dict[str, Any] = {"user": user_uri, "count": page_size}
        if page_token:
            params["page_token"] = page_token
        return await self._request("GET", "/event_types", access_token, params=params)

    # ── Scheduled events ──────────────────────────────────────────────────────

    async def list_scheduled_events(
        self,
        access_token: str,
        organization_uri: str | None = None,
        user_uri: str | None = None,
        status: str = "active",
        page_size: int = 100,
        page_token: str | None = None,
        min_start_time: str | None = None,
        count: int | None = None,
    ) -> dict[str, Any]:
        """GET /scheduled_events — list scheduled events for an org or user."""
        params: dict[str, Any] = {"status": status, "count": count if count is not None else page_size}
        if organization_uri:
            params["organization"] = organization_uri
        if user_uri:
            params["user"] = user_uri
        if page_token:
            params["page_token"] = page_token
        if min_start_time:
            params["min_start_time"] = min_start_time
        return await self._request(
            "GET", "/scheduled_events", access_token, params=params
        )

    async def get_scheduled_event(
        self, access_token: str, event_uuid: str
    ) -> dict[str, Any]:
        """GET /scheduled_events/{uuid} — fetch a single scheduled event by UUID."""
        # Accept full URI or bare UUID
        uuid = _extract_uuid(event_uuid) if event_uuid.startswith("http") else event_uuid
        return await self._request(
            "GET", f"/scheduled_events/{uuid}", access_token
        )

    # ── Invitees ──────────────────────────────────────────────────────────────

    async def list_event_invitees(
        self,
        access_token: str,
        event_uuid: str,
        page_size: int = 100,
        page_token: str | None = None,
        count: int | None = None,
    ) -> dict[str, Any]:
        """GET /scheduled_events/{uuid}/invitees — list invitees for an event."""
        # Accept full URI or bare UUID
        uuid = _extract_uuid(event_uuid) if event_uuid.startswith("http") else event_uuid
        params: dict[str, Any] = {"count": count if count is not None else page_size}
        if page_token:
            params["page_token"] = page_token
        return await self._request(
            "GET", f"/scheduled_events/{uuid}/invitees", access_token, params=params
        )

    # ── Organization memberships ──────────────────────────────────────────────

    async def list_organization_memberships(
        self,
        access_token: str,
        organization_uri: str,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """GET /organization_memberships?organization={uri} — list org members."""
        params: dict[str, Any] = {
            "organization": organization_uri,
            "count": page_size,
        }
        return await self._request(
            "GET", "/organization_memberships", access_token, params=params
        )
