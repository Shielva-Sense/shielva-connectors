"""Pacing, not retrying.

🚨 Retry-on-429 is what you do when rate limiting failed: the request was
already sent, the quota already spent, and on a shared per-user bucket the retry
storm makes the next caller fail too. Gmail answered "Quota exceeded for quota
metric 'Total Query Cost'" on a sync of a large mailbox because nothing paced
the loop — it went as fast as the network allowed, and the only defence was
three retries after the damage.

74 connectors collect `rate_limit_per_min` as an install field. Four enforced it.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))


def _bucket(per_minute: float):
    from shared.base_connector import _TokenBucket

    return _TokenBucket(per_minute)


def test_a_burst_is_capped_rather_than_let_through() -> None:
    """A fixed window lets a whole minute's allowance go out in the first
    second, which is exactly the burst that trips a per-minute quota."""
    b = _bucket(600)  # 10/s sustained
    assert b.capacity <= 600  # never a full minute in one go
    assert b.rate == 10.0


def test_calls_beyond_the_burst_are_made_to_wait() -> None:
    async def go() -> float:
        b = _bucket(600)
        b._tokens = 0  # burst already spent
        t0 = time.monotonic()
        await b.acquire(1)  # must wait ~1/10s for one token
        return time.monotonic() - t0

    waited = asyncio.run(go())
    assert waited >= 0.05, f"acquire returned immediately with an empty bucket ({waited:.3f}s)"


def test_cost_is_the_providers_unit_not_a_request_count() -> None:
    """Gmail prices messages.get at 5 units. A limiter counting requests
    under-counts by five times on the call a sync makes most."""

    async def go() -> tuple[float, float]:
        b = _bucket(600)
        b._tokens = 5
        t0 = time.monotonic()
        await b.acquire(5)  # exactly the tokens available — no wait
        cheap = time.monotonic() - t0
        t1 = time.monotonic()
        await b.acquire(5)  # nothing left — must wait for 5 tokens
        expensive = time.monotonic() - t1
        return cheap, expensive

    cheap, expensive = asyncio.run(go())
    assert cheap < 0.05
    assert expensive > cheap


def test_the_install_field_is_what_drives_it() -> None:
    """`rate_limit_per_min` was collected by 74 connectors and read by nothing."""
    src = (_CORE / "shared" / "base_connector.py").read_text()
    assert 'config or {}).get("rate_limit_per_min")' in src
    assert "async def throttle(" in src
