from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    BambooHRAuthError,
    BambooHRError,
    BambooHRNetworkError,
    BambooHRNotFoundError,
    BambooHRRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
BAMBOOHR_API_VERSION: str = "v1"


def _build_base_url(company_domain: str) -> str:
    return f"https://api.bamboohr.com/api/gateway.php/{company_domain}/{BAMBOOHR_API_VERSION}"


class BambooHRHTTPClient:
    """Low-level async HTTP client for the BambooHR REST API v1.

    Uses HTTP Basic Auth with the API key as the username and the literal
    string "x" as the password, per BambooHR's documented auth scheme.
    All requests include Accept: application/json.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(
        self,
        method: str,
        url: str,
        api_key: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        auth = aiohttp.BasicAuth(api_key, "x")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    auth=auth,
                    params=params,
                    json=json_body,
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise BambooHRNetworkError(f"Network error: {exc}") from exc
        except (
            BambooHRError,
            BambooHRAuthError,
            BambooHRRateLimitError,
            BambooHRNotFoundError,
            BambooHRNetworkError,
        ):
            raise
        except Exception as exc:
            raise BambooHRNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        # Attempt to read error body
        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_msg: str = (
            body.get("error", "")
            or body.get("message", "")
            or body.get("description", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise BambooHRAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise BambooHRNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise BambooHRRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise BambooHRNetworkError(
                f"BambooHR server error {status}: {err_msg}",
                status_code=status,
            )
        raise BambooHRError(
            f"BambooHR error {status}: {err_msg}", status_code=status
        )

    # ── Employee Directory ────────────────────────────────────────────────────

    async def get_employee_directory(
        self, company_domain: str, api_key: str
    ) -> dict[str, Any]:
        """GET /employees/directory — return all employees in the directory."""
        url = f"{_build_base_url(company_domain)}/employees/directory"
        return await self._request("GET", url, api_key)

    # ── Individual Employee ───────────────────────────────────────────────────

    async def get_employee(
        self,
        company_domain: str,
        api_key: str,
        employee_id: str | int,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """GET /employees/{id} — return a single employee's fields."""
        url = f"{_build_base_url(company_domain)}/employees/{employee_id}"
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        return await self._request("GET", url, api_key, params=params or None)

    # ── Time-Off Requests ─────────────────────────────────────────────────────

    async def list_time_off_requests(
        self,
        company_domain: str,
        api_key: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """GET /time_off/requests — return time-off requests in a date range.

        BambooHR returns a JSON array for this endpoint.
        """
        url = f"{_build_base_url(company_domain)}/time_off/requests"
        params: dict[str, Any] = {
            "start": start_date,
            "end": end_date,
        }
        # This endpoint returns a list, so we wrap in a dict for uniform handling
        result = await self._request("GET", url, api_key, params=params)
        return result if isinstance(result, list) else result.get("requests", [])  # type: ignore[return-value]

    # ── Custom Reports ────────────────────────────────────────────────────────

    async def list_custom_reports(
        self,
        company_domain: str,
        api_key: str,
        report_id: str | int,
    ) -> dict[str, Any]:
        """POST /reports/{report_id} — run a custom saved report."""
        url = f"{_build_base_url(company_domain)}/reports/{report_id}"
        return await self._request(
            "POST",
            url,
            api_key,
            json_body={"format": "JSON"},
        )

    # ── Company Info ──────────────────────────────────────────────────────────

    async def get_company_info(
        self, company_domain: str, api_key: str
    ) -> dict[str, Any]:
        """GET /company/info — return company metadata if available."""
        url = f"{_build_base_url(company_domain)}/company/info"
        return await self._request("GET", url, api_key)
