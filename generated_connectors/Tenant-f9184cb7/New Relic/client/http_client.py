from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from exceptions import (
    NewRelicAuthError,
    NewRelicNetworkError,
    NewRelicNotFoundError,
    NewRelicRateLimitError,
)

_US_BASE_URL = "https://api.newrelic.com/v2"
_EU_BASE_URL = "https://api.eu.newrelic.com/v2"
_GRAPHQL_URL = "https://api.newrelic.com/graphql"

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)


class NewRelicHTTPClient:
    """
    Async HTTP client for the New Relic REST v2 API and NerdGraph GraphQL API.

    Auth: ``Api-Key: {api_key}`` header (User API Key or License Ingest Key).
    Region: ``"US"`` (default) or ``"EU"``.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._api_key: str = cfg.get("api_key", "")
        region = str(cfg.get("region", "US")).upper()
        self._base_url: str = _EU_BASE_URL if region == "EU" else _US_BASE_URL
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "Api-Key": self._api_key,
                "Content-Type": "application/json",
            }
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=_DEFAULT_TIMEOUT,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    async def _raise_for_status(self, response: aiohttp.ClientResponse) -> None:
        if response.status == 200:
            return
        if response.status in (401, 403):
            raise NewRelicAuthError(
                f"Authentication failed: HTTP {response.status}",
                status_code=response.status,
                code="auth_error",
            )
        if response.status == 404:
            raise NewRelicNotFoundError()
        if response.status == 429:
            retry_after = float(response.headers.get("Retry-After", 0))
            raise NewRelicRateLimitError(
                "New Relic rate limit exceeded", retry_after=retry_after
            )
        if response.status >= 500:
            raise NewRelicNetworkError(
                f"New Relic server error: HTTP {response.status}",
                status_code=response.status,
                code="server_error",
            )

    # ------------------------------------------------------------------
    # REST v2 helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        session = self._get_session()
        url = f"{self._base_url}/{path.lstrip('/')}"
        async with session.get(url, params=params) as resp:
            await self._raise_for_status(resp)
            return await resp.json()

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    async def get_user(self) -> dict[str, Any]:
        """Health-check call — fetches the first user for the account."""
        data = await self._get("/users.json", params={"filter[email]": "me"})
        users = data.get("users", [])
        return users[0] if users else data

    async def list_alerts_policies(
        self, page: int | None = None
    ) -> dict[str, Any]:
        """GET /alerts_policies.json with optional page param."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        return await self._get("/alerts_policies.json", params=params or None)

    async def list_alerts_conditions(self, policy_id: int | str) -> dict[str, Any]:
        """GET /alerts_conditions.json?policy_id={id}"""
        return await self._get(
            "/alerts_conditions.json", params={"policy_id": str(policy_id)}
        )

    async def list_applications(
        self, page: int | None = None
    ) -> dict[str, Any]:
        """GET /applications.json with optional page param."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        return await self._get("/applications.json", params=params or None)

    async def list_incidents(
        self, page: int | None = None
    ) -> dict[str, Any]:
        """GET /alerts_incidents.json with optional page param."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        return await self._get("/alerts_incidents.json", params=params or None)

    async def graphql_query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST a NerdGraph GraphQL query to the NerdGraph endpoint."""
        session = self._get_session()
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        async with session.post(_GRAPHQL_URL, json=payload) as resp:
            await self._raise_for_status(resp)
            return await resp.json()

    # ------------------------------------------------------------------
    # Pagination helper — follows next_url
    # ------------------------------------------------------------------

    async def paginate(self, first_response: dict[str, Any], key: str) -> list[Any]:
        """
        Collect all items across pages for a given top-level key.
        Follows ``next_url`` links in the response.
        """
        session = self._get_session()
        items: list[Any] = list(first_response.get(key, []))
        next_url: str | None = first_response.get("next_url")

        while next_url:
            async with session.get(next_url) as resp:
                await self._raise_for_status(resp)
                data = await resp.json()
            items.extend(data.get(key, []))
            next_url = data.get("next_url")

        return items
