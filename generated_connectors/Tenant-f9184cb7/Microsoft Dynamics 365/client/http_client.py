from __future__ import annotations

import time
from typing import Any

import aiohttp

from exceptions import (
    Dynamics365AuthError,
    Dynamics365Error,
    Dynamics365NetworkError,
    Dynamics365NotFoundError,
    Dynamics365RateLimitError,
)

DATAVERSE_API_VERSION = "v9.2"
MS_GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
OAUTH_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
DEFAULT_TIMEOUT_S = 30.0

# Fields to select per entity
_CONTACT_SELECT = (
    "contactid,firstname,lastname,emailaddress1,telephone1,"
    "jobtitle,createdon,modifiedon,_parentcustomerid_value"
)
_ACCOUNT_SELECT = (
    "accountid,name,emailaddress1,telephone1,websiteurl,"
    "industry,revenue,createdon,modifiedon"
)
_LEAD_SELECT = (
    "leadid,firstname,lastname,companyname,emailaddress1,"
    "telephone1,leadsourcecode,statuscode,createdon,modifiedon"
)
_OPPORTUNITY_SELECT = (
    "opportunityid,name,estimatedvalue,actualclosedate,"
    "closeprobability,stepname,createdon,modifiedon,"
    "_parentaccountid_value,statecode"
)


class Dynamics365HTTPClient:
    """
    Low-level async HTTP client for the Microsoft Dataverse Web API (Dynamics 365 CRM).

    Uses aiohttp.ClientSession internally. Automatically refreshes the access
    token when it is near expiry (within 60 seconds of token_expires_at).
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._client_id: str = cfg.get("client_id", "")
        self._client_secret: str = cfg.get("client_secret", "")
        self._az_tenant_id: str = cfg.get("tenant_id", "")
        self._instance_url: str = cfg.get("instance_url", "").rstrip("/")
        self._access_token: str = cfg.get("access_token", "")
        self._refresh_token: str = cfg.get("refresh_token", "")
        self._token_expires_at: float = float(cfg.get("token_expires_at", 0))
        self._timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_S)
        self._session: aiohttp.ClientSession | None = None

    # ── Session management ────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Prefer": "odata.include-annotations=OData.Community.Display.V1.FormattedValue",
        }

    # ── Token refresh ─────────────────────────────────────────────────────────

    def _is_token_expired(self) -> bool:
        """True if no token yet or token expires within 60 seconds."""
        if not self._access_token:
            return True
        if self._token_expires_at == 0:
            return False  # no expiry info → assume valid
        return time.monotonic() >= self._token_expires_at - 60

    async def refresh_token(self) -> dict[str, Any]:
        """
        Exchange the refresh_token for a new access_token using the Azure AD token endpoint.
        Updates internal state and returns the raw token response dict.
        """
        if not self._refresh_token:
            raise Dynamics365AuthError("No refresh_token available; re-authorize the connector.")
        if not self._az_tenant_id:
            raise Dynamics365AuthError("tenant_id is required to refresh the access token.")

        url = OAUTH_TOKEN_URL.format(tenant_id=self._az_tenant_id)
        scope = f"{self._instance_url}/.default offline_access"
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "scope": scope,
        }
        session = self._get_session()
        try:
            async with session.post(url, data=data) as resp:
                body: dict[str, Any] = {}
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    pass
                if resp.status != 200:
                    err = body.get("error_description") or body.get("error") or f"HTTP {resp.status}"
                    raise Dynamics365AuthError(f"Token refresh failed: {err}", resp.status)

                self._access_token = body.get("access_token", self._access_token)
                if "refresh_token" in body:
                    self._refresh_token = body["refresh_token"]
                expires_in = body.get("expires_in", 3600)
                self._token_expires_at = time.monotonic() + float(expires_in)
                return body
        except Dynamics365AuthError:
            raise
        except aiohttp.ClientError as exc:
            raise Dynamics365NetworkError(f"Network error during token refresh: {exc}") from exc

    async def _ensure_token(self) -> None:
        """Refresh the access token if it is expired."""
        if self._is_token_expired():
            await self.refresh_token()

    # ── Low-level request ─────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._ensure_token()
        session = self._get_session()
        headers = self._auth_headers()
        try:
            async with session.request(
                method, url, headers=headers, params=params, json=json
            ) as resp:
                status = resp.status
                body: dict[str, Any] = {}
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    pass

                if status in (200, 201, 204):
                    return body

                self._raise_for_status(status, body)
                # unreachable — _raise_for_status always raises
                return body  # pragma: no cover
        except (Dynamics365Error,):
            raise
        except aiohttp.ClientConnectionError as exc:
            raise Dynamics365NetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise Dynamics365NetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise Dynamics365NetworkError(f"HTTP client error: {exc}") from exc

    @staticmethod
    def _raise_for_status(status: int, body: dict[str, Any]) -> None:
        """Map HTTP error codes to typed connector exceptions."""
        err_msg = (
            body.get("error", {}).get("message")
            or body.get("message")
            or f"HTTP {status}"
        )
        if status in (401, 403):
            raise Dynamics365AuthError(
                f"Authentication failed ({status}): {err_msg}", status_code=status
            )
        if status == 404:
            raise Dynamics365NotFoundError(err_msg)
        if status == 429:
            raise Dynamics365RateLimitError(f"Rate limited: {err_msg}")
        if status >= 500:
            raise Dynamics365NetworkError(
                f"Dynamics 365 server error {status}: {err_msg}", status_code=status
            )
        raise Dynamics365Error(f"Dynamics 365 error {status}: {err_msg}", status_code=status)

    # ── Dataverse entity fetches ───────────────────────────────────────────────

    def _dataverse_url(self, entity: str) -> str:
        return f"{self._instance_url}/api/data/{DATAVERSE_API_VERSION}/{entity}"

    async def get_contacts(self) -> list[dict[str, Any]]:
        """GET /contacts — returns list of CRM contact records."""
        url = self._dataverse_url("contacts")
        result = await self._request("GET", url, params={"$select": _CONTACT_SELECT, "$top": "100"})
        return result.get("value", [])

    async def get_accounts(self) -> list[dict[str, Any]]:
        """GET /accounts — returns list of CRM account records."""
        url = self._dataverse_url("accounts")
        result = await self._request("GET", url, params={"$select": _ACCOUNT_SELECT, "$top": "100"})
        return result.get("value", [])

    async def get_leads(self) -> list[dict[str, Any]]:
        """GET /leads — returns list of CRM lead records."""
        url = self._dataverse_url("leads")
        result = await self._request("GET", url, params={"$select": _LEAD_SELECT, "$top": "100"})
        return result.get("value", [])

    async def get_opportunities(self) -> list[dict[str, Any]]:
        """GET /opportunities — returns list of CRM opportunity records."""
        url = self._dataverse_url("opportunities")
        result = await self._request(
            "GET", url, params={"$select": _OPPORTUNITY_SELECT, "$top": "100"}
        )
        return result.get("value", [])

    async def get_activities(self) -> list[dict[str, Any]]:
        """GET /activitypointers — returns list of CRM activity records."""
        url = self._dataverse_url("activitypointers")
        result = await self._request(
            "GET", url, params={"$select": "activityid,subject,activitytypecode,createdon", "$top": "100"}
        )
        return result.get("value", [])

    async def get_me(self) -> dict[str, Any]:
        """GET https://graph.microsoft.com/v1.0/me — returns the authenticated user profile."""
        return await self._request("GET", MS_GRAPH_ME_URL)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> Dynamics365HTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
