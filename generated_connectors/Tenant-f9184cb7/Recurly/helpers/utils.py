from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import RecurlyAuthError, RecurlyError, RecurlyRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

RECURLY_APP_URL: str = "https://app.recurly.com"


def _short_id(value: str) -> str:
    """Return a 16-character hex digest (sha256 prefix) for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_account(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Recurly account object into a ConnectorDocument.

    Stable source_id: sha256("account:" + account_id)[:16]
    """
    account_id: str = raw.get("id", "") or raw.get("code", "")
    code: str = raw.get("code", "") or account_id
    email: str = raw.get("email", "") or ""
    first_name: str = (raw.get("first_name", "") or "").strip()
    last_name: str = (raw.get("last_name", "") or "").strip()
    company: str = (raw.get("company", "") or "").strip()
    state: str = raw.get("state", "") or ""
    username: str = raw.get("username", "") or ""
    created_at: Any = raw.get("created_at", None)
    updated_at: Any = raw.get("updated_at", None)
    tax_exempt: Any = raw.get("tax_exempt", None)
    vat_number: str = raw.get("vat_number", "") or ""
    preferred_locale: str = raw.get("preferred_locale", "") or ""

    name = f"{first_name} {last_name}".strip() or company or email or code

    content_parts: list[str] = [f"Account ID: {account_id}"]
    if code and code != account_id:
        content_parts.append(f"Code: {code}")
    if name and name not in (account_id, code):
        content_parts.append(f"Name: {name}")
    if email:
        content_parts.append(f"Email: {email}")
    if company:
        content_parts.append(f"Company: {company}")
    if username:
        content_parts.append(f"Username: {username}")
    if state:
        content_parts.append(f"State: {state}")
    if vat_number:
        content_parts.append(f"VAT Number: {vat_number}")
    if preferred_locale:
        content_parts.append(f"Locale: {preferred_locale}")
    if tax_exempt is not None:
        content_parts.append(f"Tax Exempt: {tax_exempt}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")

    source_id = _short_id(f"account:{account_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Account: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{RECURLY_APP_URL}/accounts/{code}",
        metadata={
            "account_id": account_id,
            "code": code,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "company": company,
            "state": state,
            "username": username,
            "tax_exempt": tax_exempt,
            "vat_number": vat_number,
            "preferred_locale": preferred_locale,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_subscription(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Recurly subscription object into a ConnectorDocument.

    Stable source_id: sha256("subscription:" + subscription_id)[:16]
    """
    sub_id: str = raw.get("id", "")
    uuid: str = raw.get("uuid", "") or sub_id
    state: str = raw.get("state", "") or ""
    account_id: str = ""
    # account is nested: {"id": "...", "code": "..."}
    account_ref = raw.get("account", {})
    if isinstance(account_ref, dict):
        account_id = account_ref.get("id", "") or account_ref.get("code", "")
    plan_ref = raw.get("plan", {})
    plan_code: str = ""
    plan_name: str = ""
    if isinstance(plan_ref, dict):
        plan_code = plan_ref.get("code", "") or ""
        plan_name = plan_ref.get("name", "") or ""
    quantity: Any = raw.get("quantity", None)
    unit_amount: Any = raw.get("unit_amount", None)
    currency: str = raw.get("currency", "") or ""
    subtotal: Any = raw.get("subtotal", None)
    trial_started_at: Any = raw.get("trial_started_at", None)
    trial_ends_at: Any = raw.get("trial_ends_at", None)
    current_period_started_at: Any = raw.get("current_period_started_at", None)
    current_period_ends_at: Any = raw.get("current_period_ends_at", None)
    activated_at: Any = raw.get("activated_at", None)
    expires_at: Any = raw.get("expires_at", None)
    created_at: Any = raw.get("created_at", None)
    updated_at: Any = raw.get("updated_at", None)

    display_plan = plan_name or plan_code or "unknown plan"

    content_parts: list[str] = [f"Subscription ID: {sub_id}"]
    if state:
        content_parts.append(f"State: {state}")
    if account_id:
        content_parts.append(f"Account ID: {account_id}")
    if plan_code:
        content_parts.append(f"Plan Code: {plan_code}")
    if plan_name:
        content_parts.append(f"Plan Name: {plan_name}")
    if quantity is not None:
        content_parts.append(f"Quantity: {quantity}")
    if unit_amount is not None:
        content_parts.append(f"Unit Amount: {unit_amount}")
    if currency:
        content_parts.append(f"Currency: {currency}")
    if subtotal is not None:
        content_parts.append(f"Subtotal: {subtotal}")
    if current_period_started_at:
        content_parts.append(f"Period Start: {current_period_started_at}")
    if current_period_ends_at:
        content_parts.append(f"Period End: {current_period_ends_at}")
    if trial_ends_at:
        content_parts.append(f"Trial Ends: {trial_ends_at}")
    if activated_at:
        content_parts.append(f"Activated At: {activated_at}")
    if expires_at:
        content_parts.append(f"Expires At: {expires_at}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")

    source_id = _short_id(f"subscription:{sub_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Subscription {sub_id}: {display_plan} ({state})",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{RECURLY_APP_URL}/subscriptions/{uuid}",
        metadata={
            "subscription_id": sub_id,
            "uuid": uuid,
            "state": state,
            "account_id": account_id,
            "plan_code": plan_code,
            "plan_name": plan_name,
            "quantity": quantity,
            "unit_amount": unit_amount,
            "currency": currency,
            "subtotal": subtotal,
            "current_period_started_at": current_period_started_at,
            "current_period_ends_at": current_period_ends_at,
            "trial_started_at": trial_started_at,
            "trial_ends_at": trial_ends_at,
            "activated_at": activated_at,
            "expires_at": expires_at,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_invoice(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Recurly invoice object into a ConnectorDocument.

    Stable source_id: sha256("invoice:" + invoice_id)[:16]
    """
    invoice_id: str = raw.get("id", "")
    number: str = raw.get("number", "") or invoice_id
    state: str = raw.get("state", "") or ""
    account_id: str = ""
    account_ref = raw.get("account", {})
    if isinstance(account_ref, dict):
        account_id = account_ref.get("id", "") or account_ref.get("code", "")
    type_: str = raw.get("type", "") or ""
    currency: str = raw.get("currency", "") or ""
    subtotal: Any = raw.get("subtotal", None)
    tax: Any = raw.get("tax", None)
    total: Any = raw.get("total", None)
    due_on: Any = raw.get("due_on", None)
    paid: Any = raw.get("paid", None)
    balance: Any = raw.get("balance", None)
    collection_method: str = raw.get("collection_method", "") or ""
    net_terms: Any = raw.get("net_terms", None)
    po_number: str = raw.get("po_number", "") or ""
    created_at: Any = raw.get("created_at", None)
    updated_at: Any = raw.get("updated_at", None)
    closed_at: Any = raw.get("closed_at", None)

    content_parts: list[str] = [f"Invoice ID: {invoice_id}"]
    if number and number != invoice_id:
        content_parts.append(f"Invoice Number: {number}")
    if state:
        content_parts.append(f"State: {state}")
    if type_:
        content_parts.append(f"Type: {type_}")
    if account_id:
        content_parts.append(f"Account ID: {account_id}")
    if currency:
        content_parts.append(f"Currency: {currency}")
    if subtotal is not None:
        content_parts.append(f"Subtotal: {subtotal}")
    if tax is not None:
        content_parts.append(f"Tax: {tax}")
    if total is not None:
        content_parts.append(f"Total: {total}")
    if paid is not None:
        content_parts.append(f"Paid: {paid}")
    if balance is not None:
        content_parts.append(f"Balance: {balance}")
    if collection_method:
        content_parts.append(f"Collection Method: {collection_method}")
    if due_on:
        content_parts.append(f"Due On: {due_on}")
    if net_terms is not None:
        content_parts.append(f"Net Terms: {net_terms} days")
    if po_number:
        content_parts.append(f"PO Number: {po_number}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")
    if closed_at:
        content_parts.append(f"Closed At: {closed_at}")

    source_id = _short_id(f"invoice:{invoice_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Invoice {number}: {state}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{RECURLY_APP_URL}/invoices/{number}",
        metadata={
            "invoice_id": invoice_id,
            "number": number,
            "state": state,
            "type": type_,
            "account_id": account_id,
            "currency": currency,
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
            "paid": paid,
            "balance": balance,
            "collection_method": collection_method,
            "due_on": due_on,
            "net_terms": net_terms,
            "po_number": po_number,
            "created_at": created_at,
            "updated_at": updated_at,
            "closed_at": closed_at,
        },
    )


def normalize_plan(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Recurly plan object into a ConnectorDocument.

    Stable source_id: sha256("plan:" + plan_id)[:16]
    """
    plan_id: str = raw.get("id", "")
    code: str = raw.get("code", "") or plan_id
    name: str = raw.get("name", "") or code
    description: str = raw.get("description", "") or ""
    state: str = raw.get("state", "") or ""
    interval_length: Any = raw.get("interval_length", None)
    interval_unit: str = raw.get("interval_unit", "") or ""
    trial_length: Any = raw.get("trial_length", None)
    trial_unit: str = raw.get("trial_unit", "") or ""
    setup_fee_in_cents: dict[str, Any] = raw.get("setup_fee_in_cents", {}) or {}
    currencies: list[dict[str, Any]] = raw.get("currencies", []) or []
    tax_exempt: Any = raw.get("tax_exempt", None)
    accounting_code: str = raw.get("accounting_code", "") or ""
    auto_renew: Any = raw.get("auto_renew", None)
    created_at: Any = raw.get("created_at", None)
    updated_at: Any = raw.get("updated_at", None)
    deleted_at: Any = raw.get("deleted_at", None)

    content_parts: list[str] = [f"Plan ID: {plan_id}"]
    if code and code != plan_id:
        content_parts.append(f"Code: {code}")
    content_parts.append(f"Name: {name}")
    if description:
        content_parts.append(f"Description: {description}")
    if state:
        content_parts.append(f"State: {state}")
    if interval_length is not None and interval_unit:
        content_parts.append(f"Billing Interval: every {interval_length} {interval_unit}(s)")
    if trial_length is not None and trial_unit:
        content_parts.append(f"Trial: {trial_length} {trial_unit}(s)")
    if currencies:
        for curr in currencies[:3]:  # show first 3 currencies
            currency = curr.get("currency", "")
            unit_amount = curr.get("unit_amount", None)
            if currency and unit_amount is not None:
                content_parts.append(f"Price ({currency}): {unit_amount}")
    if tax_exempt is not None:
        content_parts.append(f"Tax Exempt: {tax_exempt}")
    if auto_renew is not None:
        content_parts.append(f"Auto Renew: {auto_renew}")
    if accounting_code:
        content_parts.append(f"Accounting Code: {accounting_code}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")

    source_id = _short_id(f"plan:{plan_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Plan: {name} ({state})",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{RECURLY_APP_URL}/plans/{code}",
        metadata={
            "plan_id": plan_id,
            "code": code,
            "name": name,
            "description": description,
            "state": state,
            "interval_length": interval_length,
            "interval_unit": interval_unit,
            "trial_length": trial_length,
            "trial_unit": trial_unit,
            "setup_fee_in_cents": setup_fee_in_cents,
            "currencies": currencies,
            "tax_exempt": tax_exempt,
            "auto_renew": auto_renew,
            "accounting_code": accounting_code,
            "created_at": created_at,
            "updated_at": updated_at,
            "deleted_at": deleted_at,
        },
    )


def normalize_transaction(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Recurly transaction object into a ConnectorDocument.

    Stable source_id: sha256("transaction:" + transaction_id)[:16]
    """
    txn_id: str = raw.get("id", "")
    uuid: str = raw.get("uuid", "") or txn_id
    type_: str = raw.get("type", "") or ""
    status: str = raw.get("status", "") or ""
    origin: str = raw.get("origin", "") or ""
    account_id: str = ""
    account_ref = raw.get("account", {})
    if isinstance(account_ref, dict):
        account_id = account_ref.get("id", "") or account_ref.get("code", "")
    invoice_id: str = ""
    invoice_ref = raw.get("invoice", {})
    if isinstance(invoice_ref, dict):
        invoice_id = invoice_ref.get("id", "") or ""
    currency: str = raw.get("currency", "") or ""
    amount: Any = raw.get("amount", None)
    refunded: Any = raw.get("refunded", None)
    tax: Any = raw.get("tax", None)
    net: Any = raw.get("net", None)
    gateway_message: str = raw.get("gateway_message", "") or ""
    status_code: str = raw.get("status_code", "") or ""
    created_at: Any = raw.get("created_at", None)
    collected_at: Any = raw.get("collected_at", None)
    voided_at: Any = raw.get("voided_at", None)

    content_parts: list[str] = [f"Transaction ID: {txn_id}"]
    if type_:
        content_parts.append(f"Type: {type_}")
    if status:
        content_parts.append(f"Status: {status}")
    if origin:
        content_parts.append(f"Origin: {origin}")
    if account_id:
        content_parts.append(f"Account ID: {account_id}")
    if invoice_id:
        content_parts.append(f"Invoice ID: {invoice_id}")
    if currency:
        content_parts.append(f"Currency: {currency}")
    if amount is not None:
        content_parts.append(f"Amount: {amount}")
    if refunded is not None:
        content_parts.append(f"Refunded: {refunded}")
    if tax is not None:
        content_parts.append(f"Tax: {tax}")
    if net is not None:
        content_parts.append(f"Net: {net}")
    if status_code:
        content_parts.append(f"Status Code: {status_code}")
    if gateway_message:
        content_parts.append(f"Gateway Message: {gateway_message}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")
    if collected_at:
        content_parts.append(f"Collected At: {collected_at}")

    source_id = _short_id(f"transaction:{txn_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Transaction {txn_id}: {type_} ({status})",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{RECURLY_APP_URL}/transactions/{uuid}",
        metadata={
            "transaction_id": txn_id,
            "uuid": uuid,
            "type": type_,
            "status": status,
            "origin": origin,
            "account_id": account_id,
            "invoice_id": invoice_id,
            "currency": currency,
            "amount": amount,
            "refunded": refunded,
            "tax": tax,
            "net": net,
            "gateway_message": gateway_message,
            "status_code": status_code,
            "created_at": created_at,
            "collected_at": collected_at,
            "voided_at": voided_at,
        },
    )


# ── Retry helper ──────────────────────────────────────────────────────────────


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the Retry-After / X-RateLimit-Reset value when present.
    """
    last_exc: RecurlyError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except RecurlyAuthError:
            raise  # no retry on auth failures
        except RecurlyRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except RecurlyError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
