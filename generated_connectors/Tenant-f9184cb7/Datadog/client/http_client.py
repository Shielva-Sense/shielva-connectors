from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    DatadogAuthError,
    DatadogError,
    DatadogNetworkError,
    DatadogNotFoundError,
    DatadogRateLimitError,
)

DEFAULT_SITE = "datadoghq.com"
DEFAULT_TIMEOUT_S = 30.0


class DatadogHTTPClient:
    """Low-level async HTTP client for the Datadog API v1/v2.

    Sends both DD-API-KEY and DD-APPLICATION-KEY headers on every request.
    Base URL is dynamically constructed from the configured site:
      https://api.{site}/api/
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._api_key: str = cfg.get("api_key", "")
        self._app_key: str = cfg.get("app_key", "")
        self._site: str = cfg.get("site", DEFAULT_SITE) or DEFAULT_SITE
        self._base_url: str = f"https://api.{self._site}/api/"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "DD-API-KEY": self._api_key,
                    "DD-APPLICATION-KEY": self._app_key,
                },
            )
        return self._session

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map non-2xx HTTP status codes to typed Datadog exceptions."""
        err_msg = body.get("errors", body.get("message", f"Datadog error {status}"))
        if isinstance(err_msg, list):
            err_msg = "; ".join(str(e) for e in err_msg)
        err_msg = str(err_msg)

        if status in (401, 403):
            raise DatadogAuthError(
                f"Authentication failed: {err_msg}", status_code=status, code="auth_error"
            )
        if status == 404:
            raise DatadogNotFoundError("resource", "unknown")
        if status == 429:
            raise DatadogRateLimitError(f"Rate limited: {err_msg}")
        if status >= 500:
            raise DatadogNetworkError(
                f"Datadog server error {status}: {err_msg}", status_code=status
            )
        raise DatadogError(f"Datadog error {status}: {err_msg}", status_code=status)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to the Datadog API.

        Returns parsed JSON. Raises typed DatadogError subclasses on non-2xx responses.
        """
        session = self._get_session()
        try:
            async with session.request(method, path, params=params, json=json) as response:
                if response.status in (200, 201, 202, 204):
                    if response.status == 204 or response.content_length == 0:
                        return {}
                    return await response.json(content_type=None)

                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    try:
                        text = await response.text()
                        body = {"message": text}
                    except Exception:
                        pass

                self._raise_for_status(response.status, body)
        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise DatadogNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise DatadogNetworkError(f"Network error: {exc}") from exc
        except (DatadogError, DatadogNetworkError):
            raise
        except Exception as exc:
            raise DatadogNetworkError(f"Unexpected network error: {exc}") from exc

    # ── Authentication / Health ───────────────────────────────────────────────

    async def validate(self) -> dict[str, Any]:
        """GET /v1/validate — verify both API key and application key."""
        return await self._request("GET", "v1/validate")

    # ── Monitors ──────────────────────────────────────────────────────────────

    async def get_monitors(
        self,
        page: int = 0,
        page_size: int = 100,
        tags: list[str] | None = None,
    ) -> list[Any]:
        """GET /v1/monitor — list all monitors with pagination.

        Args:
            page:      Page number (0-indexed).
            page_size: Number of results per page (max 1000).
            tags:      Optional list of tag filters (comma-joined as ?tags=<t1,t2>).

        Returns:
            List of monitor objects from Datadog.
        """
        params: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
        }
        if tags:
            params["tags"] = ",".join(tags)
        result = await self._request("GET", "v1/monitor", params=params)
        if isinstance(result, list):
            return result
        return result.get("monitors", []) if isinstance(result, dict) else []

    async def get_monitor(self, monitor_id: int) -> dict[str, Any]:
        """GET /v1/monitor/{monitor_id} — retrieve a single monitor by ID."""
        result = await self._request("GET", f"v1/monitor/{monitor_id}")
        return result if isinstance(result, dict) else {}

    # ── Dashboards ────────────────────────────────────────────────────────────

    async def get_dashboards(self, count: int = 100, start: int = 0) -> dict[str, Any]:
        """GET /v1/dashboard — list all dashboards.

        Args:
            count: Number of dashboards per page.
            start: Offset for pagination.

        Returns:
            Dict with 'dashboards' list.
        """
        params: dict[str, Any] = {"count": count, "start": start}
        result = await self._request("GET", "v1/dashboard", params=params)
        return result if isinstance(result, dict) else {}

    async def get_dashboard(self, dashboard_id: str) -> dict[str, Any]:
        """GET /v1/dashboard/{dashboard_id} — retrieve a single dashboard by ID."""
        result = await self._request("GET", f"v1/dashboard/{dashboard_id}")
        return result if isinstance(result, dict) else {}

    # ── Hosts ─────────────────────────────────────────────────────────────────

    async def get_hosts(self, count: int = 100, start: int = 0) -> dict[str, Any]:
        """GET /v1/hosts — list hosts with pagination.

        Args:
            count: Number of hosts to retrieve (max 1000).
            start: Offset for pagination.

        Returns:
            Dict with 'host_list' and 'total_returned' keys.
        """
        params: dict[str, Any] = {
            "count": count,
            "start": start,
        }
        result = await self._request("GET", "v1/hosts", params=params)
        return result if isinstance(result, dict) else {}

    # ── Events ────────────────────────────────────────────────────────────────

    async def get_events(
        self,
        start: int,
        end: int,
        page: int = 0,
    ) -> dict[str, Any]:
        """GET /v1/events — retrieve events between two epoch timestamps.

        Args:
            start: Start epoch timestamp (seconds).
            end:   End epoch timestamp (seconds).
            page:  Page number for pagination.

        Returns:
            Dict with 'events' list and metadata.
        """
        params: dict[str, Any] = {
            "start": start,
            "end": end,
            "page": page,
        }
        result = await self._request("GET", "v1/events", params=params)
        return result if isinstance(result, dict) else {}

    # ── Logs (v2) ─────────────────────────────────────────────────────────────

    async def list_logs(
        self,
        query: str,
        from_ts: int,
        to_ts: int,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v2/logs/events/search — search log events.

        Args:
            query:   Datadog log search query string.
            from_ts: Start epoch timestamp (seconds).
            to_ts:   End epoch timestamp (seconds).
            limit:   Maximum number of results (max 1000).
            cursor:  Pagination cursor from meta.page.after (v2 cursor-based pagination).

        Returns:
            Dict with 'data' list of log events and 'meta' with pagination info.
        """
        body: dict[str, Any] = {
            "filter": {
                "query": query,
                "from": str(from_ts),
                "to": str(to_ts),
            },
            "page": {"limit": limit},
        }
        if cursor:
            body["page"]["cursor"] = cursor
        result = await self._request("POST", "v2/logs/events/search", json=body)
        return result if isinstance(result, dict) else {}

    # ── Metrics ───────────────────────────────────────────────────────────────

    async def get_metrics_list(self, q: str) -> dict[str, Any]:
        """GET /v1/metrics?q={q} — search available metrics by name query.

        Args:
            q: Metric name search query (e.g. 'system' or 'aws.ec2').

        Returns:
            Dict with 'metrics' list of metric name strings.
        """
        params: dict[str, Any] = {"q": q}
        result = await self._request("GET", "v1/metrics", params=params)
        return result if isinstance(result, dict) else {}

    # ── Service Checks ────────────────────────────────────────────────────────

    async def list_service_checks(self) -> list[Any]:
        """GET /v1/check_run — list all service check results.

        Returns:
            List of service check result objects.
        """
        result = await self._request("GET", "v1/check_run")
        if isinstance(result, list):
            return result
        return result.get("checks", []) if isinstance(result, dict) else []

    # ── Incidents ─────────────────────────────────────────────────────────────

    async def get_incidents(
        self,
        page_size: int = 10,
        page_offset: int = 0,
    ) -> dict[str, Any]:
        """GET /v2/incidents — list all incidents (Datadog API v2).

        Args:
            page_size:   Number of incidents per page.
            page_offset: Offset for pagination.

        Returns:
            Dict with 'data' list of incident objects.
        """
        params: dict[str, Any] = {
            "page[size]": page_size,
            "page[offset]": page_offset,
        }
        result = await self._request("GET", "v2/incidents", params=params)
        return result if isinstance(result, dict) else {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> DatadogHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
