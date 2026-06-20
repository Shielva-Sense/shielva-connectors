from __future__ import annotations

import time
from typing import Any

import httpx

from exceptions import (
    MarketoAuthError,
    MarketoError,
    MarketoNetworkError,
    MarketoNotFoundError,
    MarketoRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0
TOKEN_EXPIRY_BUFFER_S = 60  # refresh token this many seconds before it expires

# Marketo API error codes that map to auth failures
_AUTH_ERROR_CODES = {"600", "601", "602", "603"}
# Marketo API error code for rate limiting
_RATE_LIMIT_CODE = "606"
# Marketo API error code for not found
_NOT_FOUND_CODES = {"702"}


class MarketoHTTPClient:
    """Low-level async HTTP client for the Marketo REST API.

    Authentication uses OAuth 2.0 Client Credentials:
      GET https://{munchkin_id}.mktorest.com/identity/oauth/token
          ?grant_type=client_credentials&client_id=X&client_secret=Y

    All REST API calls require ``Authorization: Bearer {access_token}``.
    Tokens expire in 3600 s; this client auto-refreshes them.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._client_id: str = cfg.get("client_id", "")
        self._client_secret: str = cfg.get("client_secret", "")
        self._munchkin_id: str = cfg.get("munchkin_id", "")

        self._access_token: str = ""
        self._token_expires_at: float = 0.0

        # Base URL for identity (token) endpoint
        self._identity_base = f"https://{self._munchkin_id}.mktorest.com"
        # Base URL for REST API
        self._api_base = f"https://{self._munchkin_id}.mktorest.com/rest/v1"

        self._http = httpx.AsyncClient(timeout=timeout)

    # ── Token management ─────────────────────────────────────────────────────

    async def authenticate(self) -> str:
        """Obtain (or return cached) OAuth access token via client credentials."""
        now = time.monotonic()
        if self._access_token and now < self._token_expires_at - TOKEN_EXPIRY_BUFFER_S:
            return self._access_token

        url = (
            f"{self._identity_base}/identity/oauth/token"
            f"?grant_type=client_credentials"
            f"&client_id={self._client_id}"
            f"&client_secret={self._client_secret}"
        )
        try:
            response = await self._http.get(url)
        except httpx.TimeoutException as exc:
            raise MarketoNetworkError(f"Token request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise MarketoNetworkError(f"Network error during auth: {exc}") from exc

        if response.status_code == 401:
            raise MarketoAuthError(
                "Marketo authentication failed: invalid client_id or client_secret",
                401,
                "auth_failed",
            )
        if response.status_code != 200:
            raise MarketoError(
                f"Unexpected auth response {response.status_code}: {response.text}",
                response.status_code,
            )

        data: dict[str, Any] = response.json()
        if "error" in data:
            raise MarketoAuthError(
                f"Marketo token error: {data.get('error_description', data['error'])}",
                401,
                data["error"],
            )

        self._access_token = data["access_token"]
        expires_in: int = int(data.get("expires_in", 3600))
        self._token_expires_at = now + expires_in
        return self._access_token

    # ── Request plumbing ─────────────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Issue an authenticated request to the Marketo REST API."""
        token = await self.authenticate()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        url = f"{self._api_base}{path}"
        try:
            response = await self._http.request(
                method, url, headers=headers, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise MarketoNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise MarketoNetworkError(f"Network error: {exc}") from exc

        return self._raise_for_status(response.status_code, response)

    def _raise_for_status(
        self, status: int, response: httpx.Response
    ) -> dict[str, Any]:
        """Inspect HTTP status and Marketo success/errors fields; raise on failure."""
        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        if status == 404:
            raise MarketoNotFoundError("resource", response.url.path)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise MarketoRateLimitError("Marketo rate limit exceeded", retry_after)
        if status == 401:
            raise MarketoAuthError("Marketo authentication failed", 401, "auth_failed")
        if status >= 500:
            raise MarketoError(
                f"Marketo server error {status}: {response.text}", status
            )
        if status not in (200, 201, 204):
            raise MarketoError(
                f"Marketo error {status}: {response.text}", status
            )

        # Marketo returns HTTP 200 but embeds errors in the body
        if not body.get("success", True):
            errors: list[dict[str, Any]] = body.get("errors", [])
            if errors:
                err = errors[0]
                code = str(err.get("code", ""))
                msg = err.get("message", "Unknown Marketo error")
                if code in _AUTH_ERROR_CODES:
                    raise MarketoAuthError(msg, 200, code)
                if code == _RATE_LIMIT_CODE:
                    raise MarketoRateLimitError(msg)
                if code in _NOT_FOUND_CODES:
                    raise MarketoNotFoundError("resource", msg)
                raise MarketoError(msg, 200, code)
            raise MarketoError("Marketo request failed with success=false", 200)

        return body

    # ── Leads ────────────────────────────────────────────────────────────────

    async def get_leads(
        self,
        fields: list[str] | None = None,
        filter_type: str | None = None,
        filter_values: list[str] | None = None,
        next_page_token: str | None = None,
    ) -> dict[str, Any]:
        """GET /leads.json — browse/filter leads.

        Marketo requires either filterType+filterValues or nextPageToken for pagination.
        When no filter is provided we use a broad email filter to page all leads.
        """
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if filter_type and filter_values:
            params["filterType"] = filter_type
            params["filterValues"] = ",".join(str(v) for v in filter_values)
        if next_page_token:
            params["nextPageToken"] = next_page_token
        return await self._request("GET", "/leads.json", params=params)

    async def get_lead(self, lead_id: int) -> dict[str, Any]:
        """GET /lead/{id}.json — retrieve a single lead by ID."""
        return await self._request("GET", f"/lead/{lead_id}.json")

    async def get_leads_probe(self) -> dict[str, Any]:
        """Lightweight probe: GET /leads.json?filterType=id&filterValues=1&fields=id."""
        params: dict[str, Any] = {
            "filterType": "id",
            "filterValues": "1",
            "fields": "id",
        }
        token = await self.authenticate()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        url = f"{self._api_base}/leads.json"
        try:
            response = await self._http.get(url, headers=headers, params=params)
        except httpx.TimeoutException as exc:
            raise MarketoNetworkError(f"Probe timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise MarketoNetworkError(f"Probe network error: {exc}") from exc
        # 200 even if lead 1 doesn't exist — success:true is enough for a probe
        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass
        if response.status_code == 401:
            raise MarketoAuthError("Probe: authentication failed", 401, "auth_failed")
        if response.status_code >= 500:
            raise MarketoError(f"Probe server error {response.status_code}", response.status_code)
        if not body.get("success", True):
            errors = body.get("errors", [])
            if errors:
                code = str(errors[0].get("code", ""))
                msg = errors[0].get("message", "Probe failed")
                if code in _AUTH_ERROR_CODES:
                    raise MarketoAuthError(msg, 200, code)
                raise MarketoError(msg, 200, code)
        return body

    # ── Lists ─────────────────────────────────────────────────────────────────

    async def get_lists(self, next_page_token: str | None = None) -> dict[str, Any]:
        """GET /lists.json — retrieve static lists."""
        params: dict[str, Any] = {}
        if next_page_token:
            params["nextPageToken"] = next_page_token
        return await self._request("GET", "/lists.json", params=params)

    # ── Campaigns ────────────────────────────────────────────────────────────

    async def get_campaigns(
        self, next_page_token: str | None = None
    ) -> dict[str, Any]:
        """GET /campaigns.json — retrieve smart/batch campaigns."""
        params: dict[str, Any] = {}
        if next_page_token:
            params["nextPageToken"] = next_page_token
        return await self._request("GET", "/campaigns.json", params=params)

    # ── Programs ─────────────────────────────────────────────────────────────

    async def get_programs(
        self, offset: int = 0, max_return: int = 200
    ) -> dict[str, Any]:
        """GET /programs.json — retrieve programs with offset-based pagination."""
        params: dict[str, Any] = {"offset": offset, "maxReturn": max_return}
        return await self._request("GET", "/programs.json", params=params)

    # ── Activity types ────────────────────────────────────────────────────────

    async def get_activity_types(self) -> dict[str, Any]:
        """GET /activities/types.json — retrieve all activity type definitions."""
        return await self._request("GET", "/activities/types.json")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> MarketoHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
