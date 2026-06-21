"""All Dropbox Sign API HTTP calls — zero business logic, zero normalization.

Auth: HTTP Basic, where the username is the API key and the password is empty
(`Authorization: Basic base64(api_key + ":")`).

Wire format:
- Most POSTs use `application/x-www-form-urlencoded` with bracket-indexed
  notation for nested fields:
      signers[0][email_address]=alice@example.com
      signers[0][name]=Alice
      file_url[0]=https://...
      metadata[custom]=value
- Sends accepting file uploads (`file[]`) use `multipart/form-data`.

Retry strategy: exponential backoff on 429 + 5xx, up to `max_retries` attempts.
The retry envelope lives here so the connector methods stay single-purpose.
"""
import asyncio
import base64
from typing import Any, Dict, List, Optional, Tuple

import httpx
import structlog

from exceptions import (
    DropboxSignAuthError,
    DropboxSignBadRequestError,
    DropboxSignConflictError,
    DropboxSignError,
    DropboxSignNetworkError,
    DropboxSignNotFoundError,
    DropboxSignRateLimitError,
    DropboxSignServerError,
)

logger = structlog.get_logger(__name__)

_BASE_URL = "https://api.hellosign.com/v3"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds
_RETRY_STATUS = {429, 500, 502, 503, 504}


class DropboxSignHTTPClient:
    """Thin async HTTP client for the Dropbox Sign REST API."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = _BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ):
        self._api_key = api_key or ""
        self._base_url = (base_url or _BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries

    # ── Auth helpers ────────────────────────────────────────────────────────

    def set_api_key(self, api_key: str) -> None:
        """Allow the connector to inject the API key after instantiation."""
        self._api_key = api_key or ""

    def _basic_auth_header(self) -> str:
        """Return the `Authorization: Basic …` header value.

        Dropbox Sign expects HTTP Basic with the API key as the username and an
        empty password — i.e. `base64(api_key + ":")`.
        """
        raw = f"{self._api_key}:".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _headers(self, accept: str = "application/json") -> Dict[str, str]:
        return {
            "Authorization": self._basic_auth_header(),
            "Accept": accept,
            "User-Agent": "shielva-dropbox-sign-connector/1.0",
        }

    # ── Error mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _decode_body(response: httpx.Response) -> Dict[str, Any]:
        try:
            body = response.json()
            return body if isinstance(body, dict) else {"raw": body}
        except Exception:
            return {"raw": response.text}

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        status = response.status_code
        if status < 400:
            return
        body = self._decode_body(response)
        # Dropbox Sign error envelope: {"error": {"error_msg": "...", "error_name": "..."}}
        err = body.get("error") if isinstance(body, dict) else {}
        if isinstance(err, dict):
            message = err.get("error_msg") or err.get("error_name") or str(body)
        else:
            message = str(err) or str(body)

        ctx = f": {context}" if context else ""
        if status in (401, 403):
            raise DropboxSignAuthError(
                f"HTTP {status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        if status == 400:
            raise DropboxSignBadRequestError(
                f"HTTP 400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body,
            )
        if status == 404:
            raise DropboxSignNotFoundError(
                f"HTTP 404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body,
            )
        if status == 409:
            raise DropboxSignConflictError(
                f"HTTP 409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After", "")
            try:
                retry_after_s = float(retry_after) if retry_after else 5.0
            except ValueError:
                retry_after_s = 5.0
            raise DropboxSignRateLimitError(
                f"HTTP 429 Rate Limited{ctx}: {message}",
                retry_after_s=retry_after_s,
            )
        if 500 <= status < 600:
            raise DropboxSignServerError(
                f"HTTP {status} Server Error{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        raise DropboxSignError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body,
        )

    # ── Core request envelope (retry on 429 + 5xx) ──────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[List[Tuple[str, Any]]] = None,
        accept: str = "application/json",
        context: str = "",
        expect_bytes: bool = False,
    ) -> Any:
        url = f"{self._base_url}{path if path.startswith('/') else '/' + path}"
        headers = self._headers(accept=accept)

        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    if files is not None:
                        response = await client.request(
                            method, url,
                            headers=headers,
                            params=params,
                            data=dict(self._flatten_form(data or {})),
                            files=files,
                        )
                    elif data is not None:
                        response = await client.request(
                            method, url,
                            headers=headers,
                            params=params,
                            data=dict(self._flatten_form(data)),
                        )
                    else:
                        response = await client.request(
                            method, url,
                            headers=headers,
                            params=params,
                        )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "dropbox_sign.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise DropboxSignNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise DropboxSignNetworkError(
                    f"Network error{': ' + context if context else ''}: {exc}"
                ) from exc

            if response.status_code in _RETRY_STATUS and attempt < self._max_retries:
                delay = _BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "dropbox_sign.http.retry",
                    status=response.status_code,
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                )
                await asyncio.sleep(delay)
                continue

            self._raise_for_status(response, context)
            if expect_bytes:
                return response.content
            if response.status_code == 204 or not response.content:
                return {}
            return self._decode_body(response)

        # Defensive: should be unreachable
        if last_exc:
            raise DropboxSignNetworkError(str(last_exc))
        raise DropboxSignError("request loop exited without response")

    @staticmethod
    def _flatten_form(data: Dict[str, Any]) -> List[Tuple[str, str]]:
        """Flatten a nested dict into Dropbox Sign form-encoded key/value pairs.

        Dropbox Sign uses indexed bracket notation for list/dict params:
            signers[0][email_address]=alice@example.com
            signers[0][name]=Alice
            file_url[0]=https://...
            metadata[custom]=value
        """
        pairs: List[Tuple[str, str]] = []

        def encode(key: str, value: Any) -> None:
            if value is None:
                return
            if isinstance(value, bool):
                pairs.append((key, "1" if value else "0"))
            elif isinstance(value, (str, int, float)):
                pairs.append((key, str(value)))
            elif isinstance(value, dict):
                for sub_key, sub_val in value.items():
                    encode(f"{key}[{sub_key}]", sub_val)
            elif isinstance(value, list):
                for idx, item in enumerate(value):
                    encode(f"{key}[{idx}]", item)
            else:
                pairs.append((key, str(value)))

        for k, v in data.items():
            encode(k, v)
        return pairs

    # ── Account ─────────────────────────────────────────────────────────────

    async def get_account(self) -> Dict[str, Any]:
        return await self._request("GET", "/account", context="get_account")

    # ── Signature requests ──────────────────────────────────────────────────

    async def list_signature_requests(
        self,
        page: int = 1,
        page_size: int = 20,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "page_size": page_size}
        if query:
            params["query"] = query
        return await self._request(
            "GET", "/signature_request/list",
            params=params,
            context="list_signature_requests",
        )

    async def get_signature_request(self, signature_request_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/signature_request/{signature_request_id}",
            context=f"get_signature_request({signature_request_id})",
        )

    async def send_signature_request(
        self,
        *,
        title: str,
        subject: str,
        message: str,
        signers: List[Dict[str, Any]],
        file_urls: Optional[List[str]] = None,
        files: Optional[List[Tuple[str, bytes, str]]] = None,
        test_mode: bool = True,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "title": title,
            "subject": subject,
            "message": message,
            "test_mode": test_mode,
            "signers": signers,
        }
        if file_urls:
            data["file_url"] = file_urls

        # `file[]` is multipart; everything else stays in `data`.
        file_payload: Optional[List[Tuple[str, Any]]] = None
        if files:
            file_payload = []
            for idx, (filename, content, content_type) in enumerate(files):
                file_payload.append(
                    (f"file[{idx}]", (filename, content, content_type))
                )

        return await self._request(
            "POST", "/signature_request/send",
            data=data,
            files=file_payload,
            context="send_signature_request",
        )

    async def cancel_signature_request(self, signature_request_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST", f"/signature_request/cancel/{signature_request_id}",
            data={},
            context=f"cancel_signature_request({signature_request_id})",
        )

    async def remind_signature_request(
        self,
        signature_request_id: str,
        email_address: str,
    ) -> Dict[str, Any]:
        return await self._request(
            "POST", f"/signature_request/remind/{signature_request_id}",
            data={"email_address": email_address},
            context=f"remind_signature_request({signature_request_id})",
        )

    async def download_signature_request(
        self,
        signature_request_id: str,
        file_type: str = "pdf",
    ) -> bytes:
        return await self._request(
            "GET", f"/signature_request/files/{signature_request_id}",
            params={"file_type": file_type},
            accept="application/pdf" if file_type == "pdf" else "application/zip",
            context=f"download_signature_request({signature_request_id})",
            expect_bytes=True,
        )

    # ── Templates ───────────────────────────────────────────────────────────

    async def list_templates(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        return await self._request(
            "GET", "/template/list",
            params={"page": page, "page_size": page_size},
            context="list_templates",
        )

    async def get_template(self, template_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/template/{template_id}",
            context=f"get_template({template_id})",
        )

    async def send_with_template(
        self,
        *,
        template_id: str,
        title: str,
        subject: str,
        message: str,
        signers: List[Dict[str, Any]],
        custom_fields: Optional[List[Dict[str, Any]]] = None,
        test_mode: bool = True,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "template_id": template_id,
            "title": title,
            "subject": subject,
            "message": message,
            "test_mode": test_mode,
            "signers": signers,
        }
        if custom_fields:
            data["custom_fields"] = custom_fields
        return await self._request(
            "POST", "/signature_request/send_with_template",
            data=data,
            context="send_with_template",
        )

    # ── Team ────────────────────────────────────────────────────────────────

    async def list_team_members(self, page: int = 1) -> Dict[str, Any]:
        return await self._request(
            "GET", "/team",
            params={"page": page},
            context="list_team_members",
        )

    # ── Unclaimed drafts ────────────────────────────────────────────────────

    async def list_unclaimed_drafts(self) -> Dict[str, Any]:
        return await self._request(
            "GET", "/unclaimed_draft/list",
            context="list_unclaimed_drafts",
        )

    # ── Embedded signing ────────────────────────────────────────────────────

    async def create_embedded_signature_request(
        self,
        *,
        client_id: str,
        title: str,
        signers: List[Dict[str, Any]],
        file_urls: Optional[List[str]] = None,
        test_mode: bool = True,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "client_id": client_id,
            "title": title,
            "test_mode": test_mode,
            "signers": signers,
        }
        if file_urls:
            data["file_url"] = file_urls
        return await self._request(
            "POST", "/signature_request/create_embedded",
            data=data,
            context="create_embedded_signature_request",
        )

    # ── API Apps ────────────────────────────────────────────────────────────

    async def create_api_app(
        self,
        *,
        name: str,
        domain: str,
        callback_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": name,
            "domains": [domain],
        }
        if callback_url:
            data["callback_url"] = callback_url
        return await self._request(
            "POST", "/api_app",
            data=data,
            context="create_api_app",
        )
