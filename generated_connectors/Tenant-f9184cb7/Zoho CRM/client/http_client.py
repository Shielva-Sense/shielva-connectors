from __future__ import annotations

from typing import Any
from urllib.parse import quote

import aiohttp

from exceptions import (
    ZohoCRMAuthError,
    ZohoCRMError,
    ZohoCRMNetworkError,
    ZohoCRMNotFoundError,
    ZohoCRMRateLimitError,
    ZohoCRMServerError,
)

DEFAULT_TIMEOUT_S: float = 30.0
ZOHO_API_VERSION: str = "v2"


def _build_base_url(data_center: str) -> str:
    """Return the REST API base URL for the given Zoho data center."""
    dc = (data_center or "com").strip().lower()
    return f"https://www.zohoapis.{dc}/crm/{ZOHO_API_VERSION}"


def _build_auth_url(data_center: str) -> str:
    """Return the OAuth2 base URL for the given Zoho data center."""
    dc = (data_center or "com").strip().lower()
    return f"https://accounts.zoho.{dc}/oauth/v2/"


class ZohoCRMHTTPClient:
    """Low-level async HTTP client for the Zoho CRM REST API v2 using aiohttp."""

    def __init__(
        self,
        access_token: str,
        data_center: str = "com",
        timeout: float = DEFAULT_TIMEOUT_S,
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "",
    ) -> None:
        self._access_token = access_token
        self._data_center = (data_center or "com").strip().lower()
        self._base_url = _build_base_url(self._data_center)
        self._auth_base = _build_auth_url(self._data_center)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Zoho-oauthtoken {self._access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        return self._session

    def _update_token(self, access_token: str) -> None:
        """Update the bearer token (after a refresh) and recreate the session."""
        self._access_token = access_token
        if self._session and not self._session.closed:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_closed():
                    loop.run_until_complete(self._session.close())
            except Exception:
                pass
        self._session = None

    # ── Internal dispatcher ───────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self._base_url}/{path.lstrip('/')}"
        session = self._get_session()
        try:
            async with session.request(method, url, **kwargs) as response:
                return await self._raise_for_status(response)
        except aiohttp.ServerTimeoutError as exc:
            raise ZohoCRMNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            raise ZohoCRMNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise ZohoCRMNetworkError(f"Network error: {exc}") from exc

    async def _raise_for_status(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        """Parse the response, raise typed exceptions for non-2xx, return body for 2xx."""
        if response.status in (200, 201, 202, 204):
            if response.status == 204:
                return {}
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        # Parse error body
        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        # Zoho error shape: {"code": "...", "message": "...", "status": "error"}
        err_msg: str = body.get("message", str(response.status) + " Zoho CRM error")
        err_code: str = body.get("code", "")

        if response.status == 401:
            raise ZohoCRMAuthError(f"Authentication failed: {err_msg}", 401, err_code)
        if response.status == 403:
            raise ZohoCRMAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status == 404:
            raise ZohoCRMNotFoundError(err_code or "resource", err_msg)
        if response.status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise ZohoCRMRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status >= 500:
            raise ZohoCRMServerError(
                f"Zoho CRM server error {response.status}: {err_msg}",
                response.status,
            )
        raise ZohoCRMError(
            f"Zoho CRM error {response.status}: {err_msg}",
            response.status,
            err_code,
        )

    # ── Org / health ──────────────────────────────────────────────────────────

    async def get_org(self) -> dict[str, Any]:
        """GET /org — organization info (used as health check)."""
        return await self._request("GET", "org")

    async def get_current_user(self) -> dict[str, Any]:
        """GET /users?type=CurrentUser — current user info (legacy health check alias)."""
        return await self._request("GET", "users", params={"type": "CurrentUser"})

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def get_contacts(self, page: int = 1, per_page: int = 200) -> dict[str, Any]:
        """GET /Contacts?page={page}&per_page={per_page}."""
        return await self._request(
            "GET", "Contacts", params={"page": page, "per_page": per_page}
        )

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        """GET /Contacts/{contact_id}."""
        return await self._request("GET", f"Contacts/{quote(contact_id, safe='')}")

    # ── Leads ─────────────────────────────────────────────────────────────────

    async def get_leads(self, page: int = 1, per_page: int = 200) -> dict[str, Any]:
        """GET /Leads?page={page}&per_page={per_page}."""
        return await self._request(
            "GET", "Leads", params={"page": page, "per_page": per_page}
        )

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def get_accounts(self, page: int = 1, per_page: int = 200) -> dict[str, Any]:
        """GET /Accounts?page={page}&per_page={per_page}."""
        return await self._request(
            "GET", "Accounts", params={"page": page, "per_page": per_page}
        )

    # ── Deals ─────────────────────────────────────────────────────────────────

    async def get_deals(self, page: int = 1, per_page: int = 200) -> dict[str, Any]:
        """GET /Deals?page={page}&per_page={per_page}."""
        return await self._request(
            "GET", "Deals", params={"page": page, "per_page": per_page}
        )

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(
        self, user_type: str = "ActiveUsers", page: int = 1, per_page: int = 200
    ) -> dict[str, Any]:
        """GET /Users?type={user_type}&page={page}&per_page={per_page}."""
        return await self._request(
            "GET",
            "Users",
            params={"type": user_type, "page": page, "per_page": per_page},
        )

    # ── Generic module records ────────────────────────────────────────────────

    async def list_records(
        self,
        module: str,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        """GET /{module}?page={page}&per_page={per_page} — any module."""
        return await self._request(
            "GET",
            quote(module, safe=""),
            params={"page": page, "per_page": per_page},
        )

    async def get_record(self, module: str, record_id: str) -> dict[str, Any]:
        """GET /{module}/{record_id}."""
        safe_module = quote(module, safe="")
        safe_id = quote(record_id, safe="")
        return await self._request("GET", f"{safe_module}/{safe_id}")

    async def search_records(self, module: str, criteria: str) -> dict[str, Any]:
        """GET /{module}/search?criteria={criteria}."""
        safe_module = quote(module, safe="")
        return await self._request(
            "GET",
            f"{safe_module}/search",
            params={"criteria": criteria},
        )

    # ── Token operations ──────────────────────────────────────────────────────

    async def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """POST to Zoho token endpoint to exchange refresh_token for a new access_token."""
        url = f"{self._auth_base}token"
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": refresh_token,
        }
        # Token endpoint uses a plain (no-auth) session
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(url, data=data) as response:
                try:
                    body: dict[str, Any] = await response.json(content_type=None)
                except Exception:
                    body = {}
                if response.status not in (200, 201):
                    err_msg = body.get("error", "token refresh failed")
                    raise ZohoCRMAuthError(f"Token refresh failed: {err_msg}", response.status)
                return body

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        """POST to Zoho token endpoint to exchange authorization code for tokens."""
        url = f"{self._auth_base}token"
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
        }
        if self._redirect_uri:
            data["redirect_uri"] = self._redirect_uri
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(url, data=data) as response:
                try:
                    body: dict[str, Any] = await response.json(content_type=None)
                except Exception:
                    body = {}
                if response.status not in (200, 201):
                    err_msg = body.get("error", "code exchange failed")
                    raise ZohoCRMAuthError(f"Code exchange failed: {err_msg}", response.status)
                return body

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> ZohoCRMHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
