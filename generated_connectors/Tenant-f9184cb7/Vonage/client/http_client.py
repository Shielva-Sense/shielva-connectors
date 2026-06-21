"""Vonage HTTP client — single owner of every outbound HTTP call.

`connector.py` MUST go through this client; never constructs httpx requests directly.

Vonage is federated across two base URLs:
  - REST (https://rest.nexmo.com)  — Account, SMS, Numbers, Search, Verify v1
  - API  (https://api.nexmo.com)   — Voice (/v1/calls), Verify v2, Applications

Auth has two modes:
  1. HTTP Basic over api_key:api_secret — for SMS / Account / Numbers / Verify / Applications.
     Many of these endpoints also accept api_key / api_secret as form fields; we send both
     so any internal routing path inside Vonage still authenticates.
  2. RS256 JWT signed with application_id + private_key — for Voice / Messages / Conversations.

Retry policy: bounded retries on 5xx + Retry-After honour on 429.
Envelope errors (HTTP 200 with non-zero `status`) are parsed and surfaced as VonageError subclasses.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    VonageAuthError,
    VonageBadRequestError,
    VonageConflictError,
    VonageConfigError,
    VonageError,
    VonageInsufficientFunds,
    VonageNotFoundError,
    VonageRateLimitError,
    VonageServerError,
)
from helpers.utils import basic_auth_header, mint_vonage_jwt


logger = structlog.get_logger(__name__)


# Canonical base URLs (verified — implementation_plan.md §1)
REST_BASE_URL: str = "https://rest.nexmo.com"
API_BASE_URL: str = "https://api.nexmo.com"


def _raise_for_status(resp: httpx.Response) -> None:
    code = resp.status_code
    if code < 400:
        return
    try:
        body = resp.json()
        if not isinstance(body, dict):
            body = {"data": body}
    except ValueError:
        body = {"text": resp.text}
    message = (
        body.get("error_title")
        or body.get("error-text")
        or body.get("title")
        or body.get("detail")
        or body.get("message")
        or resp.text
        or f"Vonage HTTP {code}"
    )
    if code == 400:
        raise VonageBadRequestError(message, status_code=code, response_body=body)
    if code in (401, 403):
        raise VonageAuthError(message, status_code=code, response_body=body)
    if code == 402:
        raise VonageInsufficientFunds(message, status_code=code, response_body=body)
    if code == 404:
        raise VonageNotFoundError(message, status_code=code, response_body=body)
    if code == 409:
        raise VonageConflictError(message, status_code=code, response_body=body)
    if code == 429:
        retry_after_s = float(resp.headers.get("Retry-After") or 1.0)
        raise VonageRateLimitError(
            message, status_code=code, response_body=body, retry_after_s=retry_after_s
        )
    if 500 <= code < 600:
        raise VonageServerError(message, status_code=code, response_body=body)
    raise VonageError(f"HTTP {code}: {message}", status_code=code, response_body=body)


def _check_envelope(body: Any) -> None:
    """Inspect HTTP-200 SMS / Verify envelopes for non-zero `status` fields."""
    if not isinstance(body, dict):
        return
    # SMS: messages[].status
    for msg in body.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        mstatus = str(msg.get("status", "0"))
        if mstatus == "0":
            continue
        err = msg.get("error-text") or f"SMS envelope status {mstatus}"
        if mstatus == "9":
            raise VonageInsufficientFunds(err, status_code=402, response_body=body)
        if mstatus in {"4", "14"}:
            raise VonageAuthError(err, status_code=401, response_body=body)
        raise VonageError(err, status_code=400, response_body=body)

    # Verify v1: top-level `status` is "0" on success.
    # Skip if `messages`/`calls` keys present (already handled above or different envelope).
    if "status" in body and "messages" not in body and "calls" not in body:
        vstatus = str(body.get("status", "0"))
        # status "0" = success. Voice calls return string statuses like "started" — accept.
        if vstatus not in ("0", "", "started", "answered", "ringing", "completed"):
            err = body.get("error_text") or body.get("error-text") or f"envelope status {vstatus}"
            if vstatus == "9":
                raise VonageInsufficientFunds(err, status_code=402, response_body=body)
            if vstatus in {"4", "14"}:
                raise VonageAuthError(err, status_code=401, response_body=body)
            raise VonageError(err, status_code=400, response_body=body)


class VonageHTTPClient:
    """Async HTTP client scoped to a single Vonage account."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        application_id: str = "",
        private_key: str = "",
        rest_base_url: str = REST_BASE_URL,
        api_base_url: str = API_BASE_URL,
        timeout_s: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._application_id = application_id
        self._private_key = private_key
        self._rest_base = rest_base_url.rstrip("/")
        self._api_base = api_base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._basic_header = basic_auth_header(api_key, api_secret)

    # ── URL builders ─────────────────────────────────────────────────────

    @property
    def rest_base(self) -> str:
        return self._rest_base

    @property
    def api_base(self) -> str:
        return self._api_base

    def rest_url(self, path: str) -> str:
        return f"{self._rest_base}{path}"

    def api_url(self, path: str) -> str:
        return f"{self._api_base}{path}"

    # ── Credential helpers ───────────────────────────────────────────────

    def credential_form(self) -> Dict[str, str]:
        """Legacy form-style auth fields — sent alongside the Basic header."""
        return {"api_key": self._api_key, "api_secret": self._api_secret}

    def credential_params(self) -> Dict[str, str]:
        """Same fields, useful for GET endpoints that take them in the query string."""
        return {"api_key": self._api_key, "api_secret": self._api_secret}

    def _bearer_jwt_header(self) -> str:
        """Mint a fresh JWT for Voice / Messages calls."""
        if not self._application_id or not self._private_key:
            raise VonageConfigError(
                "Voice / Messages calls require both application_id and private_key. "
                "Re-install the connector with these fields populated."
            )
        token = mint_vonage_jwt(self._application_id, self._private_key)
        return f"Bearer {token}"

    # ── Header builders ──────────────────────────────────────────────────

    def _headers_basic(
        self,
        *,
        json_body: bool = False,
        extra: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        h = {
            "Authorization": self._basic_header,
            "Accept": "application/json",
        }
        if json_body:
            h["Content-Type"] = "application/json"
        if extra:
            h.update(extra)
        return h

    def _headers_jwt(
        self,
        *,
        json_body: bool = True,
        extra: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        h = {
            "Authorization": self._bearer_jwt_header(),
            "Accept": "application/json",
        }
        if json_body:
            h["Content-Type"] = "application/json"
        if extra:
            h.update(extra)
        return h

    # ── Core request method ──────────────────────────────────────────────

    async def request(
        self,
        method: str,
        url: str,
        *,
        auth_mode: str = "basic",  # "basic" | "jwt" | "none"
        json_body: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        check_envelope: bool = False,
    ) -> httpx.Response:
        """Perform a single Vonage HTTP request with retry + envelope-check.

        Args:
            auth_mode: "basic" (api_key/secret), "jwt" (application_id+private_key), "none".
            check_envelope: when True, parse HTTP-200 envelopes for non-zero `status` codes.
        """
        if auth_mode == "basic":
            merged = self._headers_basic(json_body=json_body is not None, extra=headers)
        elif auth_mode == "jwt":
            merged = self._headers_jwt(json_body=json_body is not None, extra=headers)
        elif auth_mode == "none":
            merged = {"Accept": "application/json"}
            if json_body is not None:
                merged["Content-Type"] = "application/json"
            if headers:
                merged.update(headers)
        else:
            raise ValueError(f"unknown auth_mode: {auth_mode}")

        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt <= self._max_retries:
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    resp = await client.request(
                        method,
                        url,
                        headers=merged,
                        json=json_body,
                        data=data,
                        params=params,
                    )
                _raise_for_status(resp)
                if check_envelope:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = None
                    if body is not None:
                        _check_envelope(body)
                return resp
            except VonageRateLimitError as exc:
                logger.warning(
                    "vonage.http.rate_limited",
                    method=method,
                    url=url,
                    retry_after_s=exc.retry_after_s,
                )
                last_exc = exc
                await asyncio.sleep(exc.retry_after_s)
            except VonageServerError as exc:
                logger.warning(
                    "vonage.http.server_error",
                    method=method,
                    url=url,
                    attempt=attempt,
                )
                last_exc = exc
                await asyncio.sleep(min(2 ** attempt, 8))
            except (
                VonageAuthError,
                VonageBadRequestError,
                VonageNotFoundError,
                VonageConflictError,
                VonageInsufficientFunds,
                VonageConfigError,
            ):
                # Non-retryable — propagate immediately.
                raise
            except httpx.TimeoutException as exc:
                logger.warning("vonage.http.timeout", method=method, url=url, attempt=attempt)
                last_exc = exc
                await asyncio.sleep(min(2 ** attempt, 8))
            attempt += 1
        assert last_exc is not None
        raise last_exc
