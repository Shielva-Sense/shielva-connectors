"""Misc utilities for the Dropbox Sign connector.

`with_retry` is a thin retry wrapper. The HTTP client already retries
429 + 5xx internally; this helper catches transient errors that escape it
(e.g. JSON decode flakiness, intermittent provider hiccups). Auth errors
and 404s are NEVER retried — they are caller errors.
"""
import asyncio
from typing import Any, Awaitable, Callable, TypeVar

from exceptions import (
    DropboxSignAuthError,
    DropboxSignError,
    DropboxSignNotFoundError,
)

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Retry an async call with exponential backoff."""
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except (DropboxSignAuthError, DropboxSignNotFoundError):
            raise
        except DropboxSignError as exc:
            last_exc = exc
            if attempt >= max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def validate_signers(signers: Any) -> None:
    """Raise ValueError if `signers` is not a non-empty list of {name,email_address} dicts."""
    if not isinstance(signers, list) or not signers:
        raise ValueError("signers must be a non-empty list of {name, email_address} dicts")
    for idx, s in enumerate(signers):
        if not isinstance(s, dict):
            raise ValueError(f"signers[{idx}] must be a dict")
        if not s.get("email_address"):
            raise ValueError(f"signers[{idx}] missing 'email_address'")
        if not s.get("name"):
            raise ValueError(f"signers[{idx}] missing 'name'")


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
