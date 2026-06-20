from __future__ import annotations

import sys
import os

# Allow running from the connector root directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Any

import aiohttp

from exceptions import (
    GorgiasAuthError,
    GorgiasError,
    GorgiasNetworkError,
    GorgiasNotFoundError,
    GorgiasRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0


def _build_base_url(account: str) -> str:
    """Return the Gorgias REST API base URL for a given subdomain account."""
    return f"https://{account}.gorgias.com/api"


class GorgiasHTTPClient:
    """Low-level async HTTP client for the Gorgias REST API.

    Auth: HTTP Basic Auth — username = email, password = API key.
    Base URL: https://{account}.gorgias.com/api/
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._account: str = cfg.get("account", "")
        self._email: str = cfg.get("email", "")
        self._api_key: str = cfg.get("api_key", "")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # ── Low-level request ─────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        account: str = "",
        email: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        _account = account or self._account
        _email = email or self._email
        _api_key = api_key or self._api_key

        auth = aiohttp.BasicAuth(_email, _api_key)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=self._timeout, auth=auth
            ) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise GorgiasNetworkError(f"Network error: {exc}") from exc
        except (
            GorgiasError,
            GorgiasAuthError,
            GorgiasRateLimitError,
            GorgiasNotFoundError,
            GorgiasNetworkError,
        ):
            raise
        except Exception as exc:
            raise GorgiasNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            return await response.json()

        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        err_msg: str = (
            body.get("error", "")
            or body.get("message", "")
            or body.get("description", "")
            or f"HTTP {status}"
        )

        self._raise_for_status(status, err_msg, response)
        # Unreachable — _raise_for_status always raises for non-2xx
        raise GorgiasError(f"Gorgias error {status}: {err_msg}", status_code=status)

    def _raise_for_status(
        self,
        status: int,
        body: str,
        response: aiohttp.ClientResponse | None = None,
    ) -> None:
        """Map HTTP status codes to typed Gorgias exceptions."""
        if status in (401, 403):
            raise GorgiasAuthError(
                f"Authentication failed ({status}): {body}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise GorgiasNotFoundError("resource", body)
        if status == 429:
            retry_after = 0.0
            if response is not None:
                retry_after = float(response.headers.get("Retry-After", "0"))
            raise GorgiasRateLimitError(
                f"Rate limited: {body}", retry_after=retry_after
            )
        if status >= 500:
            raise GorgiasNetworkError(
                f"Gorgias server error {status}: {body}",
                status_code=status,
            )
        raise GorgiasError(f"Gorgias error {status}: {body}", status_code=status)

    # ── Account / health ──────────────────────────────────────────────────────

    async def get_account_info(
        self,
        account: str = "",
        email: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /api/account — verify credentials and return account info."""
        _account = account or self._account
        url = f"{_build_base_url(_account)}/account"
        return await self._request("GET", url, account=_account, email=email, api_key=api_key)

    # ── Tickets ───────────────────────────────────────────────────────────────

    async def get_tickets(
        self,
        page: int = 0,
        limit: int = 100,
        cursor: str | None = None,
        order_by: str = "created_datetime",
        order_dir: str = "desc",
        account: str = "",
        email: str = "",
        api_key: str = "",
        **params: Any,
    ) -> dict[str, Any]:
        """GET /api/tickets — cursor-paginated ticket listing.

        Gorgias uses cursor pagination via meta.next_cursor in the response.
        """
        _account = account or self._account
        url = f"{_build_base_url(_account)}/tickets"
        query: dict[str, Any] = {
            "limit": limit,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if cursor:
            query["cursor"] = cursor
        elif page:
            query["page"] = page
        query.update(params)
        return await self._request("GET", url, params=query, account=_account, email=email, api_key=api_key)

    async def get_ticket(
        self,
        ticket_id: int,
        account: str = "",
        email: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /api/tickets/{id} — fetch a single ticket."""
        _account = account or self._account
        url = f"{_build_base_url(_account)}/tickets/{ticket_id}"
        return await self._request("GET", url, account=_account, email=email, api_key=api_key)

    # ── Customers ─────────────────────────────────────────────────────────────

    async def get_customers(
        self,
        page: int = 0,
        limit: int = 100,
        cursor: str | None = None,
        account: str = "",
        email: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /api/customers — cursor-paginated customer listing."""
        _account = account or self._account
        url = f"{_build_base_url(_account)}/customers"
        query: dict[str, Any] = {"limit": limit}
        if cursor:
            query["cursor"] = cursor
        elif page:
            query["page"] = page
        return await self._request("GET", url, params=query, account=_account, email=email, api_key=api_key)

    async def get_customer(
        self,
        customer_id: int,
        account: str = "",
        email: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /api/customers/{id} — fetch a single customer."""
        _account = account or self._account
        url = f"{_build_base_url(_account)}/customers/{customer_id}"
        return await self._request("GET", url, account=_account, email=email, api_key=api_key)

    # ── Tags ─────────────────────────────────────────────────────────────────

    async def get_tags(
        self,
        account: str = "",
        email: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /api/tags — list all tags (not paginated by Gorgias)."""
        _account = account or self._account
        url = f"{_build_base_url(_account)}/tags"
        return await self._request("GET", url, account=_account, email=email, api_key=api_key)

    # ── Macros ────────────────────────────────────────────────────────────────

    async def get_macros(
        self,
        page: int = 0,
        limit: int = 100,
        cursor: str | None = None,
        account: str = "",
        email: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /api/macros — cursor-paginated macro listing."""
        _account = account or self._account
        url = f"{_build_base_url(_account)}/macros"
        query: dict[str, Any] = {"limit": limit}
        if cursor:
            query["cursor"] = cursor
        elif page:
            query["page"] = page
        return await self._request("GET", url, params=query, account=_account, email=email, api_key=api_key)

    # ── Satisfaction surveys ──────────────────────────────────────────────────

    async def get_satisfaction_surveys(
        self,
        page: int = 0,
        limit: int = 100,
        cursor: str | None = None,
        account: str = "",
        email: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /api/satisfaction-surveys — cursor-paginated satisfaction survey listing."""
        _account = account or self._account
        url = f"{_build_base_url(_account)}/satisfaction-surveys"
        query: dict[str, Any] = {"limit": limit}
        if cursor:
            query["cursor"] = cursor
        elif page:
            query["page"] = page
        return await self._request("GET", url, params=query, account=_account, email=email, api_key=api_key)
