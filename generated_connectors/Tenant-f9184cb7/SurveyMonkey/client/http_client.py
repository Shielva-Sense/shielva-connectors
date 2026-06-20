from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    SurveyMonkeyAuthError,
    SurveyMonkeyError,
    SurveyMonkeyNetworkError,
    SurveyMonkeyNotFoundError,
    SurveyMonkeyRateLimitError,
)

SURVEYMONKEY_BASE_URL: str = "https://api.surveymonkey.com/v3"
SURVEYMONKEY_AUTH_URL: str = "https://api.surveymonkey.com/oauth/authorize"
SURVEYMONKEY_TOKEN_URL: str = "https://api.surveymonkey.com/oauth/token"
DEFAULT_TIMEOUT_S: float = 30.0


class SurveyMonkeyHTTPClient:
    """Low-level async HTTP client for the SurveyMonkey API v3.

    All requests use ``Authorization: Bearer {access_token}`` obtained via
    OAuth 2.0 Authorization Code flow.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        _config = config or {}
        self._access_token: str = _config.get("access_token", "")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request against the SurveyMonkey v3 API."""
        url = f"{SURVEYMONKEY_BASE_URL}{path}"
        headers = self._make_headers()
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise SurveyMonkeyNetworkError(f"Network error: {exc}") from exc
        except (
            SurveyMonkeyError,
            SurveyMonkeyAuthError,
            SurveyMonkeyRateLimitError,
            SurveyMonkeyNotFoundError,
            SurveyMonkeyNetworkError,
        ):
            raise
        except Exception as exc:
            raise SurveyMonkeyNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        """Parse the response and raise appropriate exceptions for error codes."""
        status = response.status

        if status in (200, 201):
            try:
                return await response.json()
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        err_msg: str = (
            body.get("error", {}).get("message", "")
            if isinstance(body.get("error"), dict)
            else (
                body.get("message", "")
                or body.get("error", "")
                or f"HTTP {status}"
            )
        )
        if not err_msg:
            err_msg = f"HTTP {status}"

        return self._raise_for_status(status, err_msg, response)

    def _raise_for_status(
        self,
        status: int,
        err_msg: str,
        response: aiohttp.ClientResponse | None = None,
    ) -> dict[str, Any]:
        """Map HTTP status codes to typed exceptions."""
        if status in (401, 403):
            raise SurveyMonkeyAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise SurveyMonkeyNotFoundError("resource", err_msg)
        if status == 429:
            retry_after: float = 0.0
            if response is not None:
                retry_after = float(response.headers.get("Retry-After", "0") or "0")
            raise SurveyMonkeyRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise SurveyMonkeyNetworkError(
                f"SurveyMonkey server error {status}: {err_msg}",
                status_code=status,
            )
        raise SurveyMonkeyError(
            f"SurveyMonkey error {status}: {err_msg}", status_code=status
        )

    # ── Me / Auth ─────────────────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """GET /users/me — verify credentials and return account info."""
        return await self._request("GET", "/users/me")

    # ── Surveys ───────────────────────────────────────────────────────────────

    async def get_surveys(self, page: int = 1, per_page: int = 50) -> dict[str, Any]:
        """GET /surveys — paginated survey listing.

        Uses ``links.next`` URL for pagination.
        """
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", "/surveys", params=params)

    async def get_survey(self, survey_id: str) -> dict[str, Any]:
        """GET /surveys/{survey_id} — fetch survey metadata."""
        return await self._request("GET", f"/surveys/{survey_id}")

    async def get_survey_details(self, survey_id: str) -> dict[str, Any]:
        """GET /surveys/{survey_id}/details — fetch full survey with pages/questions."""
        return await self._request("GET", f"/surveys/{survey_id}/details")

    # ── Responses ─────────────────────────────────────────────────────────────

    async def get_responses(
        self,
        survey_id: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /surveys/{survey_id}/responses/bulk — paginated bulk responses."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request(
            "GET", f"/surveys/{survey_id}/responses/bulk", params=params
        )

    # ── Collectors ────────────────────────────────────────────────────────────

    async def get_collectors(
        self,
        survey_id: str,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        """GET /surveys/{survey_id}/collectors — list collectors for a survey."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request(
            "GET", f"/surveys/{survey_id}/collectors", params=params
        )

    async def get_collectors_global(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /collectors — list all collectors (global, not per-survey)."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", "/collectors", params=params)

    # ── Groups ────────────────────────────────────────────────────────────────

    async def get_groups(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /groups — list groups/teams for the current account."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", "/groups", params=params)

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def get_contacts(self, page: int = 1, per_page: int = 50) -> dict[str, Any]:
        """GET /contacts — list contacts."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", "/contacts", params=params)

    async def get_contact_lists(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """GET /contact_lists — list contact lists."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        return await self._request("GET", "/contact_lists", params=params)

    # ── OAuth token exchange ──────────────────────────────────────────────────

    async def exchange_code_for_token(
        self,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """POST to the token endpoint to exchange an authorization code for an access token."""
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
        url = SURVEYMONKEY_TOKEN_URL
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, data=payload) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise SurveyMonkeyNetworkError(f"Network error during token exchange: {exc}") from exc

    async def refresh_access_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> dict[str, Any]:
        """POST to the token endpoint to refresh an existing access token."""
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        url = SURVEYMONKEY_TOKEN_URL
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, data=payload) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise SurveyMonkeyNetworkError(f"Network error during token refresh: {exc}") from exc
