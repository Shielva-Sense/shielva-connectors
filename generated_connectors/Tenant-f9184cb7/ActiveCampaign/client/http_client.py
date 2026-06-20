from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    ActiveCampaignAuthError,
    ActiveCampaignError,
    ActiveCampaignNetworkError,
    ActiveCampaignNotFoundError,
    ActiveCampaignRateLimitError,
    ActiveCampaignServerError,
)

DEFAULT_TIMEOUT_S: float = 30.0


class ActiveCampaignHTTPClient:
    """Low-level async HTTP client for the ActiveCampaign REST API v3.

    Uses aiohttp.ClientSession. Authentication via the Api-Token header.
    Base URL is derived from account_name: https://{account_name}.api-activecampaign.com/api/3
    """

    def __init__(
        self,
        api_key: str,
        account_name: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._account_name = account_name
        self._base_url = f"https://{account_name}.api-activecampaign.com/api/3"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._headers = {
            "Api-Token": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                headers=self._headers,
                timeout=self._timeout,
            )
        return self._session

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        session = self._get_session()
        try:
            async with session.request(method, path, **kwargs) as response:
                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                return self._raise_for_status(response, body)
        except (ActiveCampaignError,):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise ActiveCampaignNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            raise ActiveCampaignNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise ActiveCampaignNetworkError(f"Network error: {exc}") from exc

    def _raise_for_status(
        self, response: aiohttp.ClientResponse, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Map HTTP status codes to typed exceptions; return body on success."""
        status = response.status

        if status in (200, 201, 204):
            return body if body else {}

        # AC returns errors as {"errors": [{"title": "...", "code": "..."}]}
        errors = body.get("errors", [])
        if errors and isinstance(errors, list):
            first = errors[0] if errors else {}
            err_msg: str = (
                first.get("title") or first.get("detail") or f"HTTP {status}"
            )
            err_code: str = first.get("code", "")
        else:
            err_msg = body.get("message") or body.get("error") or f"HTTP {status}"
            err_code = ""

        if status == 401:
            raise ActiveCampaignAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if status == 403:
            raise ActiveCampaignAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if status == 404:
            raise ActiveCampaignNotFoundError("resource", str(response.url))
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise ActiveCampaignRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if status >= 500:
            raise ActiveCampaignServerError(
                f"ActiveCampaign server error {status}: {err_msg}",
                status,
            )

        raise ActiveCampaignError(
            f"ActiveCampaign error {status}: {err_msg}",
            status,
            err_code,
        )

    # ── Auth probe ───────────────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """GET /users/me — used for install / health check."""
        return await self._request("GET", "/users/me")

    async def get_accounts(self, limit: int = 1) -> dict[str, Any]:
        """GET /accounts?limit=1 — fallback health probe."""
        return await self._request("GET", "/accounts", params={"limit": limit})

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def get_contacts(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        return await self._request("GET", "/contacts", params=params)

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/contacts/{contact_id}")

    # Alias for backward compatibility with connector internal usage
    async def list_contacts(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return await self.get_contacts(limit=limit, offset=offset)

    # ── Lists ────────────────────────────────────────────────────────────────

    async def get_lists(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        return await self._request("GET", "/lists", params=params)

    async def list_lists(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return await self.get_lists(limit=limit, offset=offset)

    # ── Campaigns ────────────────────────────────────────────────────────────

    async def get_campaigns(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        return await self._request("GET", "/campaigns", params=params)

    async def list_campaigns(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return await self.get_campaigns(limit=limit, offset=offset)

    # ── Automations ──────────────────────────────────────────────────────────

    async def get_automations(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        return await self._request("GET", "/automations", params=params)

    async def list_automations(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return await self.get_automations(limit=limit, offset=offset)

    # ── Deals ────────────────────────────────────────────────────────────────

    async def get_deals(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        return await self._request("GET", "/deals", params=params)

    async def get_deal(self, deal_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/deals/{deal_id}")

    async def list_deals(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return await self.get_deals(limit=limit, offset=offset)

    # ── Tags ─────────────────────────────────────────────────────────────────

    async def get_tags(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        return await self._request("GET", "/tags", params=params)

    async def list_tags(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return await self.get_tags(limit=limit, offset=offset)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> ActiveCampaignHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
