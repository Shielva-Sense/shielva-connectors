from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import PlaidAuthError, PlaidError, PlaidRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _sha256_id(value: str) -> str:
    """Return the first 16 hex chars of the SHA-256 hash of value."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    PlaidAuthError is never retried — it requires human intervention.
    PlaidRateLimitError honours Retry-After when provided.
    """
    last_exc: PlaidError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except PlaidAuthError:
            raise
        except PlaidRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except PlaidError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def normalize_transaction(
    txn: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw Plaid transaction object into a ConnectorDocument."""
    transaction_id: str = txn.get("transaction_id", "")
    account_id: str = txn.get("account_id", "")
    amount: float = txn.get("amount", 0.0)
    iso_currency_code: str = txn.get("iso_currency_code") or txn.get("unofficial_currency_code") or "USD"
    date: str = txn.get("date", "")
    merchant_name: str = txn.get("merchant_name") or ""
    name: str = txn.get("name") or ""
    category: list[str] = txn.get("category") or []
    pending: bool = txn.get("pending", False)
    payment_channel: str = txn.get("payment_channel") or ""

    display_name = merchant_name or name
    title = f"{display_name}: {amount} {iso_currency_code}"

    content_parts = [
        f"Transaction ID: {transaction_id}",
        f"Account ID: {account_id}",
        f"Name: {name}",
        f"Merchant: {merchant_name}" if merchant_name else "",
        f"Amount: {amount} {iso_currency_code}",
        f"Date: {date}",
        f"Category: {', '.join(category)}" if category else "",
        f"Pending: {pending}",
        f"Payment channel: {payment_channel}" if payment_channel else "",
    ]
    content = "\n".join(p for p in content_parts if p)

    return ConnectorDocument(
        source_id=_sha256_id(transaction_id),
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "transaction_id": transaction_id,
            "account_id": account_id,
            "amount": amount,
            "iso_currency_code": iso_currency_code,
            "date": date,
            "merchant_name": merchant_name,
            "category": category,
            "pending": pending,
            "payment_channel": payment_channel,
        },
    )


def normalize_account(
    account: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw Plaid account object into a ConnectorDocument."""
    account_id: str = account.get("account_id", "")
    name: str = account.get("name") or ""
    official_name: str = account.get("official_name") or ""
    account_type: str = account.get("type") or ""
    subtype: str = account.get("subtype") or ""
    mask: str = account.get("mask") or ""
    balances: dict[str, Any] = account.get("balances") or {}
    current_balance: float | None = balances.get("current")
    available_balance: float | None = balances.get("available")
    iso_currency_code: str = balances.get("iso_currency_code") or balances.get("unofficial_currency_code") or "USD"

    title = f"{name} ({mask})" if mask else name

    content_parts = [
        f"Account ID: {account_id}",
        f"Name: {name}",
        f"Official name: {official_name}" if official_name else "",
        f"Type: {account_type}",
        f"Subtype: {subtype}" if subtype else "",
        f"Mask: {mask}" if mask else "",
        f"Current balance: {current_balance} {iso_currency_code}" if current_balance is not None else "",
        f"Available balance: {available_balance} {iso_currency_code}" if available_balance is not None else "",
    ]
    content = "\n".join(p for p in content_parts if p)

    return ConnectorDocument(
        source_id=_sha256_id(account_id),
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "account_id": account_id,
            "name": name,
            "official_name": official_name,
            "type": account_type,
            "subtype": subtype,
            "mask": mask,
            "current_balance": current_balance,
            "available_balance": available_balance,
            "iso_currency_code": iso_currency_code,
        },
    )
