"""Gmail API HTTP client.

Sole owner of:
- Building the Google API service object from an access_token.
- All execute_* methods that call the Gmail REST API.
- Mapping HttpError status codes to custom exceptions.

Does NOT own token refresh, OAuth flow, or credential persistence.
Those live exclusively in connector.py / on_token_refresh().
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import structlog
from google.auth.exceptions import TransportError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from exceptions import GmailAPIError, GmailAuthError, GmailRateLimitError
from helpers.utils import retry_on_rate_limit, retry_on_server_error

logger = structlog.get_logger(__name__)

# Maps HttpError status codes → (exception_class, message_prefix)
_HTTP_ERROR_MAP: Dict[int, tuple] = {
    401: (GmailAuthError, "HTTP 401"),
    403: (GmailAuthError, "HTTP 403"),
    429: (GmailRateLimitError, "HTTP 429 rate limit"),
}


class GmailHTTPClient:
    """Executes Gmail API calls; no auth-flow or token-refresh logic here."""

    def __init__(
        self,
        access_token: str,
        api_version: str = "v1",
    ) -> None:
        self._access_token = access_token
        self._api_version = api_version

    # ── Credentials helper ─────────────────────────────────────────────────

    def _build_credentials(self) -> Credentials:
        """Build a simple Credentials object from the current access token."""
        return Credentials(token=self._access_token)

    async def _get_service(self) -> Any:
        """Build and return an authenticated Gmail service object."""
        creds = self._build_credentials()
        loop = asyncio.get_event_loop()
        service = await loop.run_in_executor(
            None,
            lambda: build("gmail", self._api_version, credentials=creds, cache_discovery=False),
        )
        return service

    # ── Error mapping ──────────────────────────────────────────────────────

    @staticmethod
    def _map_http_error(exc: HttpError) -> Exception:
        status = int(exc.resp.status)
        exc_class, prefix = _HTTP_ERROR_MAP.get(status, (GmailAPIError, f"HTTP {status}"))
        return exc_class(f"{prefix}: {exc}")

    # ── Public execute_* methods ───────────────────────────────────────────

    async def execute_get_profile(self) -> Dict[str, Any]:
        """Call users.getProfile to verify token validity."""
        try:
            service = await self._get_service()
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: service.users().getProfile(userId="me").execute(),
            )
        except HttpError as exc:
            raise self._map_http_error(exc) from exc
        except TransportError as exc:
            raise GmailAPIError(f"Transport error: {exc}") from exc

    @retry_on_rate_limit
    async def execute_list_messages(
        self,
        label_ids: Optional[List[str]] = None,
        page_token: Optional[str] = None,
        max_results: int = 100,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Call users.messages.list for one page of message stubs."""
        if label_ids is None:
            label_ids = ["INBOX", "UNREAD"]
        try:
            service = await self._get_service()
            loop = asyncio.get_event_loop()

            kwargs: Dict[str, Any] = {
                "userId": "me",
                "labelIds": label_ids,
                "maxResults": max_results,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            if query:
                kwargs["q"] = query

            return await loop.run_in_executor(
                None,
                lambda: service.users().messages().list(**kwargs).execute(),
            )
        except HttpError as exc:
            raise self._map_http_error(exc) from exc
        except TransportError as exc:
            raise GmailAPIError(f"Transport error: {exc}") from exc

    @retry_on_server_error
    async def execute_get_message(
        self,
        msg_id: str,
        format: str = "metadata",
        metadata_headers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Call users.messages.get to fetch message metadata + snippet."""
        if metadata_headers is None:
            metadata_headers = ["Subject", "From", "Date"]
        try:
            service = await self._get_service()
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format=format,
                    metadataHeaders=metadata_headers,
                )
                .execute(),
            )
        except HttpError as exc:
            raise self._map_http_error(exc) from exc
        except TransportError as exc:
            raise GmailAPIError(f"Transport error: {exc}") from exc
