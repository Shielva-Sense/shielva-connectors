"""Shared utilities: ClientError classification, retry logic, metadata sanitization.

This module is the **single owner** of the boto-to-typed-exception bridge
(`classify_client_error`) and of the exponential-backoff retry helper
(`with_retry`). Importing them anywhere else is a SOC violation.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

import structlog

from exceptions import AwsS3AuthError, AwsS3Error, AwsS3NetworkError, AwsS3NotFound

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# OCP — retry constants live here, nowhere else.
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0

# Error codes that map to AwsS3NotFound — independent of HTTP status.
_NOT_FOUND_CODES = {
    "NoSuchKey",
    "NoSuchBucket",
    "404",
    "NotFound",
}

# Error codes that map to AwsS3AuthError.
_AUTH_ERROR_CODES = {
    "AccessDenied",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "ExpiredToken",
    "InvalidToken",
    "TokenRefreshRequired",
    "401",
    "403",
}


def classify_client_error(exc: Exception, context: str = "") -> AwsS3Error:
    """Map a `botocore.exceptions.ClientError` (or transport error) to a typed exception.

    Falls back to `AwsS3Error` when the error does not match a known class.
    Importing `botocore` here keeps the connector importable in environments
    where only the runtime tests are wired and `aiobotocore` is a soft dep.
    """
    try:
        from botocore.exceptions import (  # type: ignore
            BotoCoreError,
            ClientError,
            EndpointConnectionError,
        )
    except ImportError:  # pragma: no cover — botocore is in requirements.txt
        return AwsS3Error(f"{context}: {exc}" if context else str(exc))

    if isinstance(exc, EndpointConnectionError):
        return AwsS3NetworkError(
            f"endpoint unreachable{': ' + context if context else ''}: {exc}"
        )

    if isinstance(exc, ClientError):
        err: Dict[str, Any] = (
            exc.response.get("Error", {}) if hasattr(exc, "response") else {}
        )
        code = str(err.get("Code", "")) or ""
        message = err.get("Message", "") or str(exc)
        status = (
            int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            if hasattr(exc, "response")
            else 0
        )
        body = exc.response if hasattr(exc, "response") else {}

        if code in _NOT_FOUND_CODES or status == 404:
            return AwsS3NotFound(
                f"{code or 'NotFound'}{': ' + context if context else ''}: {message}",
                status_code=status,
                response_body=body,
            )
        if code in _AUTH_ERROR_CODES or status in (401, 403):
            return AwsS3AuthError(
                f"{code or 'AccessDenied'}{': ' + context if context else ''}: {message}",
                status_code=status,
                response_body=body,
            )
        return AwsS3Error(
            f"{code or 'S3Error'}{': ' + context if context else ''}: {message}",
            status_code=status,
            response_body=body,
        )

    if isinstance(exc, BotoCoreError):
        return AwsS3NetworkError(
            f"botocore transport error{': ' + context if context else ''}: {exc}"
        )

    return AwsS3Error(f"{context}: {exc}" if context else str(exc))


def sanitize_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """S3 user-metadata values must be ASCII strings. Coerce + drop empties.

    AWS silently rejects non-ASCII user metadata at signing time; doing this
    coercion up-front lets the connector surface the misuse before SigV4 fails.
    """
    if not metadata:
        return {}
    clean: Dict[str, str] = {}
    for k, v in metadata.items():
        if k is None or v is None:
            continue
        try:
            sk = str(k)
            sv = str(v)
        except Exception:
            continue
        # Touch — purely defensive; .encode raises if non-ASCII.
        sk.encode("ascii", "ignore")
        clean[sk] = sv
    return clean


def iso_utc(value: Any) -> Optional[str]:
    """Render a datetime / string LastModified value as ISO-8601 UTC.

    Used by the connector + normaliser so every outbound `last_modified` field
    has the same wire shape regardless of where it came from in the AWS SDK.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return None


async def with_retry(
    coro_fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> T:
    """Execute `coro_fn()` with exponential-backoff retry on transient errors.

    Retries `AwsS3NetworkError` and any non-auth/non-404 `AwsS3Error` whose
    `status_code` is 0 (transport) or 5xx. Auth / not-found errors are NOT retried.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except (AwsS3AuthError, AwsS3NotFound):
            raise
        except (AwsS3NetworkError, AwsS3Error) as exc:
            last_exc = exc
            transient = (
                isinstance(exc, AwsS3NetworkError)
                or exc.status_code == 0
                or (500 <= exc.status_code < 600)
            )
            if not transient or attempt == max_retries:
                raise
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "aws_s3.transient_error",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc
