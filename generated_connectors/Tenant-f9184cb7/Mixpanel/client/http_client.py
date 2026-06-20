"""Mixpanel HTTP client — async, aiohttp-based, HTTP Basic Auth."""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator

import aiohttp

from exceptions import (
    MixpanelAuthError,
    MixpanelError,
    MixpanelNetworkError,
    MixpanelNotFoundError,
    MixpanelRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 60.0

# US base URLs
API_BASE_US: str = "https://mixpanel.com/api/"
DATA_API_BASE_US: str = "https://data.mixpanel.com/api/2.0/"

# EU base URLs
API_BASE_EU: str = "https://eu.mixpanel.com/api/"
DATA_API_BASE_EU: str = "https://eu.data.mixpanel.com/api/2.0/"


class MixpanelHTTPClient:
    """Low-level async HTTP client for the Mixpanel API.

    Authentication: HTTP Basic Auth where
        username = service account username
        password = service account secret

    EU data-residency is supported by passing region="EU".
    """

    def __init__(
        self,
        username: str = "",
        secret: str = "",
        project_id: str = "",
        region: str = "US",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._username = username
        self._secret = secret
        self._project_id = project_id
        self._region = (region or "US").upper()
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # ── Region helpers ────────────────────────────────────────────────────────

    def _api_base(self) -> str:
        return API_BASE_EU if self._region == "EU" else API_BASE_US

    def _data_api_base(self) -> str:
        return DATA_API_BASE_EU if self._region == "EU" else DATA_API_BASE_US

    def _auth(self) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(self._username, self._secret)

    # ── Status / error mapping ────────────────────────────────────────────────

    def _raise_for_status(
        self,
        status: int,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Map HTTP status codes to typed exceptions."""
        if status == 200:
            return

        err_msg: str = (
            body.get("error", "")
            or body.get("message", "")
            or body.get("description", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise MixpanelAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 400:
            raise MixpanelError(
                f"Bad request: {err_msg}",
                status_code=400,
                code="bad_request",
            )
        if status == 404:
            raise MixpanelNotFoundError("resource", err_msg or str(status))
        if status == 429:
            retry_after: float = 0.0
            if headers:
                try:
                    retry_after = float(headers.get("Retry-After", "0"))
                except ValueError:
                    retry_after = 0.0
            raise MixpanelRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise MixpanelNetworkError(
                f"Mixpanel server error {status}: {err_msg}",
                status_code=status,
                code="server_error",
            )
        raise MixpanelError(
            f"Mixpanel error {status}: {err_msg}",
            status_code=status,
        )

    # ── Core request ──────────────────────────────────────────────────────────

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue a GET request and return parsed JSON."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url,
                    params=params,
                    auth=self._auth(),
                    headers={"Accept": "application/json"},
                ) as response:
                    body: dict[str, Any] = {}
                    try:
                        body = await response.json(content_type=None)
                    except Exception:
                        pass
                    headers_dict = dict(response.headers)
                    self._raise_for_status(response.status, body, headers_dict)
                    return body
        except (MixpanelError,):
            raise
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise MixpanelNetworkError(f"Network error: {exc}") from exc
        except Exception as exc:
            raise MixpanelNetworkError(f"Unexpected network error: {exc}") from exc

    # ── User / project probe ──────────────────────────────────────────────────

    async def get_projects(self) -> dict[str, Any]:
        """GET https://mixpanel.com/api/app/me/ — returns current user and projects."""
        url = "https://mixpanel.com/api/app/me/"
        return await self._get(url)

    # ── Event export (NDJSON) ─────────────────────────────────────────────────

    async def query_events(
        self,
        event_names: list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 100,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """GET .../export/ — NDJSON (newline-delimited JSON) raw event export.

        Returns an async generator that yields one parsed dict per event line.
        """
        base = self._data_api_base()
        url = f"{base}export/"
        params: dict[str, Any] = {
            "project_id": self._project_id,
        }
        if event_names:
            params["event"] = json.dumps(event_names)
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        if limit:
            params["limit"] = limit

        return self._stream_ndjson(url, params)

    async def _stream_ndjson(
        self,
        url: str,
        params: dict[str, Any],
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Internal: stream a NDJSON response line by line."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url,
                    params=params,
                    auth=self._auth(),
                    headers={"Accept": "application/x-ndjson"},
                ) as response:
                    if response.status in (401, 403):
                        raise MixpanelAuthError(
                            f"Authentication failed ({response.status})",
                            status_code=response.status,
                            code="auth_error",
                        )
                    if response.status == 429:
                        retry_after_val: float = 0.0
                        try:
                            retry_after_val = float(
                                response.headers.get("Retry-After", "0")
                            )
                        except ValueError:
                            retry_after_val = 0.0
                        raise MixpanelRateLimitError(
                            "Rate limited", retry_after=retry_after_val
                        )
                    if response.status >= 500:
                        raise MixpanelNetworkError(
                            f"Mixpanel server error {response.status}",
                            status_code=response.status,
                        )
                    if response.status == 400:
                        raise MixpanelError(
                            f"Bad request (HTTP 400)",
                            status_code=400,
                            code="bad_request",
                        )
                    if response.status != 200:
                        raise MixpanelError(
                            f"Unexpected status {response.status}",
                            status_code=response.status,
                        )
                    async for raw_line in response.content:
                        line = raw_line.decode("utf-8").strip()
                        if line:
                            try:
                                yield json.loads(line)
                            except json.JSONDecodeError:
                                continue
        except (MixpanelError,):
            raise
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise MixpanelNetworkError(f"Network error during export: {exc}") from exc
        except Exception as exc:
            raise MixpanelNetworkError(
                f"Unexpected error during export: {exc}"
            ) from exc

    # ── Funnels ───────────────────────────────────────────────────────────────

    async def query_funnels(
        self,
        funnel_id: int | str,
        from_date: str,
        to_date: str,
        unit: str = "day",
    ) -> dict[str, Any]:
        """GET .../2.0/funnels/ — funnel conversion data."""
        base = self._api_base()
        url = f"{base}2.0/funnels/"
        params: dict[str, Any] = {
            "project_id": self._project_id,
            "funnel_id": funnel_id,
            "from_date": from_date,
            "to_date": to_date,
            "unit": unit,
        }
        return await self._get(url, params)

    async def list_funnels(self) -> dict[str, Any]:
        """GET .../2.0/funnels/list/ — list all saved funnels."""
        base = self._api_base()
        url = f"{base}2.0/funnels/list/"
        params: dict[str, Any] = {"project_id": self._project_id}
        return await self._get(url, params)

    # ── Retention ────────────────────────────────────────────────────────────

    async def query_retention(
        self,
        from_date: str,
        to_date: str,
        retention_type: str = "birth",
        interval: int = 1,
        unit: str = "week",
    ) -> dict[str, Any]:
        """GET .../2.0/retention/ — user retention data."""
        base = self._api_base()
        url = f"{base}2.0/retention/"
        params: dict[str, Any] = {
            "project_id": self._project_id,
            "from_date": from_date,
            "to_date": to_date,
            "retention_type": retention_type,
            "interval": interval,
            "unit": unit,
        }
        return await self._get(url, params)

    # ── Segmentation ─────────────────────────────────────────────────────────

    async def query_segmentation(
        self,
        event: str,
        from_date: str,
        to_date: str,
        type: str = "general",
        unit: str = "day",
    ) -> dict[str, Any]:
        """GET .../2.0/segmentation/ — event segmentation / aggregated counts."""
        base = self._api_base()
        url = f"{base}2.0/segmentation/"
        params: dict[str, Any] = {
            "project_id": self._project_id,
            "event": event,
            "from_date": from_date,
            "to_date": to_date,
            "type": type,
            "unit": unit,
        }
        return await self._get(url, params)

    # ── Event properties ─────────────────────────────────────────────────────

    async def get_event_properties(self, event_name: str) -> dict[str, Any]:
        """GET .../2.0/events/properties/ — properties for a named event."""
        base = self._api_base()
        url = f"{base}2.0/events/properties/"
        params: dict[str, Any] = {
            "project_id": self._project_id,
            "event": event_name,
            "type": "general",
        }
        return await self._get(url, params)
