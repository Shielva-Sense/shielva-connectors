from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    PagerDutyAuthError,
    PagerDutyError,
    PagerDutyNetworkError,
    PagerDutyNotFoundError,
    PagerDutyRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
PAGERDUTY_API_BASE: str = "https://api.pagerduty.com"
PAGERDUTY_ACCEPT_HEADER: str = "application/vnd.pagerduty+json;version=2"


class PagerDutyHTTPClient:
    """Low-level async HTTP client for the PagerDuty REST API v2.

    Auth header format: ``Authorization: Token token={api_key}``
    Required API version header: ``Accept: application/vnd.pagerduty+json;version=2``
    Pagination: offset/limit/more (boolean) in response body.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._api_key: str = cfg.get("api_key", "")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token token={self._api_key}",
            "Accept": PAGERDUTY_ACCEPT_HEADER,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{PAGERDUTY_API_BASE}{path}"
        headers = self._make_headers()
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise PagerDutyNetworkError(f"Network error: {exc}") from exc
        except (
            PagerDutyError,
            PagerDutyAuthError,
            PagerDutyRateLimitError,
            PagerDutyNotFoundError,
            PagerDutyNetworkError,
        ):
            raise
        except Exception as exc:
            raise PagerDutyNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            return await response.json()

        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        self._raise_for_status(status, body)
        # Unreachable — satisfies type checker
        raise PagerDutyError(f"HTTP {status}", status_code=status)

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map HTTP status codes to typed PagerDuty exceptions."""
        error_obj = body.get("error", {})
        if isinstance(error_obj, dict):
            err_msg: str = (
                error_obj.get("message", "")
                or error_obj.get("code", "")
                or f"HTTP {status}"
            )
        else:
            err_msg = str(error_obj) if error_obj else f"HTTP {status}"

        if status in (401, 403):
            raise PagerDutyAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise PagerDutyNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(body.get("retry_after", 0) or 0)
            raise PagerDutyRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise PagerDutyNetworkError(
                f"PagerDuty server error {status}: {err_msg}",
                status_code=status,
            )
        raise PagerDutyError(
            f"PagerDuty error {status}: {err_msg}", status_code=status
        )

    # ── Abilities (health probe) ───────────────────────────────────────────────

    async def get_abilities(self) -> dict[str, Any]:
        """GET /abilities — verify API key and return account abilities."""
        return await self._request("GET", "/abilities")

    # ── Incidents ─────────────────────────────────────────────────────────────

    async def list_incidents(
        self,
        statuses: list[str] | None = None,
        urgencies: list[str] | None = None,
        time_zone: str = "UTC",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /incidents — paginated incident listing.

        Supports filtering by statuses[] and urgencies[] query params.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "time_zone": time_zone,
        }
        if statuses:
            params["statuses[]"] = statuses
        if urgencies:
            params["urgencies[]"] = urgencies
        return await self._request("GET", "/incidents", params=params)

    async def get_incident(self, incident_id: str) -> dict[str, Any]:
        """GET /incidents/{id} — fetch a single incident."""
        return await self._request("GET", f"/incidents/{incident_id}")

    async def list_incident_alerts(self, incident_id: str) -> dict[str, Any]:
        """GET /incidents/{id}/alerts — list alerts for an incident."""
        return await self._request("GET", f"/incidents/{incident_id}/alerts")

    # ── Services ──────────────────────────────────────────────────────────────

    async def list_services(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """GET /services — paginated service listing."""
        return await self._request(
            "GET", "/services", params={"limit": limit, "offset": offset}
        )

    async def get_service(self, service_id: str) -> dict[str, Any]:
        """GET /services/{id} — fetch a single service."""
        return await self._request("GET", f"/services/{service_id}")

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """GET /users — paginated user listing."""
        return await self._request(
            "GET", "/users", params={"limit": limit, "offset": offset}
        )

    # ── Escalation Policies ───────────────────────────────────────────────────

    async def list_escalation_policies(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """GET /escalation_policies — paginated escalation policy listing."""
        return await self._request(
            "GET",
            "/escalation_policies",
            params={"limit": limit, "offset": offset},
        )

    # ── Schedules ─────────────────────────────────────────────────────────────

    async def list_schedules(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """GET /schedules — paginated schedule listing."""
        return await self._request(
            "GET", "/schedules", params={"limit": limit, "offset": offset}
        )
