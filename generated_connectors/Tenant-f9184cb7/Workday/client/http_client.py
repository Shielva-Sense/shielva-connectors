"""Low-level async HTTP client for the Workday REST API.

Handles OAuth 2.0 Client Credentials flow and all resource endpoints with
pagination support (offset/limit). Token is auto-fetched and refreshed.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Any

import aiohttp

from exceptions import (
    WorkdayAuthError,
    WorkdayError,
    WorkdayNetworkError,
    WorkdayNotFoundError,
    WorkdayRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_PAGE_SIZE: int = 100
WORKDAY_API_VERSION: str = "v1"


class WorkdayHTTPClient:
    """Async HTTP client for the Workday REST API.

    Authenticates via OAuth 2.0 Client Credentials and provides paginated
    access to workers, organizations, job profiles, and locations.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._config: dict[str, Any] = config or {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._access_token: str = ""

    # ── Private helpers ───────────────────────────────────────────────────────

    def _client_id(self) -> str:
        return str(self._config.get("client_id", ""))

    def _client_secret(self) -> str:
        return str(self._config.get("client_secret", ""))

    def _tenant(self) -> str:
        return str(self._config.get("tenant", ""))

    def _hostname(self) -> str:
        """Return the Workday hostname.

        Prefers the 'hostname' key (spec format, e.g. 'wd2-impl-services1.workday.com').
        Falls back to deriving from 'base_url' for backward compatibility.
        """
        hostname = self._config.get("hostname", "")
        if hostname:
            return str(hostname).rstrip("/")
        base_url = str(self._config.get("base_url", "")).rstrip("/")
        if base_url.startswith("https://"):
            return base_url[len("https://"):]
        if base_url.startswith("http://"):
            return base_url[len("http://"):]
        return base_url

    def _base_url(self) -> str:
        """Return the scheme+hostname base URL."""
        hostname = self._config.get("hostname", "")
        if hostname:
            return f"https://{str(hostname).rstrip('/')}"
        return str(self._config.get("base_url", "")).rstrip("/")

    def _token_url(self) -> str:
        hostname = self._hostname()
        tenant = self._tenant()
        return f"https://{hostname}/ccx/oauth2/{tenant}/token"

    def _api_base(self) -> str:
        hostname = self._hostname()
        tenant = self._tenant()
        return f"https://{hostname}/ccx/api/{WORKDAY_API_VERSION}/{tenant}"

    # ── Authentication ────────────────────────────────────────────────────────

    async def authenticate(self) -> str:
        """Obtain an OAuth 2.0 Bearer token via Client Credentials grant.

        Stores the token internally and returns it.
        """
        token_url = self._token_url()
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id(),
            "client_secret": self._client_secret(),
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(token_url, data=data, headers=headers) as resp:
                    if resp.status in (401, 403):
                        body: dict[str, Any] = {}
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            pass
                        err = body.get("error_description", "") or body.get("error", "") or f"HTTP {resp.status}"
                        raise WorkdayAuthError(
                            f"OAuth2 authentication failed ({resp.status}): {err}",
                            status_code=resp.status,
                            code="auth_error",
                        )
                    if resp.status not in (200, 201):
                        body = {}
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            pass
                        err = body.get("error_description", "") or body.get("error", "") or f"HTTP {resp.status}"
                        raise WorkdayError(
                            f"Token request failed ({resp.status}): {err}",
                            status_code=resp.status,
                        )
                    token_data: dict[str, Any] = await resp.json(content_type=None)
                    token = token_data.get("access_token", "")
                    if not token:
                        raise WorkdayAuthError(
                            "OAuth2 response did not contain access_token",
                            status_code=resp.status,
                            code="auth_error",
                        )
                    self._access_token = token
                    return token
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise WorkdayNetworkError(f"Network error during authentication: {exc}") from exc
        except (WorkdayError, WorkdayAuthError, WorkdayNetworkError):
            raise
        except Exception as exc:
            raise WorkdayNetworkError(f"Unexpected error during authentication: {exc}") from exc

    async def _ensure_token(self) -> str:
        """Return the cached token, fetching a new one if not yet obtained."""
        if not self._access_token:
            await self.authenticate()
        return self._access_token

    # ── Core request ──────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        """Execute an authenticated request, refreshing the token on 401."""
        token = await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                ) as response:
                    if response.status == 401 and retry_auth:
                        # Token may have expired — refresh once
                        self._access_token = ""
                        await self.authenticate()
                        return await self._request(
                            method, url, params=params, json_body=json_body, retry_auth=False
                        )
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise WorkdayNetworkError(f"Network error: {exc}") from exc
        except (WorkdayError, WorkdayAuthError, WorkdayRateLimitError, WorkdayNotFoundError, WorkdayNetworkError):
            raise
        except Exception as exc:
            raise WorkdayNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        """Map HTTP response status to the appropriate exception or return body."""
        status = response.status

        if status in (200, 201):
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        self._raise_for_status(status, body)
        # unreachable — _raise_for_status always raises
        raise WorkdayError(f"HTTP {status}", status_code=status)  # pragma: no cover

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Raise the appropriate WorkdayError subclass for a non-2xx status."""
        err_msg: str = (
            body.get("error_description", "")
            or body.get("error", "")
            or body.get("message", "")
            or body.get("detail", "")
            or f"HTTP {status}"
        )
        if status in (401, 403):
            raise WorkdayAuthError(
                f"Workday authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise WorkdayNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = 0.0
            try:
                retry_after = float(body.get("retry_after", 0))
            except (ValueError, TypeError):
                pass
            raise WorkdayRateLimitError(f"Rate limited: {err_msg}", retry_after=retry_after)
        if status >= 500:
            raise WorkdayNetworkError(
                f"Workday server error {status}: {err_msg}",
                status_code=status,
            )
        raise WorkdayError(f"Workday error {status}: {err_msg}", status_code=status)

    # ── Paginated list helper ─────────────────────────────────────────────────

    async def _get_paginated(
        self,
        path: str,
        result_key: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        extra_params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a paginated Workday endpoint.

        Workday REST API uses offset + limit pagination. Iterates until
        all records are retrieved.
        """
        url = f"{self._api_base()}/{path.lstrip('/')}"
        all_items: list[dict[str, Any]] = []
        offset = 0

        while True:
            params: dict[str, Any] = {
                "limit": page_size,
                "offset": offset,
            }
            if extra_params:
                params.update(extra_params)

            response = await self._request("GET", url, params=params)

            # Workday wraps results under a key (e.g. "data") or the result_key
            items: list[dict[str, Any]] = (
                response.get(result_key)
                or response.get("data")
                or []
            )

            if not isinstance(items, list):
                items = []

            all_items.extend(items)

            # Check for more pages
            total = response.get("total", None)
            if total is not None:
                if offset + len(items) >= int(total):
                    break
            elif len(items) < page_size:
                # No total field — stop when we get fewer items than requested
                break

            if not items:
                break

            offset += len(items)

        return all_items

    # ── Resource endpoints ────────────────────────────────────────────────────

    async def get_workers(self) -> list[dict[str, Any]]:
        """GET /workers — return all workers with pagination."""
        return await self._get_paginated("workers", result_key="data")

    async def get_organizations(self) -> list[dict[str, Any]]:
        """GET /organizations — return all organizations with pagination."""
        return await self._get_paginated("organizations", result_key="data")

    async def get_job_profiles(self) -> list[dict[str, Any]]:
        """GET /jobProfiles — return all job profiles with pagination."""
        return await self._get_paginated("jobProfiles", result_key="data")

    async def get_locations(self) -> list[dict[str, Any]]:
        """GET /locations — return all locations with pagination."""
        return await self._get_paginated("locations", result_key="data")

    async def get_worker(self, worker_id: str) -> dict[str, Any]:
        """GET /workers/{worker_id} — return a single worker record."""
        url = f"{self._api_base()}/workers/{worker_id}"
        return await self._request("GET", url)
