from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    FreshworksCRMAuthError,
    FreshworksCRMError,
    FreshworksCRMNetworkError,
    FreshworksCRMNotFoundError,
    FreshworksCRMRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0


def _base_url(domain: str) -> str:
    """Return the Freshworks CRM API v2 base URL for the given subdomain."""
    domain = domain.strip().rstrip("/")
    # Accept bare subdomain (e.g. "acme") or full host (acme.myfreshworks.com)
    if not domain.startswith("http"):
        if "." not in domain:
            domain = f"https://{domain}.myfreshworks.com"
        else:
            domain = f"https://{domain}"
    return f"{domain}/crm/sales/api/v2"


class FreshworksCRMHTTPClient:
    """Low-level async HTTP client for the Freshworks CRM (Freshsales) REST API v2.

    Authentication uses a Token header:
        Authorization: Token token={api_key}
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Token token={api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        domain: str,
        api_key: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        url = f"{_base_url(domain)}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                headers=self._auth_headers(api_key),
                **kwargs,
            ) as response:
                if response.status in (200, 201):
                    return await response.json(content_type=None)
                if response.status == 204:
                    return {}

                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                err_msg = str(body) if body else f"HTTP {response.status}"

                if response.status in (401, 403):
                    raise FreshworksCRMAuthError(
                        f"Authentication failed: {err_msg}",
                        status_code=response.status,
                        code="auth_error",
                    )
                if response.status == 404:
                    raise FreshworksCRMNotFoundError("resource", path)
                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise FreshworksCRMRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )
                if response.status >= 500:
                    raise FreshworksCRMNetworkError(
                        f"Freshworks CRM server error {response.status}: {err_msg}",
                        status_code=response.status,
                    )
                raise FreshworksCRMError(
                    f"Freshworks CRM error {response.status}: {err_msg}",
                    status_code=response.status,
                )
        except (FreshworksCRMError,):
            raise
        except aiohttp.ClientConnectorError as exc:
            raise FreshworksCRMNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise FreshworksCRMNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise FreshworksCRMNetworkError(f"Network error: {exc}") from exc

    # ── Owners (health check / auth validation) ───────────────────────────────

    async def list_owners(self, domain: str, api_key: str) -> dict[str, Any]:
        """GET /selector/owners — validates credentials and lists CRM owners."""
        return await self._request("GET", domain, api_key, "/selector/owners")

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        domain: str,
        api_key: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """POST /contacts/filters — list contacts with pagination.

        Freshworks CRM uses POST with a JSON body for filtered list endpoints.
        Returns: {"contacts": [...], "meta": {"total_pages": N, "current_page": N}}
        """
        payload: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        result = await self._request(
            "POST", domain, api_key, "/contacts/filters", json=payload
        )
        if isinstance(result, dict):
            return result
        return {"contacts": [], "meta": {}}

    async def get_contact(
        self, domain: str, api_key: str, contact_id: int
    ) -> dict[str, Any]:
        """GET /contacts/{id} — fetch a single contact."""
        result = await self._request(
            "GET", domain, api_key, f"/contacts/{contact_id}"
        )
        if isinstance(result, dict):
            return result
        return {}

    # ── Deals ─────────────────────────────────────────────────────────────────

    async def list_deals(
        self,
        domain: str,
        api_key: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """POST /deals/filters — list deals with pagination.

        Returns: {"deals": [...], "meta": {"total_pages": N, "current_page": N}}
        """
        payload: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        result = await self._request(
            "POST", domain, api_key, "/deals/filters", json=payload
        )
        if isinstance(result, dict):
            return result
        return {"deals": [], "meta": {}}

    async def get_deal(
        self, domain: str, api_key: str, deal_id: int
    ) -> dict[str, Any]:
        """GET /deals/{id} — fetch a single deal."""
        result = await self._request(
            "GET", domain, api_key, f"/deals/{deal_id}"
        )
        if isinstance(result, dict):
            return result
        return {}

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def list_accounts(
        self,
        domain: str,
        api_key: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """POST /sales_accounts/filters — list accounts with pagination.

        Returns: {"sales_accounts": [...], "meta": {"total_pages": N, "current_page": N}}
        """
        payload: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        result = await self._request(
            "POST", domain, api_key, "/sales_accounts/filters", json=payload
        )
        if isinstance(result, dict):
            return result
        return {"sales_accounts": [], "meta": {}}

    async def get_account(
        self, domain: str, api_key: str, account_id: int
    ) -> dict[str, Any]:
        """GET /sales_accounts/{id} — fetch a single account."""
        result = await self._request(
            "GET", domain, api_key, f"/sales_accounts/{account_id}"
        )
        if isinstance(result, dict):
            return result
        return {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> FreshworksCRMHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
