"""Bandwidth HTTP client — Basic auth, retry, rate-limit handling.

Single owner of every outbound HTTP call. `connector.py` MUST go through this
client and never construct httpx requests directly.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Dict, Optional

import httpx

from ..exceptions import (
    BandwidthAuthError,
    BandwidthBadRequestError,
    BandwidthConflictError,
    BandwidthError,
    BandwidthNotFoundError,
    BandwidthRateLimitError,
    BandwidthServerError,
)


# Canonical base URLs (verified, see implementation_plan.md §3)
MESSAGING_BASE_URL = "https://messaging.bandwidth.com/api/v2"
VOICE_BASE_URL = "https://voice.bandwidth.com/api/v2"
DASHBOARD_BASE_URL = "https://dashboard.bandwidth.com/api"


def _basic_auth_header(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _raise_for_status(resp: httpx.Response) -> None:
    code = resp.status_code
    if code < 400:
        return
    try:
        body = resp.json()
    except ValueError:
        body = {"text": resp.text}
    message = body.get("description") or body.get("message") or resp.text or "Bandwidth error"
    if code == 400:
        raise BandwidthBadRequestError(message)
    if code in (401, 403):
        raise BandwidthAuthError(message)
    if code == 404:
        raise BandwidthNotFoundError(message)
    if code == 409:
        raise BandwidthConflictError(message)
    if code == 429:
        retry_after_s = float(resp.headers.get("Retry-After") or 1.0)
        raise BandwidthRateLimitError(message, retry_after_s=retry_after_s)
    if 500 <= code < 600:
        raise BandwidthServerError(message)
    raise BandwidthError(f"HTTP {code}: {message}")


class BandwidthHTTPClient:
    """Async HTTP client over httpx, scoped to a single Bandwidth account.

    Implements retry with exponential backoff for transient errors
    (429 / 5xx) and honours `Retry-After`.
    """

    def __init__(
        self,
        *,
        account_id: str,
        username: str,
        password: str,
        timeout_s: float = 60.0,
        max_retries: int = 3,
    ):
        self._account_id = account_id
        self._username = username
        self._password = password
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._auth_header = _basic_auth_header(username, password)

    @property
    def account_id(self) -> str:
        return self._account_id

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        content: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> httpx.Response:
        merged_headers = self._headers(headers)
        if content_type:
            merged_headers["Content-Type"] = content_type
        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt <= self._max_retries:
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    resp = await client.request(
                        method,
                        url,
                        headers=merged_headers,
                        json=json_body if content is None else None,
                        params=params,
                        content=content,
                    )
                _raise_for_status(resp)
                return resp
            except BandwidthRateLimitError as exc:
                last_exc = exc
                await asyncio.sleep(exc.retry_after_s)
            except BandwidthServerError as exc:
                last_exc = exc
                await asyncio.sleep(min(2 ** attempt, 8))
            except (BandwidthAuthError, BandwidthBadRequestError,
                    BandwidthNotFoundError, BandwidthConflictError):
                raise
            attempt += 1
        assert last_exc is not None
        raise last_exc

    # ── Surface helpers ────────────────────────────────────────────────

    def messaging_url(self, path: str) -> str:
        return f"{MESSAGING_BASE_URL}/users/{self._account_id}{path}"

    def voice_url(self, path: str) -> str:
        return f"{VOICE_BASE_URL}/accounts/{self._account_id}{path}"

    def dashboard_url(self, path: str) -> str:
        return f"{DASHBOARD_BASE_URL}/accounts/{self._account_id}{path}"
