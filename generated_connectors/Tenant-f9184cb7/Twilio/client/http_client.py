from __future__ import annotations

import base64
from typing import Any

import aiohttp

from exceptions import (
    TwilioAuthError,
    TwilioNetworkError,
    TwilioNotFoundError,
    TwilioRateLimitError,
)

TWILIO_BASE_URL = "https://api.twilio.com/2010-04-01"
DEFAULT_TIMEOUT_S = 30.0


def _basic_auth(account_sid: str, auth_token: str) -> str:
    """Return a base64-encoded Basic auth header value."""
    creds = f"{account_sid}:{auth_token}"
    return "Basic " + base64.b64encode(creds.encode()).decode()


class TwilioHTTPClient:
    """Low-level async HTTP client for the Twilio REST API (2010-04-01)."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _request(
        self,
        method: str,
        url: str,
        account_sid: str,
        auth_token: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": _basic_auth(account_sid, auth_token),
            "Accept": "application/json",
        }
        session = self._get_session()
        try:
            async with session.request(method, url, headers=headers, **kwargs) as response:
                if response.status == 200:
                    return await response.json(content_type=None)

                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                err_msg = body.get("message", str(body) or "Unknown Twilio error")
                err_code = str(body.get("code", ""))

                if response.status in (401, 403):
                    raise TwilioAuthError(
                        f"Authentication failed: {err_msg}",
                        response.status,
                        err_code,
                    )
                if response.status == 404:
                    raise TwilioNotFoundError("resource", err_code or "unknown")
                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise TwilioRateLimitError(f"Rate limited: {err_msg}", retry_after)
                if response.status >= 500:
                    raise TwilioNetworkError(
                        f"Twilio server error {response.status}: {err_msg}",
                        response.status,
                        err_code,
                    )
                from exceptions import TwilioError
                raise TwilioError(
                    f"Twilio error {response.status}: {err_msg}",
                    response.status,
                    err_code,
                )
        except (aiohttp.ServerTimeoutError, TimeoutError) as exc:
            raise TwilioNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise TwilioNetworkError(f"Connection error: {exc}") from exc
        except (TwilioAuthError, TwilioNotFoundError, TwilioRateLimitError, TwilioNetworkError):
            raise
        except Exception as exc:
            # Re-raise TwilioError subclasses that come from the inner try
            from exceptions import TwilioError as _TE
            if isinstance(exc, _TE):
                raise
            raise TwilioNetworkError(f"Unexpected error: {exc}") from exc

    # ── Account ──────────────────────────────────────────────────────────────

    async def get_account(self, account_sid: str, auth_token: str) -> dict[str, Any]:
        """GET /Accounts/{account_sid}.json — verify credentials."""
        url = f"{TWILIO_BASE_URL}/Accounts/{account_sid}.json"
        return await self._request("GET", url, account_sid, auth_token)

    # ── Messages ─────────────────────────────────────────────────────────────

    async def list_messages(
        self,
        account_sid: str,
        auth_token: str,
        page_size: int = 100,
        page_token: str | None = None,
        date_sent_after: str | None = None,
    ) -> dict[str, Any]:
        """GET /Accounts/{account_sid}/Messages.json — first page or a specific page."""
        if page_token:
            url = f"https://api.twilio.com{page_token}"
        else:
            url = f"{TWILIO_BASE_URL}/Accounts/{account_sid}/Messages.json"
        params: dict[str, Any] = {"PageSize": page_size}
        if date_sent_after and not page_token:
            params["DateSent>"] = date_sent_after
        return await self._request("GET", url, account_sid, auth_token, params=params)

    async def get_message(
        self,
        account_sid: str,
        auth_token: str,
        message_sid: str,
    ) -> dict[str, Any]:
        """GET /Accounts/{account_sid}/Messages/{Sid}.json."""
        url = f"{TWILIO_BASE_URL}/Accounts/{account_sid}/Messages/{message_sid}.json"
        return await self._request("GET", url, account_sid, auth_token)

    # ── Calls ────────────────────────────────────────────────────────────────

    async def list_calls(
        self,
        account_sid: str,
        auth_token: str,
        page_size: int = 100,
        page_token: str | None = None,
        start_time_after: str | None = None,
    ) -> dict[str, Any]:
        """GET /Accounts/{account_sid}/Calls.json — first page or a specific page."""
        if page_token:
            url = f"https://api.twilio.com{page_token}"
        else:
            url = f"{TWILIO_BASE_URL}/Accounts/{account_sid}/Calls.json"
        params: dict[str, Any] = {"PageSize": page_size}
        if start_time_after and not page_token:
            params["StartTime>"] = start_time_after
        return await self._request("GET", url, account_sid, auth_token, params=params)

    async def get_call(
        self,
        account_sid: str,
        auth_token: str,
        call_sid: str,
    ) -> dict[str, Any]:
        """GET /Accounts/{account_sid}/Calls/{Sid}.json."""
        url = f"{TWILIO_BASE_URL}/Accounts/{account_sid}/Calls/{call_sid}.json"
        return await self._request("GET", url, account_sid, auth_token)

    # ── Recordings ────────────────────────────────────────────────────────────

    async def list_recordings(
        self,
        account_sid: str,
        auth_token: str,
        call_sid: str | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """GET /Accounts/{account_sid}/Recordings.json — optionally filtered by CallSid."""
        url = f"{TWILIO_BASE_URL}/Accounts/{account_sid}/Recordings.json"
        params: dict[str, Any] = {"PageSize": page_size}
        if call_sid:
            params["CallSid"] = call_sid
        return await self._request("GET", url, account_sid, auth_token, params=params)

    # ── Phone Numbers ─────────────────────────────────────────────────────────

    async def list_phone_numbers(
        self,
        account_sid: str,
        auth_token: str,
    ) -> dict[str, Any]:
        """GET /Accounts/{account_sid}/IncomingPhoneNumbers.json."""
        url = f"{TWILIO_BASE_URL}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
        return await self._request("GET", url, account_sid, auth_token)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> TwilioHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
