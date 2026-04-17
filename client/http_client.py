"""
Gmail Connector — HTTP Client
ALL Gmail API calls live here. connector.py NEVER calls .execute() or build() directly.
Handles retry logic, exponential backoff, Retry-After header, and HTTP error mapping.
"""
import asyncio
import time
from typing import Any, Dict, List, Optional

import structlog

from exceptions import GmailAuthError, map_http_error

logger = structlog.get_logger(__name__)


class GmailHttpClient:
    """
    Wraps google-api-python-client's Gmail service behind named async methods.
    Exposes only domain operations; never leaks googleapiclient internals to connector.py.
    """

    MAX_RETRIES: int = 3
    INITIAL_BACKOFF_S: float = 1.0
    BACKOFF_FACTOR: float = 2.0
    REQUEST_TIMEOUT_S: int = 60

    def __init__(self, credentials: Any, base_url: str = "https://gmail.googleapis.com") -> None:
        """
        Args:
            credentials: google.oauth2.credentials.Credentials instance.
            base_url: Optional override for the Gmail API base URL.
        """
        from googleapiclient.discovery import build
        import httplib2
        import google_auth_httplib2

        http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http())
        self._service = build(
            "gmail",
            "v1",
            http=http,
            cache_discovery=False,
        )
        self._log = logger.bind(client="GmailHttpClient")

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _execute_with_retry(self, request: Any, message_id: str = "") -> Dict[str, Any]:
        """
        Execute a googleapiclient request with retry + exponential backoff.
        Handles HTTP 429 Retry-After and 5xx retries.
        Raises domain exceptions on unrecoverable errors.
        """
        from googleapiclient.errors import HttpError

        backoff = self.INITIAL_BACKOFF_S
        last_exc: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                return request.execute(num_retries=0)
            except HttpError as exc:
                status = exc.resp.status
                headers = dict(exc.resp)

                if status in (429, 500, 502, 503, 504):
                    if attempt == self.MAX_RETRIES:
                        raise map_http_error(status, message_id=message_id) from exc

                    # Respect Retry-After header on 429
                    retry_after = float(headers.get("retry-after", backoff))
                    sleep_for = max(retry_after, backoff)
                    self._log.warning(
                        "gmail.retry",
                        attempt=attempt + 1,
                        status=status,
                        sleep_for=sleep_for,
                    )
                    time.sleep(sleep_for)
                    backoff *= self.BACKOFF_FACTOR
                    last_exc = exc
                else:
                    raise map_http_error(status, message_id=message_id) from exc

        raise map_http_error(500) from last_exc  # should not reach here

    # ── Public API methods ───────────────────────────────────────────────────

    async def list_messages(
        self,
        query: str = "",
        page_token: Optional[str] = None,
        max_results: int = 20,
    ) -> Dict[str, Any]:
        """
        List Gmail messages matching an optional query.

        Returns dict with keys: messages (list[{id, threadId}]), nextPageToken (optional),
        resultSizeEstimate (int).
        """
        kwargs: Dict[str, Any] = {
            "userId": "me",
            "maxResults": max_results,
        }
        if query:
            kwargs["q"] = query
        if page_token:
            kwargs["pageToken"] = page_token

        request = self._service.users().messages().list(**kwargs)
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._execute_with_retry(request)
        )
        return result

    async def get_message(self, message_id: str) -> Dict[str, Any]:
        """
        Fetch a full Gmail message resource by ID.

        Returns the raw Gmail message dict (format=full).
        Raises GmailMessageNotFoundError on 404.
        """
        request = self._service.users().messages().get(
            userId="me", id=message_id, format="full"
        )
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._execute_with_retry(request, message_id=message_id)
        )
        return result

    async def send_message(self, raw_message: str) -> Dict[str, Any]:
        """
        Send a base64url-encoded RFC 2822 message.

        Args:
            raw_message: base64url-encoded MIME message string from helpers/utils.py.

        Returns dict with id, threadId, labelIds.
        Raises GmailAPIError on 400 (malformed MIME).
        """
        request = self._service.users().messages().send(
            userId="me", body={"raw": raw_message}
        )
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._execute_with_retry(request)
        )
        return result

    async def trash_message(self, message_id: str) -> Dict[str, Any]:
        """
        Move a message to the Trash label.

        Returns the updated message resource.
        Raises GmailMessageNotFoundError on 404.
        """
        request = self._service.users().messages().trash(
            userId="me", id=message_id
        )
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._execute_with_retry(request, message_id=message_id)
        )
        return result

    async def delete_message_permanent(self, message_id: str) -> None:
        """
        Permanently delete a message (irreversible).

        Raises GmailMessageNotFoundError on 404.
        Returns None on HTTP 204.
        """
        request = self._service.users().messages().delete(
            userId="me", id=message_id
        )
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._execute_with_retry(request, message_id=message_id)
        )

    async def get_profile(self) -> Dict[str, Any]:
        """Fetch the authenticated user's Gmail profile (used in health_check probe)."""
        request = self._service.users().getProfile(userId="me")
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._execute_with_retry(request)
        )
        return result
