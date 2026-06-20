"""Low-level async HTTP client for the Google Analytics 4 Data + Admin APIs.

Auth flow:
  Bearer token is passed in from config (obtained externally via OAuth2 authorization code flow).
  All requests: Authorization: Bearer {access_token}

GA4 Data API base:  https://analyticsdata.googleapis.com/v1beta/
GA4 Admin API base: https://analyticsadmin.googleapis.com/v1alpha/
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import aiohttp

# Allow running directly from connector root
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from exceptions import (
    GoogleAnalyticsAuthError,
    GoogleAnalyticsError,
    GoogleAnalyticsNetworkError,
    GoogleAnalyticsNotFoundError,
    GoogleAnalyticsRateLimitError,
)

ADMIN_BASE_URL = "https://analyticsadmin.googleapis.com/v1alpha"
DATA_BASE_URL = "https://analyticsdata.googleapis.com/v1beta"
DEFAULT_TIMEOUT_S = 30.0


class GoogleAnalyticsHTTPClient:
    """Async HTTP client for the GA4 Data API and GA4 Admin API.

    Accepts a pre-obtained Bearer access_token (from OAuth2 authorization code flow).
    All requests include: Authorization: Bearer {access_token}.
    """

    def __init__(
        self,
        access_token: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"Accept": "application/json"},
            )
        return self._session

    def _auth_headers(self) -> dict[str, str]:
        """Return authorization headers for GA4 API requests."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _raise_for_status(
        self,
        status: int,
        path: str = "",
        err_msg: str = "",
        err_code: str = "",
        retry_after: float = 0.0,
    ) -> None:
        """Raise the appropriate typed exception for a given HTTP status code.

        Also checks Google API error.code in the JSON response body.
        """
        if status in (401, 403):
            raise GoogleAnalyticsAuthError(
                f"Authentication failed: {err_msg}", status, err_code
            )
        if status == 404:
            raise GoogleAnalyticsNotFoundError("resource", path or "unknown")
        if status == 429:
            raise GoogleAnalyticsRateLimitError(
                f"Rate limited: {err_msg}", retry_after
            )
        if status >= 500:
            raise GoogleAnalyticsNetworkError(
                f"Google Analytics server error {status}: {err_msg}", status, err_code
            )
        if status >= 400:
            raise GoogleAnalyticsError(
                f"Google Analytics error {status}: {err_msg}", status, err_code
            )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to a GA4 API endpoint.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Full URL to request.
            params: Optional query parameters.
            json: Optional JSON body for POST requests.

        Returns:
            Parsed JSON response body.

        Raises:
            GoogleAnalyticsAuthError: On 401/403.
            GoogleAnalyticsNotFoundError: On 404.
            GoogleAnalyticsRateLimitError: On 429.
            GoogleAnalyticsNetworkError: On 5xx or connectivity failures.
            GoogleAnalyticsError: On other 4xx errors.
        """
        session = self._get_session()
        headers = self._auth_headers()

        try:
            async with session.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
            ) as response:
                if response.status in (200, 201, 202, 204):
                    if response.status == 204 or response.content_length == 0:
                        return {}
                    return await response.json(content_type=None)

                # Error path — attempt to read JSON for Google API error details
                body: dict[str, Any] = {}
                err_text = ""
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    try:
                        err_text = await response.text()
                    except Exception:
                        pass

                # Google API errors are nested under body["error"]
                err_obj = body.get("error", {}) if isinstance(body, dict) else {}
                if isinstance(err_obj, dict):
                    err_msg = err_obj.get(
                        "message",
                        body.get("message", err_text or "Unknown error"),
                    )
                    err_code = str(err_obj.get("code", body.get("code", "")))
                    # Google API may return 200 with error.code for some cases;
                    # also check the actual HTTP status via _raise_for_status
                    google_status = int(err_obj.get("code", response.status) or response.status)
                else:
                    err_msg = err_text or "Unknown error"
                    err_code = ""
                    google_status = response.status

                retry_after = float(response.headers.get("Retry-After", "0"))

                # Use the HTTP status for error classification (Google API mirrors HTTP status in error.code)
                http_status = response.status
                if http_status in (401, 403) or google_status in (401, 403):
                    raise GoogleAnalyticsAuthError(
                        f"Authentication failed: {err_msg}", http_status, err_code
                    )
                if http_status == 404 or google_status == 404:
                    raise GoogleAnalyticsNotFoundError("resource", url)
                if http_status == 429 or google_status == 429:
                    raise GoogleAnalyticsRateLimitError(
                        f"Rate limited: {err_msg}", retry_after
                    )
                if http_status >= 500 or google_status >= 500:
                    raise GoogleAnalyticsNetworkError(
                        f"Google Analytics server error {http_status}: {err_msg}",
                        http_status,
                        err_code,
                    )
                raise GoogleAnalyticsError(
                    f"Google Analytics error {http_status}: {err_msg}",
                    http_status,
                    err_code,
                )

        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise GoogleAnalyticsNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise GoogleAnalyticsNetworkError(f"Network error: {exc}") from exc
        except (
            GoogleAnalyticsError,
            GoogleAnalyticsNetworkError,
            GoogleAnalyticsAuthError,
            GoogleAnalyticsNotFoundError,
            GoogleAnalyticsRateLimitError,
        ):
            raise
        except Exception as exc:
            raise GoogleAnalyticsNetworkError(
                f"Unexpected network error: {exc}"
            ) from exc

    # ── Admin API ─────────────────────────────────────────────────────────────

    async def list_accounts(self) -> dict[str, Any]:
        """GET https://analyticsadmin.googleapis.com/v1alpha/accounts

        Returns dict with 'accounts' list.
        """
        url = f"{ADMIN_BASE_URL}/accounts"
        return await self._request("GET", url)

    async def list_properties(self, account_id: str) -> dict[str, Any]:
        """GET .../properties?filter=parent:accounts/{account_id}

        Returns dict with 'properties' list.
        """
        url = f"{ADMIN_BASE_URL}/properties"
        return await self._request(
            "GET", url, params={"filter": f"parent:accounts/{account_id}"}
        )

    async def get_property(self, property_id: str) -> dict[str, Any]:
        """GET .../properties/{property_id}

        Returns property detail dict.
        """
        url = f"{ADMIN_BASE_URL}/properties/{property_id}"
        return await self._request("GET", url)

    # ── Data API — Reports ────────────────────────────────────────────────────

    async def run_report(
        self,
        property_id: str,
        dimensions: list[str],
        metrics: list[str],
        date_ranges: list[dict[str, str]],
        limit: int = 10000,
        offset: int = 0,
    ) -> dict[str, Any]:
        """POST .../properties/{property_id}:runReport

        Args:
            property_id: GA4 property ID (numeric, e.g. "123456789").
            dimensions: List of dimension names (e.g. ["date", "sessionSource"]).
            metrics: List of metric names (e.g. ["sessions", "activeUsers"]).
            date_ranges: List of date range dicts, e.g. [{"startDate": "30daysAgo", "endDate": "today"}].
            limit: Max rows per page (default 10000).
            offset: Row offset for pagination (default 0).

        Returns:
            Raw GA4 runReport response dict.
        """
        url = f"{DATA_BASE_URL}/properties/{property_id}:runReport"
        body: dict[str, Any] = {
            "dimensions": [{"name": d} for d in dimensions],
            "metrics": [{"name": m} for m in metrics],
            "dateRanges": date_ranges,
            "limit": limit,
            "offset": offset,
        }
        return await self._request("POST", url, json=body)

    async def run_realtime_report(
        self,
        property_id: str,
        dimensions: list[str],
        metrics: list[str],
        limit: int = 100,
    ) -> dict[str, Any]:
        """POST .../properties/{property_id}:runRealtimeReport

        Args:
            property_id: GA4 property ID.
            dimensions: List of dimension names.
            metrics: List of metric names.
            limit: Max rows (default 100).

        Returns:
            Raw GA4 runRealtimeReport response dict.
        """
        url = f"{DATA_BASE_URL}/properties/{property_id}:runRealtimeReport"
        body: dict[str, Any] = {
            "dimensions": [{"name": d} for d in dimensions],
            "metrics": [{"name": m} for m in metrics],
            "limit": limit,
        }
        return await self._request("POST", url, json=body)

    # ── Data API — Metadata ───────────────────────────────────────────────────

    async def list_dimensions(self, property_id: str) -> dict[str, Any]:
        """GET .../properties/{property_id}/metadata

        Returns metadata dict including available dimensions and metrics.
        Alias for get_metadata — kept for semantic clarity.
        """
        url = f"{DATA_BASE_URL}/properties/{property_id}/metadata"
        return await self._request("GET", url)

    async def get_metadata(self, property_id: str) -> dict[str, Any]:
        """GET .../properties/{property_id}/metadata

        Returns full metadata for a GA4 property including all available
        dimensions and metrics with their descriptions.
        """
        url = f"{DATA_BASE_URL}/properties/{property_id}/metadata"
        return await self._request("GET", url)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "GoogleAnalyticsHTTPClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
