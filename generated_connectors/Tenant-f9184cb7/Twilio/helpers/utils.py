from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import TwilioAuthError, TwilioError, TwilioRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(sid: str) -> str:
    """Return first 16 hex chars of the SHA-256 digest of sid."""
    return hashlib.sha256(sid.encode()).hexdigest()[:16]


def normalize_message(
    msg: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Normalize a raw Twilio message object to a ConnectorDocument."""
    sid: str = msg.get("sid", "")
    from_: str = msg.get("from", "") or msg.get("from_", "")
    to: str = msg.get("to", "")
    body: str = msg.get("body") or ""
    direction: str = msg.get("direction", "")
    status: str = msg.get("status", "")
    date_sent: str = msg.get("date_sent", "") or msg.get("date_created", "")
    num_segments: str = str(msg.get("num_segments", "1"))
    price: str = str(msg.get("price", "")) if msg.get("price") is not None else ""

    return ConnectorDocument(
        source_id=_short_id(sid),
        title=f"SMS from {from_} to {to}",
        content=body,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://console.twilio.com/us1/monitor/logs/sms/{sid}",
        metadata={
            "sid": sid,
            "from": from_,
            "to": to,
            "direction": direction,
            "status": status,
            "date_sent": date_sent,
            "num_segments": num_segments,
            "price": price,
        },
    )


def normalize_call(
    call: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Normalize a raw Twilio call object to a ConnectorDocument."""
    sid: str = call.get("sid", "")
    from_: str = call.get("from", "") or call.get("from_", "")
    to: str = call.get("to", "")
    direction: str = call.get("direction", "")
    status: str = call.get("status", "")
    duration: str = str(call.get("duration", "0"))
    start_time: str = call.get("start_time", "") or call.get("date_created", "")
    price: str = str(call.get("price", "")) if call.get("price") is not None else ""

    return ConnectorDocument(
        source_id=_short_id(sid),
        title=f"Call from {from_} to {to} ({duration}s)",
        content=f"Direction: {direction}, Status: {status}, Duration: {duration}s, Price: {price}",
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://console.twilio.com/us1/monitor/logs/calls/{sid}",
        metadata={
            "sid": sid,
            "from": from_,
            "to": to,
            "direction": direction,
            "status": status,
            "duration": duration,
            "start_time": start_time,
            "price": price,
        },
    )


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: TwilioError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except TwilioAuthError:
            raise
        except TwilioRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except TwilioError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
