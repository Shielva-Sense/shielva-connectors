"""All Sage Intacct XML-Gateway HTTP calls — zero business logic.

The Intacct gateway is a single POST endpoint
(`https://api.intacct.com/ia/xml/xmlgw.phtml`) that accepts a full XML
envelope and replies with an XML envelope. This client:

  1. Sends the envelope with ``Content-Type: application/xml``.
  2. Retries on 429 + 5xx + transport errors with exponential backoff + jitter.
  3. Parses the response (via ``helpers.xml_builder.parse_envelope``) and
     raises typed exceptions on control-level / function-level failures.

It does NOT build envelopes — that belongs to ``helpers/xml_builder.py`` — and
it does NOT know about Intacct objects. The connector orchestrates both.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    SageIntacctAuthError,
    SageIntacctBadRequestError,
    SageIntacctError,
    SageIntacctNetworkError,
    SageIntacctNotFoundError,
    SageIntacctRateLimitError,
    SageIntacctValidationError,
)
from helpers.xml_builder import parse_envelope

logger = structlog.get_logger(__name__)

_DEFAULT_BASE = "https://api.intacct.com/ia/xml/xmlgw.phtml"
_DEFAULT_TIMEOUT_S = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 16.0

# Intacct error codes (errorno prefix) that indicate an auth problem rather
# than a validation/business problem. Source: Intacct DTD docs (XL03 = auth /
# login / session failures).
_AUTH_ERROR_PREFIXES = ("XL03",)


class SageIntacctHTTPClient:
    """Thin async XML-over-HTTP client for the Intacct gateway."""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ):
        self._base_url: str = (base_url or _DEFAULT_BASE).rstrip("/") or _DEFAULT_BASE
        self._timeout: float = timeout

    # ── Public surface ────────────────────────────────────────────────────

    async def send_envelope(
        self,
        envelope_xml: str,
        context: str = "send_envelope",
    ) -> Dict[str, Any]:
        """POST one XML envelope to the gateway and return the parsed dict.

        Raises:
            SageIntacctAuthError: on HTTP 401 / 403 or XML XL03* failures.
            SageIntacctRateLimitError: on 429 after retries are exhausted.
            SageIntacctNetworkError: on transport / repeated 5xx failures.
            SageIntacctValidationError: on XML failure that is NOT auth-related.
            SageIntacctError: on any other unrecognised non-success state.
        """
        response_text = await self._post_with_retry(envelope_xml, context)
        parsed = parse_envelope(response_text)
        self._raise_on_failure(parsed, context)
        return parsed

    # ── Transport ─────────────────────────────────────────────────────────

    async def _post_with_retry(self, envelope_xml: str, context: str) -> str:
        """POST to the gateway with retry on 429 / 5xx / transport errors."""
        last_exc: Optional[Exception] = None
        attempt = 0
        while attempt <= _MAX_RETRIES:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        self._base_url,
                        content=envelope_xml.encode("utf-8"),
                        headers={
                            "Content-Type": "application/xml",
                            "Accept": "application/xml",
                        },
                    )
                status = response.status_code
                if status == 401:
                    raise SageIntacctAuthError(
                        f"401 Unauthorized: {context}",
                        status_code=401,
                        response_body={"text": response.text},
                    )
                if status == 403:
                    raise SageIntacctAuthError(
                        f"403 Forbidden: {context}",
                        status_code=403,
                        response_body={"text": response.text},
                    )
                if status == 404:
                    raise SageIntacctNotFoundError(
                        f"404 Not Found: {context}",
                        status_code=404,
                        response_body={"text": response.text},
                    )
                if status == 400:
                    raise SageIntacctBadRequestError(
                        f"400 Bad Request: {context}",
                        status_code=400,
                        response_body={"text": response.text},
                    )
                if status == 429 or 500 <= status < 600:
                    retry_after = self._parse_retry_after(response.headers)
                    last_exc = (
                        SageIntacctRateLimitError(
                            f"HTTP 429 during {context}",
                            retry_after_s=retry_after,
                        )
                        if status == 429
                        else SageIntacctNetworkError(
                            f"HTTP {status} during {context}",
                            status_code=status,
                            response_body={"text": response.text},
                        )
                    )
                    if attempt == _MAX_RETRIES:
                        raise last_exc
                    logger.warning(
                        "sage_intacct.http.retry",
                        status=status,
                        attempt=attempt + 1,
                        context=context,
                    )
                    await self._sleep_backoff(attempt)
                    attempt += 1
                    continue
                if status >= 400:
                    raise SageIntacctError(
                        f"HTTP {status} during {context}",
                        status_code=status,
                        response_body={"text": response.text},
                    )
                return response.text
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = SageIntacctNetworkError(
                    f"Transport error during {context}: {exc}",
                    status_code=0,
                )
                if attempt == _MAX_RETRIES:
                    raise last_exc
                logger.warning(
                    "sage_intacct.http.transport_retry",
                    attempt=attempt + 1,
                    context=context,
                    error=str(exc),
                )
                await self._sleep_backoff(attempt)
                attempt += 1
                continue

        # Unreachable — every loop iteration either returns or raises.
        assert last_exc is not None
        raise last_exc

    @staticmethod
    async def _sleep_backoff(attempt: int) -> None:
        delay = min(
            _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.5),
            _BACKOFF_MAX,
        )
        await asyncio.sleep(delay)

    @staticmethod
    def _parse_retry_after(headers: httpx.Headers) -> float:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if not raw:
            return 5.0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 5.0

    # ── XML-level failure mapping ─────────────────────────────────────────

    @staticmethod
    def _raise_on_failure(parsed: Dict[str, Any], context: str) -> None:
        """Translate XML ``<status>failure</status>`` blocks to typed exceptions."""
        if parsed.get("control_status") == "failure":
            err = parsed.get("control_error") or {}
            errorno = err.get("errorno", "")
            message = err.get("description") or f"Control failure during {context}"
            if any(errorno.startswith(p) for p in _AUTH_ERROR_PREFIXES):
                raise SageIntacctAuthError(message, response_body=err)
            raise SageIntacctValidationError(message, response_body=err)

        for fn in parsed.get("functions", []):
            if fn.get("status") == "failure":
                err = fn.get("error") or {}
                errorno = err.get("errorno", "")
                message = (
                    err.get("description")
                    or err.get("description2")
                    or f"Function {fn.get('function_name') or '?'} failed during {context}"
                )
                if any(errorno.startswith(p) for p in _AUTH_ERROR_PREFIXES):
                    raise SageIntacctAuthError(message, response_body=err)
                raise SageIntacctValidationError(message, response_body=err)
