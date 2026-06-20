from __future__ import annotations

import aiohttp
from typing import Any, Optional

from exceptions import (
    BrevoAuthError,
    BrevoError,
    BrevoNetworkError,
    BrevoNotFoundError,
    BrevoRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
BREVO_BASE_URL: str = "https://api.brevo.com"


class BrevoHTTPClient:
    """Low-level async HTTP client for the Brevo REST API v3.

    Authentication: ``api-key: {api_key}`` header (not Bearer, not Authorization).
    Base URL: https://api.brevo.com
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = BREVO_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                headers={
                    "api-key": self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        return self._session

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        session = self._get_session()
        try:
            async with session.request(method, path, **kwargs) as response:
                return await self._raise_for_status(response)
        except (BrevoError,):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise BrevoNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            raise BrevoNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise BrevoNetworkError(f"Network error: {exc}") from exc

    async def _raise_for_status(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        """Parse the response and raise an appropriate Brevo exception on error."""
        if response.status in (200, 201, 204):
            if response.status == 204 or response.content_length == 0:
                return {}
            try:
                return await response.json(content_type=None)  # type: ignore[no-any-return]
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_msg: str = (
            body.get("message")
            or body.get("error")
            or str(await response.text())
            or "Unknown error"
        )
        err_code: str = body.get("code", "")

        if response.status == 401:
            raise BrevoAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if response.status == 403:
            raise BrevoAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status == 404:
            raise BrevoNotFoundError("resource", str(response.url))
        if response.status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise BrevoRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status >= 500:
            raise BrevoError(
                f"Brevo server error {response.status}: {err_msg}",
                response.status,
                err_code,
            )

        raise BrevoError(
            f"Brevo error {response.status}: {err_msg}",
            response.status,
            err_code,
        )

    # ── Account ──────────────────────────────────────────────────────────────

    async def get_account(self) -> dict[str, Any]:
        """GET /v3/account — account info, used for install/health-check."""
        return await self._request("GET", "/v3/account")

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def get_contacts(
        self, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """GET /v3/contacts — paginated contact list."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        return await self._request("GET", "/v3/contacts", params=params)

    async def get_contact(self, identifier: str) -> dict[str, Any]:
        """GET /v3/contacts/{identifier} — contact by email or id."""
        return await self._request("GET", f"/v3/contacts/{identifier}")

    async def get_contact_lists(
        self, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """GET /v3/contacts/lists — paginated contact lists."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        return await self._request("GET", "/v3/contacts/lists", params=params)

    # ── Email Campaigns ──────────────────────────────────────────────────────

    async def get_email_campaigns(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
    ) -> dict[str, Any]:
        """GET /v3/emailCampaigns — paginated email campaign list."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        return await self._request("GET", "/v3/emailCampaigns", params=params)

    async def get_campaign_report(self, campaign_id: int | str) -> dict[str, Any]:
        """GET /v3/emailCampaigns/{campaign_id} — single campaign report."""
        return await self._request("GET", f"/v3/emailCampaigns/{campaign_id}")

    # ── Senders ──────────────────────────────────────────────────────────────

    async def get_senders(self) -> dict[str, Any]:
        """GET /v3/senders — list all senders."""
        return await self._request("GET", "/v3/senders")

    # ── SMTP Templates ───────────────────────────────────────────────────────

    async def get_smtp_templates(
        self, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """GET /v3/smtp/templates — paginated SMTP template list."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        return await self._request("GET", "/v3/smtp/templates", params=params)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> BrevoHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
