"""Low-level async HTTP client for the Adobe Analytics 2.0 API.

Auth flow:
1. POST https://ims-na1.adobelogin.com/ims/token/v3 → access_token (client_credentials grant)
2. All subsequent requests: Authorization: Bearer {access_token}, x-api-key: {client_id}
"""
from __future__ import annotations

import time
from typing import Any

import aiohttp

from exceptions import (
    AdobeAnalyticsAuthError,
    AdobeAnalyticsError,
    AdobeAnalyticsNetworkError,
    AdobeAnalyticsNotFoundError,
    AdobeAnalyticsRateLimitError,
)

IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
ANALYTICS_BASE_URL = "https://analytics.adobe.io"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_SCOPES = (
    "openid,AdobeID,read_organizations,"
    "additional_info.projectedProductContext,"
    "additional_info.job_function"
)


class AdobeAnalyticsHTTPClient:
    """Async HTTP client for Adobe Analytics 2.0 API.

    Handles OAuth2 client_credentials token acquisition and refresh.
    All analytics calls include:
      Authorization: Bearer {access_token}
      x-api-key: {client_id}
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        company_id: str,
        scopes: str = DEFAULT_SCOPES,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._company_id = company_id
        self._scopes = scopes
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._access_token: str = ""
        self._token_expires_at: float = 0.0
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"Accept": "application/json"},
            )
        return self._session

    def _is_token_valid(self) -> bool:
        """Return True if the stored access token is still valid (with 30s buffer)."""
        return bool(self._access_token) and time.monotonic() < self._token_expires_at - 30

    async def get_token(self) -> str:
        """Acquire an OAuth2 access token via client_credentials grant.

        POSTs to IMS_TOKEN_URL and stores token + expiry.
        Returns the access_token string.
        Raises AdobeAnalyticsAuthError on 401/403.
        Raises AdobeAnalyticsNetworkError on connectivity failures.
        """
        if self._is_token_valid():
            return self._access_token

        session = self._get_session()
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scopes,
        }
        try:
            async with session.post(IMS_TOKEN_URL, data=payload) as response:
                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                if response.status in (200, 201):
                    token: str = body.get("access_token", "")
                    expires_in: int = int(body.get("expires_in", 3600))
                    if not token:
                        raise AdobeAnalyticsAuthError(
                            "IMS token response missing access_token",
                            response.status,
                        )
                    self._access_token = token
                    self._token_expires_at = time.monotonic() + expires_in
                    return self._access_token

                err_msg = body.get("error_description", body.get("error", "IMS auth failed"))
                if response.status in (400, 401, 403):
                    raise AdobeAnalyticsAuthError(
                        f"Authentication failed: {err_msg}",
                        response.status,
                        body.get("error", ""),
                    )
                raise AdobeAnalyticsError(
                    f"Token endpoint error {response.status}: {err_msg}",
                    response.status,
                )
        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise AdobeAnalyticsNetworkError(f"Token request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise AdobeAnalyticsNetworkError(f"Network error during token fetch: {exc}") from exc
        except (AdobeAnalyticsError, AdobeAnalyticsNetworkError):
            raise
        except Exception as exc:
            raise AdobeAnalyticsNetworkError(
                f"Unexpected error during token fetch: {exc}"
            ) from exc

    async def _auth_headers(self) -> dict[str, str]:
        """Return the auth headers required by the Analytics 2.0 API."""
        token = await self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "x-api-key": self._client_id,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to the Adobe Analytics 2.0 API.

        path should be relative to the company base, e.g. '/reportsuites'.
        Full URL: {ANALYTICS_BASE_URL}/api/{company_id}{path}
        """
        session = self._get_session()
        headers = await self._auth_headers()
        url = f"{ANALYTICS_BASE_URL}/api/{self._company_id}{path}"

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

                # Error path — read body for message
                body: dict[str, Any] = {}
                err_text = ""
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    try:
                        err_text = await response.text()
                    except Exception:
                        pass

                err_msg = body.get(
                    "message",
                    body.get("error_description", body.get("error", err_text or "Unknown error")),
                )
                err_code = str(body.get("errorCode", body.get("code", "")))

                if response.status in (401, 403):
                    raise AdobeAnalyticsAuthError(
                        f"Authentication failed: {err_msg}",
                        response.status,
                        err_code,
                    )
                if response.status == 404:
                    raise AdobeAnalyticsNotFoundError("resource", path)
                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise AdobeAnalyticsRateLimitError(
                        f"Rate limited: {err_msg}", retry_after
                    )
                if response.status >= 500:
                    raise AdobeAnalyticsError(
                        f"Adobe Analytics server error {response.status}: {err_msg}",
                        response.status,
                        err_code,
                    )
                raise AdobeAnalyticsError(
                    f"Adobe Analytics error {response.status}: {err_msg}",
                    response.status,
                    err_code,
                )
        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise AdobeAnalyticsNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise AdobeAnalyticsNetworkError(f"Network error: {exc}") from exc
        except (AdobeAnalyticsError, AdobeAnalyticsNetworkError):
            raise
        except Exception as exc:
            raise AdobeAnalyticsNetworkError(
                f"Unexpected network error: {exc}"
            ) from exc

    def _raise_for_status(
        self,
        status: int,
        path: str = "",
        err_msg: str = "",
        err_code: str = "",
        retry_after: float = 0.0,
    ) -> None:
        """Raise the appropriate typed exception for a given HTTP status code.

        Used by tests and internal _request to centralise error mapping.
        """
        if status in (401, 403):
            raise AdobeAnalyticsAuthError(
                f"Authentication failed: {err_msg}", status, err_code
            )
        if status == 404:
            raise AdobeAnalyticsNotFoundError("resource", path or "unknown")
        if status == 429:
            raise AdobeAnalyticsRateLimitError(
                f"Rate limited: {err_msg}", retry_after
            )
        if status >= 500:
            raise AdobeAnalyticsError(
                f"Adobe Analytics server error {status}: {err_msg}", status, err_code
            )
        if status >= 400:
            raise AdobeAnalyticsError(
                f"Adobe Analytics error {status}: {err_msg}", status, err_code
            )

    # ── Report Suites ─────────────────────────────────────────────────────────

    async def get_report_suites(self) -> dict[str, Any]:
        """GET /api/{company_id}/reportsuites — list all report suites."""
        return await self._request("GET", "/reportsuites")

    # ── Dimensions ────────────────────────────────────────────────────────────

    async def get_dimensions(self, report_suite_id: str) -> dict[str, Any]:
        """GET /api/{company_id}/dimensions?rsid={report_suite_id}."""
        return await self._request(
            "GET", "/dimensions", params={"rsid": report_suite_id}
        )

    # ── Metrics ───────────────────────────────────────────────────────────────

    async def get_metrics(self, report_suite_id: str) -> dict[str, Any]:
        """GET /api/{company_id}/metrics?rsid={report_suite_id}."""
        return await self._request(
            "GET", "/metrics", params={"rsid": report_suite_id}
        )

    # ── Reports ───────────────────────────────────────────────────────────────

    async def run_report(
        self, report_suite_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST /api/{company_id}/reports — run a ranked or trended report.

        Args:
            report_suite_id: The report suite RSID.
            body: Adobe Analytics report request body dict.

        Returns:
            Raw JSON report response.
        """
        report_body = dict(body)
        report_body.setdefault("rsid", report_suite_id)
        return await self._request("POST", "/reports", json=report_body)

    # ── Segments ──────────────────────────────────────────────────────────────

    async def get_segments(self, report_suite_id: str) -> dict[str, Any]:
        """GET /api/{company_id}/segments — list segments for a report suite."""
        return await self._request(
            "GET", "/segments", params={"rsid": report_suite_id}
        )

    # ── Calculated Metrics ────────────────────────────────────────────────────

    async def get_calculated_metrics(self) -> dict[str, Any]:
        """GET /api/{company_id}/calculatedmetrics — list all calculated metrics."""
        return await self._request("GET", "/calculatedmetrics")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> AdobeAnalyticsHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
