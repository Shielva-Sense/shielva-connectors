"""All Gmail API HTTP calls — zero business logic, zero normalization."""
from typing import Any, Dict, List, Optional

import aiohttp
import structlog

from exceptions import GmailAPIError, GmailAuthError, GmailRateLimitError

logger = structlog.get_logger(__name__)

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"


class GmailHTTPClient:
    """Thin async HTTP client for the Gmail REST API.

    All methods accept an *access_token* and return raw response dicts.
    Retry logic is handled by the caller via helpers/utils.with_retry().
    """

    def __init__(self, base_url: str = _GMAIL_BASE):
        self._base_url = base_url.rstrip("/")

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    async def _raise_for_status(self, response: aiohttp.ClientResponse, context: str = "") -> None:
        """Map HTTP error codes to connector exceptions."""
        status = response.status
        if status < 400:
            return
        try:
            body: Dict[str, Any] = await response.json(content_type=None)
        except Exception:
            body = {}

        error_obj = body.get("error", {})
        if isinstance(error_obj, dict):
            message = error_obj.get("message", "") or str(body)
        else:
            message = str(error_obj) or str(body)

        if status == 401:
            raise GmailAuthError(f"401 Unauthorized{': ' + context if context else ''}: {message}")
        if status == 403:
            raise GmailAPIError(message, status_code=403, response_body=body)
        if status == 429:
            raise GmailRateLimitError(f"429 Rate limit exceeded{': ' + context if context else ''}")
        raise GmailAPIError(
            f"HTTP {status}{': ' + context if context else ''}: {message}",
            status_code=status,
            response_body=body,
        )

    async def get_profile(self, access_token: str) -> Dict[str, Any]:
        """GET /users/me/profile — returns Gmail account profile."""
        url = f"{self._base_url}/users/me/profile"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._auth_headers(access_token)) as resp:
                await self._raise_for_status(resp, "get_profile")
                return await resp.json()

    async def list_messages(
        self,
        access_token: str,
        query: str = "",
        max_results: int = 500,
        page_token: Optional[str] = None,
        label_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /users/me/messages — list message stubs."""
        url = f"{self._base_url}/users/me/messages"
        params: Dict[str, Any] = {"maxResults": max_results}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        if label_ids:
            params["labelIds"] = label_ids
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._auth_headers(access_token), params=params) as resp:
                await self._raise_for_status(resp, "list_messages")
                return await resp.json()

    async def get_message(
        self,
        access_token: str,
        message_id: str,
        fmt: str = "full",
    ) -> Dict[str, Any]:
        """GET /users/me/messages/{id} — fetch full message."""
        url = f"{self._base_url}/users/me/messages/{message_id}"
        params = {"format": fmt}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._auth_headers(access_token), params=params) as resp:
                await self._raise_for_status(resp, f"get_message({message_id})")
                return await resp.json()

    async def execute_modify_message(
        self,
        access_token: str,
        message_id: str,
        add_label_ids: Optional[List[str]] = None,
        remove_label_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /users/me/messages/{id}/modify — add/remove labels."""
        url = f"{self._base_url}/users/me/messages/{message_id}/modify"
        body = {
            "addLabelIds": add_label_ids or [],
            "removeLabelIds": remove_label_ids or [],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._auth_headers(access_token), json=body) as resp:
                await self._raise_for_status(resp, f"modify_message({message_id})")
                return await resp.json()

    async def execute_send_message(
        self,
        access_token: str,
        raw_message: str,
    ) -> Dict[str, Any]:
        """POST /users/me/messages/send — send a base64url-encoded RFC 2822 message.

        Mirrors execute_modify_message(): accepts the encoded payload, POSTs it,
        and returns the full response dict.

        Raises:
            PermissionError: On 403 — gmail.send scope is missing.
            ValueError: On 400 — bad request body (surfaces error_description).
            GmailRateLimitError: On 429.
            GmailAuthError: On 401.
            GmailAPIError: On other 4xx/5xx.
        """
        url = f"{self._base_url}/users/me/messages/send"
        body = {"raw": raw_message}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._auth_headers(access_token), json=body) as resp:
                status = resp.status
                if status == 403:
                    raise PermissionError(
                        "gmail.send scope missing — re-authorize the connector"
                    )
                if status == 400:
                    try:
                        err_body = await resp.json(content_type=None)
                    except Exception:
                        err_body = {}
                    error_obj = err_body.get("error", {})
                    description = (
                        (error_obj.get("message") if isinstance(error_obj, dict) else str(error_obj))
                        or str(err_body)
                    )
                    raise ValueError(description)
                await self._raise_for_status(resp, "execute_send_message")
                return await resp.json()

    async def execute_create_draft(
        self,
        access_token: str,
        raw_message: str,
    ) -> Dict[str, Any]:
        """POST /users/me/drafts — create a draft from a base64url-encoded MIME message."""
        url = f"{self._base_url}/users/me/drafts"
        body = {"message": {"raw": raw_message}}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._auth_headers(access_token), json=body) as resp:
                await self._raise_for_status(resp, "execute_create_draft")
                return await resp.json()

    async def list_history(
        self,
        access_token: str,
        start_history_id: str,
        history_types: Optional[List[str]] = None,
        max_results: int = 500,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /users/me/history — list changes since a history ID."""
        url = f"{self._base_url}/users/me/history"
        params: Dict[str, Any] = {
            "startHistoryId": start_history_id,
            "maxResults": max_results,
        }
        if history_types:
            params["historyTypes"] = history_types
        if page_token:
            params["pageToken"] = page_token
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._auth_headers(access_token), params=params) as resp:
                await self._raise_for_status(resp, "list_history")
                return await resp.json()

    async def post_form_data(
        self,
        url: str,
        payload: Dict[str, str],
        context: str = "post_form_data",
    ) -> Dict[str, Any]:
        """Generic POST of form-encoded data to any URL — returns parsed JSON.

        Used by connector.py to exchange and renew tokens.  The payload is built
        in connector.py, keeping all auth-specific field names out of this class.
        """
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=payload) as resp:
                await self._raise_for_status(resp, context)
                return await resp.json()
