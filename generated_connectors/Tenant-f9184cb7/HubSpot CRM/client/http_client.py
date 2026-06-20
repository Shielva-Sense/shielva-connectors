from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    HubSpotAuthError,
    HubSpotError,
    HubSpotNetworkError,
    HubSpotNotFoundError,
    HubSpotRateLimitError,
)

HUBSPOT_BASE_URL = "https://api.hubapi.com"
DEFAULT_TIMEOUT_S = 30


_CONTACT_PROPS = "firstname,lastname,email,phone,company,createdate,lastmodifieddate"
_COMPANY_PROPS = "name,domain,industry,city,country,phone,numberofemployees,createdate"
_DEAL_PROPS = "dealname,amount,dealstage,pipeline,closedate,createdate,hubspot_owner_id"
_TICKET_PROPS = "subject,content,hs_ticket_priority,hs_pipeline_stage,createdate,hs_lastmodifieddate"


class HubSpotHTTPClient:
    """Low-level async HTTP client for the HubSpot CRM v3 API using aiohttp."""

    def __init__(
        self,
        access_token: str = "",
        timeout: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=HUBSPOT_BASE_URL,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse, body: dict[str, Any]
    ) -> None:
        status = response.status
        if status in (200, 201, 204):
            return

        err_msg = (
            body.get("message")
            or body.get("error")
            or str(status)
        )

        if status in (401, 403):
            raise HubSpotAuthError(
                f"HubSpot authentication failed: {err_msg}",
                status_code=status,
                code=body.get("category", ""),
            )
        if status == 404:
            resource_type = body.get("context", {}).get("type", "resource")
            path = str(response.url.path)
            raise HubSpotNotFoundError(resource_type, path)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise HubSpotRateLimitError(
                f"HubSpot rate limit exceeded: {err_msg}",
                retry_after=retry_after,
            )
        if status >= 500:
            raise HubSpotNetworkError(
                f"HubSpot server error {status}: {err_msg}",
                status_code=status,
            )
        raise HubSpotError(
            f"HubSpot error {status}: {err_msg}",
            status_code=status,
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._get_session()
        try:
            async with session.get(path, params=params) as resp:
                body: dict[str, Any] = {}
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    pass
                await self._raise_for_status(resp, body)
                return body
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise HubSpotNetworkError(f"Network error: {exc}") from exc

    async def _post(self, path: str, json_data: dict[str, Any]) -> dict[str, Any]:
        session = self._get_session()
        try:
            async with session.post(path, json=json_data) as resp:
                body: dict[str, Any] = {}
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    pass
                await self._raise_for_status(resp, body)
                return body
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise HubSpotNetworkError(f"Network error: {exc}") from exc

    # ── Token / auth ─────────────────────────────────────────────────────────

    async def get_access_token_info(self, access_token: str) -> dict[str, Any]:
        """GET /oauth/v1/access-tokens/{token} — verify token validity."""
        return await self._get(f"/oauth/v1/access-tokens/{access_token}")

    async def refresh_access_token(
        self, client_id: str, client_secret: str, refresh_token: str
    ) -> dict[str, Any]:
        """POST form-encoded to /oauth/v1/token to refresh the access token."""
        session = self._get_session()
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
        try:
            async with session.post(
                "/oauth/v1/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                body: dict[str, Any] = {}
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    pass
                await self._raise_for_status(resp, body)
                return body
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise HubSpotNetworkError(f"Network error: {exc}") from exc

    async def exchange_code_for_token(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        code: str,
    ) -> dict[str, Any]:
        """Exchange an OAuth authorization code for access + refresh tokens."""
        session = self._get_session()
        data = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        try:
            async with session.post(
                "/oauth/v1/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                body: dict[str, Any] = {}
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    pass
                await self._raise_for_status(resp, body)
                return body
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise HubSpotNetworkError(f"Network error: {exc}") from exc

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def get_contacts(
        self, limit: int = 100, after: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit,
            "properties": _CONTACT_PROPS,
        }
        if after:
            params["after"] = after
        return await self._get("/crm/v3/objects/contacts", params=params)

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        return await self._get(
            f"/crm/v3/objects/contacts/{contact_id}",
            params={"properties": _CONTACT_PROPS},
        )

    # ── Companies ────────────────────────────────────────────────────────────

    async def get_companies(
        self, limit: int = 100, after: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit,
            "properties": _COMPANY_PROPS,
        }
        if after:
            params["after"] = after
        return await self._get("/crm/v3/objects/companies", params=params)

    # ── Deals ────────────────────────────────────────────────────────────────

    async def get_deals(
        self, limit: int = 100, after: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit,
            "properties": _DEAL_PROPS,
        }
        if after:
            params["after"] = after
        return await self._get("/crm/v3/objects/deals", params=params)

    async def get_deal(self, deal_id: str) -> dict[str, Any]:
        return await self._get(
            f"/crm/v3/objects/deals/{deal_id}",
            params={"properties": _DEAL_PROPS},
        )

    # ── Tickets ──────────────────────────────────────────────────────────────

    async def get_tickets(
        self, limit: int = 100, after: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit,
            "properties": _TICKET_PROPS,
        }
        if after:
            params["after"] = after
        return await self._get("/crm/v3/objects/tickets", params=params)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> HubSpotHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
