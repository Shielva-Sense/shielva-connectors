from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    IntercomAuthError,
    IntercomError,
    IntercomNetworkError,
    IntercomNotFoundError,
    IntercomRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
INTERCOM_API_BASE: str = "https://api.intercom.io"
INTERCOM_VERSION: str = "2.10"


class IntercomHTTPClient:
    """Low-level async HTTP client for the Intercom REST API v2.11."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Intercom-Version": INTERCOM_VERSION,
        }

    async def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{INTERCOM_API_BASE}{path}"
        headers = self._make_headers(access_token)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params, json=json
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise IntercomNetworkError(f"Network error: {exc}") from exc
        except (
            IntercomError,
            IntercomAuthError,
            IntercomRateLimitError,
            IntercomNotFoundError,
            IntercomNetworkError,
        ):
            raise
        except Exception as exc:
            raise IntercomNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            return await response.json()

        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        err_msg: str = (
            body.get("message", "")
            or body.get("error", "")
            or body.get("description", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise IntercomAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise IntercomNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(response.headers.get("X-RateLimit-Reset", "0"))
            raise IntercomRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise IntercomNetworkError(
                f"Intercom server error {status}: {err_msg}",
                status_code=status,
            )
        raise IntercomError(f"Intercom error {status}: {err_msg}", status_code=status)

    # ── Me (auth check) ───────────────────────────────────────────────────────

    async def get_me(self, access_token: str) -> dict[str, Any]:
        """GET /me — verify credentials and return admin info."""
        return await self._request("GET", "/me", access_token)

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        access_token: str,
        page: int = 1,
        per_page: int = 150,
        starting_after: str | None = None,
    ) -> dict[str, Any]:
        """GET /contacts — paginated contact listing (leads + users).

        Uses cursor-based pagination via ``starting_after`` when provided.
        """
        params: dict[str, Any] = {"per_page": per_page}
        if starting_after:
            params["starting_after"] = starting_after
        else:
            params["page"] = page
        return await self._request("GET", "/contacts", access_token, params=params)

    async def get_contact(
        self,
        access_token: str,
        contact_id: str,
    ) -> dict[str, Any]:
        """GET /contacts/{id} — fetch a single contact."""
        return await self._request("GET", f"/contacts/{contact_id}", access_token)

    async def search_contacts(
        self,
        access_token: str,
        query: dict[str, Any],
        per_page: int = 150,
        starting_after: str | None = None,
    ) -> dict[str, Any]:
        """POST /contacts/search — search contacts by field/operator/value.

        ``query`` is the Intercom query object, e.g.
        ``{"field": "email", "operator": "=", "value": "x@y.com"}``.
        """
        pagination: dict[str, Any] = {"per_page": per_page}
        if starting_after:
            pagination["starting_after"] = starting_after
        body: dict[str, Any] = {"query": query, "pagination": pagination}
        return await self._request(
            "POST", "/contacts/search", access_token, json=body
        )

    # ── Conversations ─────────────────────────────────────────────────────────

    async def list_conversations(
        self,
        access_token: str,
        per_page: int = 20,
        starting_after: str | None = None,
    ) -> dict[str, Any]:
        """GET /conversations — paginated conversation listing.

        Uses cursor-based pagination via ``starting_after`` when provided.
        """
        params: dict[str, Any] = {"per_page": per_page}
        if starting_after:
            params["starting_after"] = starting_after
        return await self._request("GET", "/conversations", access_token, params=params)

    async def get_conversation(
        self,
        access_token: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        """GET /conversations/{id} — fetch a single conversation."""
        return await self._request(
            "GET", f"/conversations/{conversation_id}", access_token
        )

    # ── Companies ─────────────────────────────────────────────────────────────

    async def list_companies(
        self,
        access_token: str,
        page: int = 1,
        per_page: int = 150,
    ) -> dict[str, Any]:
        """GET /companies — list companies (page-based pagination)."""
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        return await self._request("GET", "/companies", access_token, params=params)

    # ── Admins ────────────────────────────────────────────────────────────────

    async def list_admins(self, access_token: str) -> dict[str, Any]:
        """GET /admins — list all admins in the workspace."""
        return await self._request("GET", "/admins", access_token)

    # ── Tags ──────────────────────────────────────────────────────────────────

    async def list_tags(self, access_token: str) -> dict[str, Any]:
        """GET /tags — list all tags."""
        return await self._request("GET", "/tags", access_token)

    # ── Segments ──────────────────────────────────────────────────────────────

    async def list_segments(self, access_token: str) -> dict[str, Any]:
        """GET /segments — list all segments."""
        return await self._request("GET", "/segments", access_token)
