"""All Dropbox API HTTP calls — zero business logic, zero normalization.

Dropbox v2 quirks the client owns end-to-end:

  - **Two hosts**: ``https://api.dropboxapi.com/2`` for JSON-RPC endpoints,
    ``https://content.dropboxapi.com/2`` for upload/download. Content endpoints
    carry their JSON args in the ``Dropbox-API-Arg`` header (NOT the body).
  - **Empty bodies must be sent as JSON ``null``**, not an empty dict
    (e.g. ``/users/get_current_account``).
  - **404 ⇒ 409**: Dropbox encodes "not found" as HTTP 409 with a
    ``not_found`` tag inside the error JSON.
  - **Retry**: 429/5xx with exponential backoff. 429 honours ``Retry-After``
    (seconds, capped at ``_MAX_RETRY_AFTER``).
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    DropboxAuthError,
    DropboxBadRequestError,
    DropboxConflictError,
    DropboxError,
    DropboxNetworkError,
    DropboxNotFoundError,
    DropboxRateLimitError,
    DropboxServerError,
)

logger = structlog.get_logger(__name__)

_API_BASE = "https://api.dropboxapi.com/2"
_CONTENT_BASE = "https://content.dropboxapi.com/2"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds
_MAX_RETRY_AFTER = 30.0  # seconds — cap on Dropbox-supplied Retry-After


class DropboxHTTPClient:
    """Thin async HTTP client for the Dropbox API v2.

    All methods are awaitable and return raw response dicts (or binary-aware
    envelopes for content endpoints). Auth header + retry + error mapping are
    owned here — ``connector.py`` only orchestrates.
    """

    def __init__(
        self,
        access_token: str = "",
        api_base: str = _API_BASE,
        content_base: str = _CONTENT_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._access_token = access_token or ""
        self._api_base = api_base.rstrip("/")
        self._content_base = content_base.rstrip("/")
        self._timeout = timeout

    # ── Internal: auth / error mapping / retry ─────────────────────────────

    def _auth_headers(self) -> Dict[str, str]:
        if not self._access_token:
            return {}
        return {"Authorization": f"Bearer {self._access_token}"}

    def set_access_token(self, token: str) -> None:
        """Swap in a refreshed access token without rebuilding the client."""
        self._access_token = token or ""

    def _classify_error(
        self,
        status: int,
        body: Any,
        retry_after_header: Optional[str],
        context: str,
    ) -> DropboxError:
        """Map a Dropbox error response to a typed exception."""
        body_dict: Dict[str, Any] = body if isinstance(body, dict) else {"raw": body}
        summary = ""
        tag = ""
        if isinstance(body, dict):
            summary = (
                body.get("error_summary")
                or body.get("user_message", {}).get("text", "")
                if isinstance(body.get("user_message"), dict)
                else body.get("error_summary", "")
            )
            err_inner = body.get("error")
            if isinstance(err_inner, dict):
                tag = err_inner.get(".tag", "") or tag
                # Dropbox often nests another tagged variant inside ``error.path``.
                path_obj = err_inner.get("path")
                if isinstance(path_obj, dict):
                    tag = tag or path_obj.get(".tag", "")
            summary = summary or body.get("error_description") or ""
        if not summary:
            summary = str(body)

        ctx = f": {context}" if context else ""

        if status == 400:
            return DropboxBadRequestError(
                f"400 Bad Request{ctx}: {summary}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 401:
            return DropboxAuthError(
                f"401 Unauthorized{ctx}: {summary}",
                status_code=401,
                response_body=body_dict,
            )
        if status == 403:
            return DropboxAuthError(
                f"403 Forbidden{ctx}: {summary}",
                status_code=403,
                response_body=body_dict,
            )
        if status == 404:
            return DropboxNotFoundError(
                f"404 Not Found{ctx}: {summary}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            if "not_found" in tag or "not_found" in summary:
                return DropboxNotFoundError(
                    f"409 not_found{ctx}: {summary}",
                    status_code=409,
                    response_body=body_dict,
                )
            return DropboxConflictError(
                f"409 Conflict{ctx}: {summary}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = _parse_retry_after(retry_after_header, body)
            return DropboxRateLimitError(
                f"429 Too Many Requests{ctx}: {summary}",
                retry_after_s=retry_after,
                response_body=body_dict,
            )
        if status >= 500:
            return DropboxServerError(
                f"{status} Server Error{ctx}: {summary}",
                status_code=status,
                response_body=body_dict,
            )
        return DropboxError(
            f"HTTP {status}{ctx}: {summary}",
            status_code=status,
            response_body=body_dict,
        )

    async def _request_rpc(
        self,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """POST to a Dropbox RPC endpoint with JSON body. Retries 429/5xx + transport."""
        url = f"{self._api_base}{path}" if not path.startswith("http") else path
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            **self._auth_headers(),
        }
        # Dropbox expects ``null`` for empty-body endpoints (not ``{}``).
        encoded_body: Any = body if body is not None else None

        last_exc: Optional[BaseException] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        url,
                        headers=headers,
                        content=json.dumps(encoded_body) if encoded_body is not None else "null",
                    )
                status = response.status_code
                if status == 200:
                    if not response.content:
                        return {}
                    try:
                        return response.json()
                    except Exception:
                        return {"raw": response.text}
                if status in (429,) or status >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _retry_delay(
                            attempt=attempt,
                            status=status,
                            retry_after_header=response.headers.get("Retry-After"),
                        )
                        logger.warning(
                            "dropbox.http.retry",
                            status=status,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                # Non-retryable or final attempt — raise mapped exception.
                try:
                    err_body: Any = response.json()
                except Exception:
                    err_body = {"raw": response.text}
                raise self._classify_error(
                    status=status,
                    body=err_body,
                    retry_after_header=response.headers.get("Retry-After"),
                    context=context,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "dropbox.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise DropboxNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc is not None:
            raise DropboxNetworkError(str(last_exc)) from last_exc
        raise DropboxNetworkError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    async def _request_content(
        self,
        path: str,
        *,
        args: Dict[str, Any],
        body: Optional[bytes] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """POST to a Dropbox content endpoint.

        Dropbox content endpoints (``/files/upload``, ``/files/download``) ship
        the JSON args in the ``Dropbox-API-Arg`` header and the binary payload
        in the body. The response carries the JSON metadata in the
        ``Dropbox-API-Result`` header and the binary in the body.

        Returns ``{"metadata": <parsed result>, "content_b64": <base64-bytes>}``
        for downloads, or the parsed result dict for uploads.
        """
        url = f"{self._content_base}{path}" if not path.startswith("http") else path
        headers: Dict[str, str] = {
            "Dropbox-API-Arg": json.dumps(args),
            **self._auth_headers(),
        }
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"

        last_exc: Optional[BaseException] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        url,
                        headers=headers,
                        content=body if body is not None else b"",
                    )
                status = response.status_code
                if status == 200:
                    result_header = response.headers.get("Dropbox-API-Result")
                    metadata: Dict[str, Any] = {}
                    if result_header:
                        try:
                            metadata = json.loads(result_header)
                        except Exception:
                            metadata = {"raw": result_header}
                    if body is None:
                        # download
                        return {
                            "metadata": metadata,
                            "content_b64": base64.b64encode(response.content).decode("ascii"),
                            "size": len(response.content),
                        }
                    # upload — payload may be in the body (most common) or in the
                    # Dropbox-API-Result header. Prefer the body, fall back to
                    # the header so we never silently drop the response.
                    if response.content:
                        try:
                            return response.json()
                        except Exception:
                            return metadata or {}
                    return metadata or {}
                if status in (429,) or status >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _retry_delay(
                            attempt=attempt,
                            status=status,
                            retry_after_header=response.headers.get("Retry-After"),
                        )
                        logger.warning(
                            "dropbox.content.retry",
                            status=status,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                try:
                    err_body: Any = response.json()
                except Exception:
                    err_body = {"raw": response.text}
                raise self._classify_error(
                    status=status,
                    body=err_body,
                    retry_after_header=response.headers.get("Retry-After"),
                    context=context,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    await asyncio.sleep(delay)
                    continue
                raise DropboxNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc is not None:
            raise DropboxNetworkError(str(last_exc)) from last_exc
        raise DropboxNetworkError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── Files (RPC) ────────────────────────────────────────────────────────

    async def list_folder(
        self,
        path: str = "",
        recursive: bool = False,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """POST /files/list_folder — page through folder entries."""
        return await self._request_rpc(
            "/files/list_folder",
            body={
                "path": path,
                "recursive": recursive,
                "limit": limit,
                "include_media_info": False,
                "include_deleted": False,
                "include_has_explicit_shared_members": False,
            },
            context="list_folder",
        )

    async def list_folder_continue(self, cursor: str) -> Dict[str, Any]:
        """POST /files/list_folder/continue — cursor-based pagination."""
        return await self._request_rpc(
            "/files/list_folder/continue",
            body={"cursor": cursor},
            context="list_folder_continue",
        )

    async def get_metadata(self, path: str) -> Dict[str, Any]:
        """POST /files/get_metadata."""
        return await self._request_rpc(
            "/files/get_metadata",
            body={
                "path": path,
                "include_media_info": False,
                "include_deleted": False,
                "include_has_explicit_shared_members": False,
            },
            context=f"get_metadata({path})",
        )

    async def copy_file(
        self,
        from_path: str,
        to_path: str,
        autorename: bool = False,
    ) -> Dict[str, Any]:
        """POST /files/copy_v2."""
        return await self._request_rpc(
            "/files/copy_v2",
            body={
                "from_path": from_path,
                "to_path": to_path,
                "autorename": autorename,
                "allow_shared_folder": False,
                "allow_ownership_transfer": False,
            },
            context=f"copy_file({from_path}->{to_path})",
        )

    async def move_file(
        self,
        from_path: str,
        to_path: str,
        autorename: bool = False,
    ) -> Dict[str, Any]:
        """POST /files/move_v2."""
        return await self._request_rpc(
            "/files/move_v2",
            body={
                "from_path": from_path,
                "to_path": to_path,
                "autorename": autorename,
                "allow_shared_folder": False,
                "allow_ownership_transfer": False,
            },
            context=f"move_file({from_path}->{to_path})",
        )

    async def delete_file(self, path: str) -> Dict[str, Any]:
        """POST /files/delete_v2."""
        return await self._request_rpc(
            "/files/delete_v2",
            body={"path": path},
            context=f"delete_file({path})",
        )

    async def create_folder(
        self,
        path: str,
        autorename: bool = False,
    ) -> Dict[str, Any]:
        """POST /files/create_folder_v2."""
        return await self._request_rpc(
            "/files/create_folder_v2",
            body={"path": path, "autorename": autorename},
            context=f"create_folder({path})",
        )

    async def search(
        self,
        query: str,
        max_results: int = 100,
        path: str = "",
    ) -> Dict[str, Any]:
        """POST /files/search_v2 — full-text search."""
        options: Dict[str, Any] = {
            "max_results": max_results,
            "file_status": "active",
        }
        if path:
            options["path"] = path
        return await self._request_rpc(
            "/files/search_v2",
            body={"query": query, "options": options},
            context=f"search({query!r})",
        )

    async def list_revisions(
        self,
        path: str,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """POST /files/list_revisions."""
        return await self._request_rpc(
            "/files/list_revisions",
            body={"path": path, "mode": "path", "limit": limit},
            context=f"list_revisions({path})",
        )

    async def restore_revision(self, path: str, rev: str) -> Dict[str, Any]:
        """POST /files/restore."""
        return await self._request_rpc(
            "/files/restore",
            body={"path": path, "rev": rev},
            context=f"restore_revision({path}, rev={rev})",
        )

    # ── Files (content) ────────────────────────────────────────────────────

    async def download_file(self, path: str) -> Dict[str, Any]:
        """POST https://content.dropboxapi.com/2/files/download — binary download."""
        return await self._request_content(
            "/files/download",
            args={"path": path},
            body=None,
            context=f"download_file({path})",
        )

    async def upload_file(
        self,
        path: str,
        content: bytes,
        mode: str = "add",
        autorename: bool = False,
        mute: bool = False,
    ) -> Dict[str, Any]:
        """POST https://content.dropboxapi.com/2/files/upload — binary upload."""
        return await self._request_content(
            "/files/upload",
            args={
                "path": path,
                "mode": mode,
                "autorename": autorename,
                "mute": mute,
                "strict_conflict": False,
            },
            body=content,
            context=f"upload_file({path}, {len(content)}B)",
        )

    # ── Sharing ────────────────────────────────────────────────────────────

    async def create_shared_link(
        self,
        path: str,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /sharing/create_shared_link_with_settings."""
        body: Dict[str, Any] = {"path": path}
        if settings:
            body["settings"] = settings
        return await self._request_rpc(
            "/sharing/create_shared_link_with_settings",
            body=body,
            context=f"create_shared_link({path})",
        )

    async def list_shared_links(
        self,
        path: Optional[str] = None,
        cursor: Optional[str] = None,
        direct_only: bool = True,
    ) -> Dict[str, Any]:
        """POST /sharing/list_shared_links."""
        body: Dict[str, Any] = {"direct_only": direct_only}
        if path is not None:
            body["path"] = path
        if cursor is not None:
            body["cursor"] = cursor
        return await self._request_rpc(
            "/sharing/list_shared_links",
            body=body,
            context="list_shared_links",
        )

    # ── Users / account ────────────────────────────────────────────────────

    async def get_current_account(self) -> Dict[str, Any]:
        """POST /users/get_current_account — body must be ``null``."""
        return await self._request_rpc(
            "/users/get_current_account",
            body=None,
            context="get_current_account",
        )

    async def get_account(self, account_id: str) -> Dict[str, Any]:
        """POST /users/get_account."""
        return await self._request_rpc(
            "/users/get_account",
            body={"account_id": account_id},
            context=f"get_account({account_id})",
        )

    async def get_space_usage(self) -> Dict[str, Any]:
        """POST /users/get_space_usage — body must be ``null``."""
        return await self._request_rpc(
            "/users/get_space_usage",
            body=None,
            context="get_space_usage",
        )

    # ── Auth ───────────────────────────────────────────────────────────────

    async def token_revoke(self) -> Dict[str, Any]:
        """POST /auth/token/revoke — disconnects the current token."""
        return await self._request_rpc(
            "/auth/token/revoke",
            body=None,
            context="token_revoke",
        )


# ── Module-level helpers ──────────────────────────────────────────────────


def _parse_retry_after(header: Optional[str], body: Any) -> float:
    """Parse a Dropbox ``Retry-After`` (seconds), capped at ``_MAX_RETRY_AFTER``."""
    candidate: Optional[str] = header
    if not candidate and isinstance(body, dict):
        # Dropbox sometimes encodes the wait as ``error.retry_after``.
        err = body.get("error")
        if isinstance(err, dict):
            candidate = str(err.get("retry_after", "")) or candidate
    try:
        value = float(candidate) if candidate else 0.0
    except (ValueError, TypeError):
        value = 0.0
    if value <= 0:
        return _BACKOFF_BASE
    return min(value, _MAX_RETRY_AFTER)


def _retry_delay(*, attempt: int, status: int, retry_after_header: Optional[str]) -> float:
    """Choose retry delay — honour Retry-After on 429, exponential otherwise."""
    if status == 429:
        return _parse_retry_after(retry_after_header, None)
    return _BACKOFF_BASE * (2 ** attempt)
