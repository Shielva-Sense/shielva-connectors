"""All Google Calendar API HTTP calls — zero business logic, zero normalization.

Uses aiohttp.ClientSession. Each method accepts an access_token and returns the
raw parsed JSON dict. Retry and backoff are handled by the caller via
helpers/utils.with_retry().
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import aiohttp
import structlog

from exceptions import (
    GoogleCalendarAuthError,
    GoogleCalendarNetworkError,
    GoogleCalendarNotFoundError,
    GoogleCalendarRateLimitError,
)

logger = structlog.get_logger(__name__)

_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
_GOOGLE_BASE = "https://www.googleapis.com"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleCalendarHTTPClient:
    """Thin async HTTP client for the Google Calendar REST API (v3).

    All methods accept *access_token* and return raw response dicts.
    Never interprets business logic — callers own retry and normalization.
    """

    def __init__(self, base_url: str = _CALENDAR_BASE) -> None:
        self._base_url = base_url.rstrip("/")

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse, body: Dict[str, Any]
    ) -> None:
        """Map HTTP error codes to connector exceptions.

        Called AFTER the body has been parsed, so callers must read the body
        before calling this method.
        """
        status = response.status
        if status < 400:
            return

        error_obj = body.get("error", {})
        if isinstance(error_obj, dict):
            message = error_obj.get("message", "") or str(body)
        else:
            message = str(error_obj) or str(body)

        if status in (401, 403):
            raise GoogleCalendarAuthError(
                f"{status} Unauthorized: {message}"
            )
        if status == 404:
            raise GoogleCalendarNotFoundError(
                f"404 Not Found: {message}"
            )
        if status == 429:
            raise GoogleCalendarRateLimitError(
                f"429 Rate limit exceeded"
            )
        raise GoogleCalendarNetworkError(
            f"HTTP {status}: {message}"
        )

    async def get_calendar(self, access_token: str, calendar_id: str) -> Dict[str, Any]:
        """GET /calendars/{calendarId} — get info for a specific calendar."""
        url = f"{self._base_url}/calendars/{calendar_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self._auth_headers(access_token)) as resp:
                    try:
                        body: Dict[str, Any] = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    await self._raise_for_status(resp, body)
                    return body
        except (GoogleCalendarAuthError, GoogleCalendarNotFoundError,
                GoogleCalendarRateLimitError, GoogleCalendarNetworkError):
            raise
        except aiohttp.ClientError as exc:
            raise GoogleCalendarNetworkError(
                f"Network error in get_calendar({calendar_id}): {exc}"
            ) from exc

    async def get_primary_calendar(self, access_token: str) -> Dict[str, Any]:
        """GET /calendars/primary — verify token works and fetch primary calendar."""
        return await self.get_calendar(access_token, "primary")

    async def get_calendar_list(self, access_token: str) -> Dict[str, Any]:
        """GET /users/me/calendarList — list all calendars for the authenticated user."""
        url = f"{self._base_url}/users/me/calendarList"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self._auth_headers(access_token)) as resp:
                    try:
                        body: Dict[str, Any] = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    await self._raise_for_status(resp, body)
                    return body
        except (GoogleCalendarAuthError, GoogleCalendarNotFoundError,
                GoogleCalendarRateLimitError, GoogleCalendarNetworkError):
            raise
        except aiohttp.ClientError as exc:
            raise GoogleCalendarNetworkError(
                f"Network error in get_calendar_list: {exc}"
            ) from exc

    async def get_events(
        self,
        access_token: str,
        calendar_id: str = "primary",
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        max_results: int = 100,
        page_token: Optional[str] = None,
        single_events: bool = True,
        order_by: str = "startTime",
    ) -> Dict[str, Any]:
        """GET /calendars/{calendarId}/events — list events in a calendar.

        *time_min* and *time_max* must be RFC 3339 timestamps when provided.
        *single_events* expands recurring events into individual instances.
        """
        url = f"{self._base_url}/calendars/{calendar_id}/events"
        params: Dict[str, Any] = {
            "maxResults": max_results,
            "singleEvents": str(single_events).lower(),
            "orderBy": order_by,
        }
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        if page_token:
            params["pageToken"] = page_token

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=self._auth_headers(access_token),
                    params=params,
                ) as resp:
                    try:
                        body: Dict[str, Any] = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    await self._raise_for_status(resp, body)
                    return body
        except (GoogleCalendarAuthError, GoogleCalendarNotFoundError,
                GoogleCalendarRateLimitError, GoogleCalendarNetworkError):
            raise
        except aiohttp.ClientError as exc:
            raise GoogleCalendarNetworkError(
                f"Network error in get_events({calendar_id}): {exc}"
            ) from exc

    async def get_event(
        self,
        access_token: str,
        calendar_id: str,
        event_id: str,
    ) -> Dict[str, Any]:
        """GET /calendars/{calendarId}/events/{eventId} — fetch a single event."""
        url = f"{self._base_url}/calendars/{calendar_id}/events/{event_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self._auth_headers(access_token)) as resp:
                    try:
                        body: Dict[str, Any] = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    await self._raise_for_status(resp, body)
                    return body
        except (GoogleCalendarAuthError, GoogleCalendarNotFoundError,
                GoogleCalendarRateLimitError, GoogleCalendarNetworkError):
            raise
        except aiohttp.ClientError as exc:
            raise GoogleCalendarNetworkError(
                f"Network error in get_event({calendar_id}, {event_id}): {exc}"
            ) from exc

    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """GET /oauth2/v2/userinfo — fetch authenticated user info.

        Uses the base Google API URL (not the Calendar-specific base).
        """
        url = f"{_GOOGLE_BASE}/oauth2/v2/userinfo"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=self._auth_headers(access_token)
                ) as resp:
                    try:
                        body: Dict[str, Any] = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    await self._raise_for_status(resp, body)
                    return body
        except (GoogleCalendarAuthError, GoogleCalendarNotFoundError,
                GoogleCalendarRateLimitError, GoogleCalendarNetworkError):
            raise
        except aiohttp.ClientError as exc:
            raise GoogleCalendarNetworkError(
                f"Network error in get_user_info: {exc}"
            ) from exc

    async def refresh_access_token(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> Dict[str, Any]:
        """POST to token URL with refresh_token grant type.

        Returns the raw token response dict (access_token, expires_in, etc.).
        """
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        return await self._post_form(_TOKEN_URL, payload, "refresh_access_token")

    async def exchange_code_for_token(
        self,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        token_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST to token URL with authorization_code grant type.

        Returns the raw token response dict (access_token, refresh_token, etc.).
        """
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
        return await self._post_form(
            token_url or _TOKEN_URL, payload, "exchange_code_for_token"
        )

    async def post_form_data(
        self,
        url: str,
        payload: Dict[str, str],
        context: str = "post_form_data",
    ) -> Dict[str, Any]:
        """Generic POST of form-encoded data — used for OAuth token operations.

        Returns parsed JSON. All auth field naming stays in connector.py.
        """
        return await self._post_form(url, payload, context)

    async def _post_form(
        self,
        url: str,
        payload: Dict[str, str],
        context: str,
    ) -> Dict[str, Any]:
        """Internal: POST application/x-www-form-urlencoded and return JSON."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload) as resp:
                    try:
                        body: Dict[str, Any] = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    await self._raise_for_status(resp, body)
                    return body
        except (GoogleCalendarAuthError, GoogleCalendarNotFoundError,
                GoogleCalendarRateLimitError, GoogleCalendarNetworkError):
            raise
        except aiohttp.ClientError as exc:
            raise GoogleCalendarNetworkError(
                f"Network error in {context}: {exc}"
            ) from exc
