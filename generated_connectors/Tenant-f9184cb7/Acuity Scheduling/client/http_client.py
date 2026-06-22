from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    AcuityAuthError,
    AcuityError,
    AcuityNetworkError,
    AcuityNotFoundError,
    AcuityRateLimitError,
)

ACUITY_API_BASE: str = "https://acuityscheduling.com"
DEFAULT_TIMEOUT_S: float = 30.0


class AcuityHTTPClient:
    """Low-level async HTTP client for the Acuity Scheduling REST API v1.

    All requests use HTTP BasicAuth: user_id as username, api_key as password.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(
        self,
        method: str,
        path: str,
        user_id: str,
        api_key: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{ACUITY_API_BASE}{path}"
        auth = aiohttp.BasicAuth(user_id, api_key)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, auth=auth, params=params
                ) as response:
                    return await self._raise_for_status(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise AcuityNetworkError(f"Network error: {exc}") from exc
        except (
            AcuityError,
            AcuityAuthError,
            AcuityRateLimitError,
            AcuityNotFoundError,
            AcuityNetworkError,
        ):
            raise
        except Exception as exc:
            raise AcuityNetworkError(f"Unexpected network error: {exc}") from exc

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse
    ) -> Any:
        status = response.status

        if status == 200:
            return await response.json()

        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        err_msg: str = (
            body.get("message", "")
            or body.get("error", "")
            or body.get("title", "")
            or f"HTTP {status}"
        )
        if isinstance(err_msg, list):
            err_msg = "; ".join(str(e) for e in err_msg)

        if status in (401, 403):
            raise AcuityAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise AcuityNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise AcuityRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise AcuityNetworkError(
                f"Acuity server error {status}: {err_msg}",
                status_code=status,
            )
        raise AcuityError(
            f"Acuity error {status}: {err_msg}", status_code=status
        )

    # ── Account info ─────────────────────────────────────────────────────────

    async def get_me(self, user_id: str, api_key: str) -> dict[str, Any]:
        """GET /api/v1/me — return the authenticated account info."""
        return await self._request("GET", "/api/v1/me", user_id, api_key)

    # ── Appointments ─────────────────────────────────────────────────────────

    async def get_appointments(
        self,
        user_id: str,
        api_key: str,
        page: int = 1,
        max: int = 25,
        min_date: str | None = None,
        max_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/appointments — list appointments with optional date filters."""
        params: dict[str, Any] = {"page": page, "max": max}
        if min_date is not None:
            params["minDate"] = min_date
        if max_date is not None:
            params["maxDate"] = max_date
        return await self._request("GET", "/api/v1/appointments", user_id, api_key, params=params)

    # ── Appointment types ─────────────────────────────────────────────────────

    async def get_appointment_types(
        self,
        user_id: str,
        api_key: str,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/appointment-types — list all appointment types."""
        return await self._request("GET", "/api/v1/appointment-types", user_id, api_key)

    # ── Calendars ─────────────────────────────────────────────────────────────

    async def get_calendars(
        self,
        user_id: str,
        api_key: str,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/calendars — list all calendars."""
        return await self._request("GET", "/api/v1/calendars", user_id, api_key)

    # ── Clients ───────────────────────────────────────────────────────────────

    async def get_clients(
        self,
        user_id: str,
        api_key: str,
        page: int = 1,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/clients — list clients with optional search."""
        params: dict[str, Any] = {"page": page}
        if search is not None:
            params["search"] = search
        return await self._request("GET", "/api/v1/clients", user_id, api_key, params=params)

    # ── Availability ──────────────────────────────────────────────────────────

    async def get_availability(
        self,
        user_id: str,
        api_key: str,
        appointment_type_id: int,
        month: str,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/availability/times — get available times for an appointment type."""
        params: dict[str, Any] = {
            "appointmentTypeID": appointment_type_id,
            "month": month,
        }
        return await self._request(
            "GET", "/api/v1/availability/times", user_id, api_key, params=params
        )

    # ── Blocked times ─────────────────────────────────────────────────────────

    async def get_blocked_times(
        self,
        user_id: str,
        api_key: str,
        calendarID: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/blocks — list blocked time slots."""
        params: dict[str, Any] = {}
        if calendarID is not None:
            params["calendarID"] = calendarID
        return await self._request("GET", "/api/v1/blocks", user_id, api_key, params=params)
