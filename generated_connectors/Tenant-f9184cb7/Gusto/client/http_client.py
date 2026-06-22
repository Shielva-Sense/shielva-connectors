from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    GustoAuthError,
    GustoError,
    GustoNetworkError,
    GustoNotFoundError,
    GustoRateLimitError,
)

GUSTO_BASE_URL = "https://api.gusto.com"
GUSTO_API_V1 = f"{GUSTO_BASE_URL}/v1"
GUSTO_OAUTH_TOKEN_URL = f"{GUSTO_BASE_URL}/oauth/token"
DEFAULT_TIMEOUT_S = 30.0


class GustoHTTPClient:
    """Low-level async HTTP client for the Gusto REST API v1."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        url: str,
        access_token: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._get_session()
        headers = self._auth_headers(access_token)
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
            ) as response:
                status = response.status

                if status == 200:
                    return await response.json()  # type: ignore[no-any-return]

                body: dict[str, Any] = {}
                try:
                    body = await response.json()
                except Exception:
                    pass

                # Gusto returns errors as {"message": "..."} or {"errors": [...]}
                err_msg = (
                    body.get("message")
                    or str(body.get("errors", body))
                    or f"HTTP {status}"
                )
                err_code = str(status)

                if status in (401, 403):
                    raise GustoAuthError(
                        f"Authentication failed ({status}): {err_msg}",
                        status_code=status,
                        code=err_code,
                    )
                if status == 404:
                    raise GustoNotFoundError("resource", err_code)
                if status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise GustoRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )
                if status >= 500:
                    raise GustoNetworkError(
                        f"Gusto API server error {status}: {err_msg}",
                        status_code=status,
                    )
                raise GustoError(
                    f"Gusto API error {status}: {err_msg}",
                    status_code=status,
                    code=err_code,
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise GustoNetworkError(f"Network error: {exc}") from exc
        except (
            GustoAuthError,
            GustoNetworkError,
            GustoRateLimitError,
            GustoNotFoundError,
            GustoError,
        ):
            raise

    # ── Me / Current User ─────────────────────────────────────────────────────

    async def get_me(self, access_token: str) -> dict[str, Any]:
        """GET /v1/me — returns the current user info including company list."""
        url = f"{GUSTO_API_V1}/me"
        return await self._request("GET", url, access_token)

    # ── Companies ─────────────────────────────────────────────────────────────

    async def list_companies(self, access_token: str) -> list[dict[str, Any]]:
        """GET /v1/me — extract companies from the me response."""
        me = await self.get_me(access_token)
        # Gusto returns roles.payroll_admin.companies in /v1/me
        roles: dict[str, Any] = me.get("roles", {})
        payroll_admin: dict[str, Any] = roles.get("payroll_admin", {})
        companies: list[dict[str, Any]] = payroll_admin.get("companies", [])
        return companies

    # ── Employees ─────────────────────────────────────────────────────────────

    async def list_employees(
        self,
        access_token: str,
        company_id: str,
        page: int = 1,
        per: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /v1/companies/{company_id}/employees — paginated employee list."""
        url = f"{GUSTO_API_V1}/companies/{company_id}/employees"
        params: dict[str, Any] = {"page": page, "per": per}
        result = await self._request("GET", url, access_token, params=params)
        # Gusto returns a list directly or wraps in {"employees": [...]}
        if isinstance(result, list):
            return result  # type: ignore[return-value]
        return result.get("employees", result.get("data", []))  # type: ignore[return-value]

    async def get_employee(
        self,
        access_token: str,
        company_id: str,
        employee_id: str,
    ) -> dict[str, Any]:
        """GET /v1/companies/{company_id}/employees/{employee_id}."""
        url = f"{GUSTO_API_V1}/companies/{company_id}/employees/{employee_id}"
        return await self._request("GET", url, access_token)

    # ── Payrolls ──────────────────────────────────────────────────────────────

    async def list_payrolls(
        self,
        access_token: str,
        company_id: str,
        processed: bool = True,
    ) -> list[dict[str, Any]]:
        """GET /v1/companies/{company_id}/payrolls."""
        url = f"{GUSTO_API_V1}/companies/{company_id}/payrolls"
        params: dict[str, Any] = {"processed": str(processed).lower()}
        result = await self._request("GET", url, access_token, params=params)
        if isinstance(result, list):
            return result  # type: ignore[return-value]
        return result.get("payrolls", result.get("data", []))  # type: ignore[return-value]

    # ── Departments ───────────────────────────────────────────────────────────

    async def get_departments(
        self,
        access_token: str,
        company_id: str,
    ) -> list[dict[str, Any]]:
        """GET /v1/companies/{company_id}/departments."""
        url = f"{GUSTO_API_V1}/companies/{company_id}/departments"
        result = await self._request("GET", url, access_token)
        if isinstance(result, list):
            return result  # type: ignore[return-value]
        return result.get("departments", result.get("data", []))  # type: ignore[return-value]

    # ── Token exchange ────────────────────────────────────────────────────────

    async def exchange_code_for_token(
        self,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str = "",
    ) -> dict[str, Any]:
        """POST /oauth/token — exchange an authorization code for access + refresh tokens."""
        session = self._get_session()
        payload: dict[str, str] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        try:
            async with session.post(
                GUSTO_OAUTH_TOKEN_URL,
                data=payload,
            ) as response:
                status = response.status
                body: dict[str, Any] = {}
                try:
                    body = await response.json()
                except Exception:
                    pass
                if status == 200:
                    return body
                err_msg = body.get("error_description") or body.get("error") or f"HTTP {status}"
                if status in (401, 403):
                    raise GustoAuthError(f"Token exchange failed ({status}): {err_msg}", status_code=status)
                raise GustoError(f"Token exchange error {status}: {err_msg}", status_code=status)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise GustoNetworkError(f"Network error during token exchange: {exc}") from exc
        except (GustoAuthError, GustoError):
            raise

    async def refresh_access_token(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> dict[str, Any]:
        """POST /oauth/token — refresh an expired access token."""
        session = self._get_session()
        payload: dict[str, str] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            async with session.post(
                GUSTO_OAUTH_TOKEN_URL,
                data=payload,
            ) as response:
                status = response.status
                body: dict[str, Any] = {}
                try:
                    body = await response.json()
                except Exception:
                    pass
                if status == 200:
                    return body
                err_msg = body.get("error_description") or body.get("error") or f"HTTP {status}"
                if status in (401, 403):
                    raise GustoAuthError(f"Token refresh failed ({status}): {err_msg}", status_code=status)
                raise GustoError(f"Token refresh error {status}: {err_msg}", status_code=status)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise GustoNetworkError(f"Network error during token refresh: {exc}") from exc
        except (GustoAuthError, GustoError):
            raise

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> GustoHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
