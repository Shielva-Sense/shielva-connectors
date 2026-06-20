from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    AmplitudeAuthError,
    AmplitudeError,
    AmplitudeNetworkError,
    AmplitudeNotFoundError,
    AmplitudeRateLimitError,
    AmplitudeServerError,
)

US_BASE_URL = "https://amplitude.com/api/2/"
EU_BASE_URL = "https://analytics.eu.amplitude.com/api/2/"
DEFAULT_TIMEOUT_S = 30.0


class AmplitudeHTTPClient:
    """Low-level async HTTP client for the Amplitude Analytics API v2.

    Uses HTTP Basic Auth (api_key as username, api_secret as password).
    Supports both US and EU data residency regions.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        region: str = "us",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._region = region.lower()
        self._base_url = EU_BASE_URL if self._region == "eu" else US_BASE_URL
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._auth = aiohttp.BasicAuth(api_key, api_secret)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                auth=self._auth,
                timeout=self._timeout,
                headers={"Accept": "application/json"},
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        full_url: str | None = None,
        accept_zip: bool = False,
    ) -> Any:
        """Make an authenticated request to the Amplitude API.

        Returns parsed JSON dict for normal requests, or raw bytes for ZIP exports.
        Raises typed AmplitudeError subclasses on non-2xx responses.
        """
        session = self._get_session()
        url = full_url if full_url else path
        request_headers: dict[str, str] = {}
        if accept_zip:
            request_headers["Accept"] = "application/zip"

        try:
            async with session.request(
                method, url, params=params, headers=request_headers or None
            ) as response:
                if response.status in (200, 201, 202, 204):
                    if response.status == 204 or response.content_length == 0:
                        return {}
                    content_type = response.headers.get("Content-Type", "")
                    if accept_zip or "zip" in content_type:
                        return await response.read()
                    return await response.json(content_type=None)

                # Error path — try to read body for message
                body: dict[str, Any] = {}
                err_text = ""
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    try:
                        err_text = await response.text()
                    except Exception:
                        pass

                err_msg = (
                    body.get("error", body.get("message", err_text or "Unknown Amplitude error"))
                )
                err_code = str(body.get("code", ""))

                if response.status in (401, 403):
                    raise AmplitudeAuthError(
                        f"Authentication failed: {err_msg}", response.status, err_code
                    )
                if response.status == 404:
                    raise AmplitudeNotFoundError("resource", path)
                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise AmplitudeRateLimitError(
                        f"Rate limited: {err_msg}", retry_after
                    )
                if response.status >= 500:
                    raise AmplitudeServerError(
                        f"Amplitude server error {response.status}: {err_msg}",
                        response.status,
                    )
                raise AmplitudeError(
                    f"Amplitude error {response.status}: {err_msg}",
                    response.status,
                    err_code,
                )
        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise AmplitudeNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise AmplitudeNetworkError(f"Network error: {exc}") from exc
        except (AmplitudeError, AmplitudeNetworkError):
            raise
        except Exception as exc:
            raise AmplitudeNetworkError(f"Unexpected network error: {exc}") from exc

    # ── Project settings / Health ─────────────────────────────────────────────

    async def get_project_settings(self) -> dict[str, Any]:
        """GET /settings — project name, org info; used for health check."""
        return await self._request("GET", "settings")

    async def get_taxonomy_categories(self) -> dict[str, Any]:
        """GET /taxonomy/category — used as fallback health check."""
        return await self._request("GET", "taxonomy/category")

    # ── Taxonomy: events & user properties ───────────────────────────────────

    async def list_events(self, chart_id: str | None = None) -> dict[str, Any]:
        """GET /taxonomy/event — list all event types in the project.

        Args:
            chart_id: Optional chart ID to filter events by chart context.
        """
        params: dict[str, Any] | None = None
        if chart_id is not None:
            params = {"chart_id": chart_id}
        return await self._request("GET", "taxonomy/event", params=params)

    async def list_user_properties(self) -> dict[str, Any]:
        """GET /taxonomy/user-property — list all user property definitions."""
        return await self._request("GET", "taxonomy/user-property")

    # ── Charts / Dashboards ───────────────────────────────────────────────────

    async def list_charts(self) -> dict[str, Any]:
        """GET /chart/list — list user-accessible charts and dashboards."""
        return await self._request("GET", "chart/list")

    # ── Event segmentation ────────────────────────────────────────────────────

    async def get_event_segmentation(
        self,
        event: str,
        start_date: str,
        end_date: str,
        *,
        m: str = "totals",
        i: int = 1,
    ) -> dict[str, Any]:
        """GET /events/segmentation — event counts over a date range.

        Args:
            event: Event type name (JSON-encoded Amplitude event).
            start_date: Start date YYYYMMDD.
            end_date:   End date YYYYMMDD.
            m:          Metric — 'totals', 'uniques', 'sessions', 'pct_dau'.
            i:          Interval — 1 (daily), 7 (weekly), 30 (monthly).
        """
        params: dict[str, Any] = {
            "e": event,
            "start": start_date,
            "end": end_date,
            "m": m,
            "i": i,
        }
        return await self._request("GET", "events/segmentation", params=params)

    async def query_event_counts(
        self,
        event: str,
        start: str,
        end: str,
        *,
        i: int = -300000,
        m: str = "uniques",
    ) -> dict[str, Any]:
        """GET /events/segmentation — convenience alias matching spec signature.

        Args:
            event:  Event type name (plain string — encoded to JSON here).
            start:  Start date YYYYMMDD.
            end:    End date YYYYMMDD.
            i:      Interval (default -300000 = auto-select).
            m:      Metric — 'uniques', 'totals', 'sessions', 'pct_dau'.
        """
        import json as _json

        event_param = _json.dumps({"event_type": event})
        params: dict[str, Any] = {
            "e": event_param,
            "start": start,
            "end": end,
            "i": i,
            "m": m,
        }
        return await self._request("GET", "events/segmentation", params=params)

    # ── Cohorts ───────────────────────────────────────────────────────────────

    async def list_cohorts(self) -> dict[str, Any]:
        """GET /cohorts — list all cohorts."""
        return await self._request("GET", "cohorts")

    async def get_cohort_members(self, cohort_id: str) -> dict[str, Any]:
        """GET /cohorts/{cohort_id}/members — list cohort member user IDs."""
        return await self._request("GET", f"cohorts/{cohort_id}/members")

    # ── Active users (DAU/WAU/MAU) ────────────────────────────────────────────

    async def get_active_users(
        self,
        start_date: str,
        end_date: str,
        *,
        m: str = "active",
        i: int = 1,
    ) -> dict[str, Any]:
        """GET /active — DAU / WAU / MAU data.

        Args:
            start_date: Start date YYYYMMDD.
            end_date:   End date YYYYMMDD.
            m:          Metric — 'active' (DAU), 'new', 'returning'.
            i:          Interval — 1 (daily), 7 (weekly), 30 (monthly).
        """
        params: dict[str, Any] = {
            "start": start_date,
            "end": end_date,
            "m": m,
            "i": i,
        }
        return await self._request("GET", "active", params=params)

    # ── User activity ─────────────────────────────────────────────────────────

    async def get_user_activity(self, user_id: str) -> dict[str, Any]:
        """GET /usersearch?user={user_id} — user event stream."""
        return await self._request("GET", "usersearch", params={"user": user_id})

    # ── Export events (ZIP) ───────────────────────────────────────────────────

    async def export_events(self, start: str, end: str) -> bytes:
        """GET https://amplitude.com/api/2/export?start={start}&end={end}

        Returns a ZIP archive (bytes).  The export endpoint always uses the
        US base URL even for EU projects — callers that need EU must pass the
        full URL explicitly.

        Args:
            start: Start timestamp — YYYYMMDDTHH (e.g. '20240101T00').
            end:   End timestamp   — YYYYMMDDTHH (e.g. '20240101T23').
        """
        # Export endpoint lives on amplitude.com regardless of region
        full_url = f"https://amplitude.com/api/2/export"
        result = await self._request(
            "GET",
            "export",
            params={"start": start, "end": end},
            full_url=full_url,
            accept_zip=True,
        )
        return result if isinstance(result, bytes) else b""

    # ── Funnels ───────────────────────────────────────────────────────────────

    async def get_funnel(self, funnel_id: str) -> dict[str, Any]:
        """GET /funnels?funnel_id={funnel_id} — retrieve a saved funnel by ID.

        Args:
            funnel_id: The Amplitude funnel ID string.
        """
        return await self._request("GET", "funnels", params={"funnel_id": funnel_id})

    async def get_funnel_by_events(
        self,
        events: list[dict[str, Any]],
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """GET /funnels — funnel conversion between a sequence of events (ad-hoc).

        Args:
            events:     List of Amplitude event dicts, e.g. [{"event_type": "PageView"}].
            start_date: YYYYMMDD.
            end_date:   YYYYMMDD.
        """
        import json

        params: dict[str, Any] = {
            "e": json.dumps(events),
            "start": start_date,
            "end": end_date,
        }
        return await self._request("GET", "funnels", params=params)

    # ── Retention ─────────────────────────────────────────────────────────────

    async def get_retention(
        self,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """GET /retention — retention data for a date range.

        Args:
            start_date: YYYYMMDD.
            end_date:   YYYYMMDD.
        """
        params: dict[str, Any] = {
            "start": start_date,
            "end": end_date,
        }
        return await self._request("GET", "retention", params=params)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> AmplitudeHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
