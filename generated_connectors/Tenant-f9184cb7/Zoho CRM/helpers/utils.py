from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ZohoCRMAuthError, ZohoCRMError, ZohoCRMRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

ZOHO_APP_BASE = "https://crm.zoho.com"  # fallback source_url base

T = TypeVar("T")


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
    last_exc: ZohoCRMError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ZohoCRMAuthError:
            raise
        except ZohoCRMRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ZohoCRMError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_id(module: str, record_id: str) -> str:
    """Compute a stable 16-char hex ID from module + record_id."""
    raw = f"{module}:{record_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_record(
    module: str,
    record: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    data_center: str = "com",
) -> ConnectorDocument:
    """Convert a raw Zoho CRM record into a ConnectorDocument.

    Works for any module (Leads, Contacts, Deals, Accounts, etc.).
    The stable source_id is SHA-256(module + ":" + record_id)[:16].
    """
    record_id: str = str(record.get("id", record.get("Id", "")))
    source_id = _stable_id(module, record_id) if record_id else ""

    # ── Title heuristics by module ─────────────────────────────────────────
    module_lower = module.lower()
    if module_lower == "leads":
        first = record.get("First_Name", "") or ""
        last = record.get("Last_Name", "") or ""
        company = record.get("Company", "") or ""
        full_name = f"{first} {last}".strip() or "Unknown Lead"
        title = f"Zoho Lead: {full_name}" + (f" — {company}" if company else "")
    elif module_lower == "contacts":
        first = record.get("First_Name", "") or ""
        last = record.get("Last_Name", "") or ""
        account = record.get("Account_Name", {})
        account_name = account.get("name", "") if isinstance(account, dict) else str(account or "")
        full_name = f"{first} {last}".strip() or "Unknown Contact"
        title = f"Zoho Contact: {full_name}" + (f" — {account_name}" if account_name else "")
    elif module_lower == "deals":
        deal_name = record.get("Deal_Name", "") or "Unnamed Deal"
        stage = record.get("Stage", "") or ""
        title = f"Zoho Deal: {deal_name}" + (f" — {stage}" if stage else "")
    elif module_lower == "accounts":
        account_name = record.get("Account_Name", "") or "Unnamed Account"
        title = f"Zoho Account: {account_name}"
    else:
        # Generic: use Name field or record_id
        name = record.get("Name", "") or record_id or "Unknown Record"
        title = f"Zoho {module}: {name}"

    # ── Build content from all scalar fields ──────────────────────────────
    content_parts: list[str] = [f"Record ID: {record_id}", f"Module: {module}"]
    for key, value in record.items():
        if key in ("id", "Id"):
            continue
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            content_parts.append(f"{key}: {value}")
        elif isinstance(value, dict) and "name" in value:
            content_parts.append(f"{key}: {value['name']}")

    dc = (data_center or "com").strip().lower()
    source_url = (
        f"https://crm.zoho.{dc}/crm/org/tab/{module}/{record_id}"
        if record_id
        else ""
    )

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "module": module,
            "zoho_record_id": record_id,
            "data_center": dc,
        },
    )


class CircuitBreaker:
    """Simple three-state circuit breaker (closed → open → half-open → closed)."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self._failures: int = 0
        self._state: str = "closed"
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                self._state = "half-open"
        return self._state

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()

    def on_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    @property
    def is_open(self) -> bool:
        return self.state == "open"
