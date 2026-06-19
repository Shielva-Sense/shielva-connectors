"""Outlook Calendar connector — Microsoft Graph HTTP client."""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from exceptions import (
    OutlookCalendarAuthError,
    OutlookCalendarNetworkError,
    OutlookCalendarNotFoundError,
    OutlookCalendarRateLimitError,
)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_DEFAULT_TIMEOUT = 20.0


class OutlookCalendarHTTPClient:
    """Thin async HTTP wrapper for Microsoft Graph Calendar endpoints."""

    def __init__(self, base_url: str = _GRAPH_BASE, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

    def _raise_for_status(self, resp: httpx.Response, context: str) -> None:
        if resp.status_code == 401:
            raise OutlookCalendarAuthError(f"[{context}] 401 Unauthorized — token expired or invalid")
        if resp.status_code == 403:
            raise OutlookCalendarAuthError(f"[{context}] 403 Forbidden — insufficient scopes")
        if resp.status_code == 404:
            raise OutlookCalendarNotFoundError(f"[{context}] 404 Not Found")
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "60")
            raise OutlookCalendarRateLimitError(f"[{context}] 429 Rate limited — retry after {retry_after}s")
        if resp.status_code >= 400:
            raise OutlookCalendarNetworkError(f"[{context}] HTTP {resp.status_code}: {resp.text[:200]}")

    async def get_me(self, access_token: str) -> Dict[str, Any]:
        """Fetch authenticated user profile — used as connectivity check."""
        url = f"{self._base_url}/me"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                resp = await c.get(url, headers=self._auth_headers(access_token))
        except httpx.TimeoutException as exc:
            raise OutlookCalendarNetworkError(f"[get_me] Timeout: {exc}") from exc
        except httpx.RequestError as exc:
            raise OutlookCalendarNetworkError(f"[get_me] Request error: {exc}") from exc
        self._raise_for_status(resp, "get_me")
        return resp.json()

    async def get_calendars(self, access_token: str) -> Dict[str, Any]:
        """Return all calendars for the authenticated user."""
        url = f"{self._base_url}/me/calendars"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                resp = await c.get(url, headers=self._auth_headers(access_token))
        except httpx.TimeoutException as exc:
            raise OutlookCalendarNetworkError(f"[get_calendars] Timeout: {exc}") from exc
        except httpx.RequestError as exc:
            raise OutlookCalendarNetworkError(f"[get_calendars] Request error: {exc}") from exc
        self._raise_for_status(resp, "get_calendars")
        return resp.json()

    async def get_events(
        self,
        access_token: str,
        calendar_id: str = "primary",
        start_datetime: Optional[str] = None,
        end_datetime: Optional[str] = None,
        top: int = 100,
        next_link: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return events from a calendar view (time-bounded)."""
        if next_link:
            url = next_link
            params: Dict[str, Any] = {}
        else:
            url = (
                f"{self._base_url}/me/calendarView"
                if calendar_id in ("primary", "")
                else f"{self._base_url}/me/calendars/{calendar_id}/calendarView"
            )
            params = {"$top": top}
            if start_datetime:
                params["startDateTime"] = start_datetime
            if end_datetime:
                params["endDateTime"] = end_datetime

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                resp = await c.get(url, headers=self._auth_headers(access_token), params=params)
        except httpx.TimeoutException as exc:
            raise OutlookCalendarNetworkError(f"[get_events] Timeout: {exc}") from exc
        except httpx.RequestError as exc:
            raise OutlookCalendarNetworkError(f"[get_events] Request error: {exc}") from exc
        self._raise_for_status(resp, "get_events")
        return resp.json()

    async def get_event(
        self,
        access_token: str,
        event_id: str,
        calendar_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch a single event by id."""
        if calendar_id and calendar_id not in ("primary", ""):
            url = f"{self._base_url}/me/calendars/{calendar_id}/events/{event_id}"
        else:
            url = f"{self._base_url}/me/events/{event_id}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                resp = await c.get(url, headers=self._auth_headers(access_token))
        except httpx.TimeoutException as exc:
            raise OutlookCalendarNetworkError(f"[get_event] Timeout: {exc}") from exc
        except httpx.RequestError as exc:
            raise OutlookCalendarNetworkError(f"[get_event] Request error: {exc}") from exc
        self._raise_for_status(resp, "get_event")
        return resp.json()

    async def post_form_data(
        self,
        url: str,
        payload: Dict[str, str],
        context: str = "post_form_data",
    ) -> Dict[str, Any]:
        """POST application/x-www-form-urlencoded — used for token exchange."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                resp = await c.post(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.TimeoutException as exc:
            raise OutlookCalendarNetworkError(f"[{context}] Timeout: {exc}") from exc
        except httpx.RequestError as exc:
            raise OutlookCalendarNetworkError(f"[{context}] Request error: {exc}") from exc
        if resp.status_code >= 400:
            raise OutlookCalendarAuthError(
                f"[{context}] Token request failed {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()
