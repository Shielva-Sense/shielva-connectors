from __future__ import annotations

from typing import Any

from models import ConnectorDocument


def normalize_event(event: dict[str, Any], connector_id: str, tenant_id: str) -> ConnectorDocument:
    """Convert a raw Stripe Event object into a ConnectorDocument."""
    event_type = event.get("type", "unknown")
    event_id = event.get("id", "")
    created = event.get("created", 0)
    livemode = event.get("livemode", False)
    data_obj = event.get("data", {}).get("object", {})

    title = f"Stripe event: {event_type} [{event_id}]"
    content_parts = [
        f"Event ID: {event_id}",
        f"Type: {event_type}",
        f"Live mode: {livemode}",
        f"Created: {created}",
    ]
    if data_obj:
        content_parts.append(f"Data object type: {data_obj.get('object', 'unknown')}")
        if "amount" in data_obj:
            content_parts.append(f"Amount: {data_obj['amount']} {data_obj.get('currency', '').upper()}")
        if "status" in data_obj:
            content_parts.append(f"Status: {data_obj['status']}")

    return ConnectorDocument(
        source_id=event_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://dashboard.stripe.com/events/{event_id}",
        metadata={
            "event_type": event_type,
            "livemode": livemode,
            "created": created,
            "object_type": data_obj.get("object", "unknown") if data_obj else "unknown",
        },
    )


def normalize_customer(customer: dict[str, Any], connector_id: str, tenant_id: str) -> ConnectorDocument:
    """Convert a raw Stripe Customer object into a ConnectorDocument."""
    customer_id = customer.get("id", "")
    email = customer.get("email", "")
    name = customer.get("name", "") or "Unknown"
    created = customer.get("created", 0)

    title = f"Stripe customer: {name} <{email}>"
    content_parts = [
        f"Customer ID: {customer_id}",
        f"Name: {name}",
        f"Email: {email}",
        f"Created: {created}",
    ]
    if customer.get("description"):
        content_parts.append(f"Description: {customer['description']}")
    if customer.get("phone"):
        content_parts.append(f"Phone: {customer['phone']}")

    return ConnectorDocument(
        source_id=customer_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://dashboard.stripe.com/customers/{customer_id}",
        metadata={
            "email": email,
            "name": name,
            "created": created,
            "currency": customer.get("currency", ""),
            "delinquent": customer.get("delinquent", False),
        },
    )


def normalize_charge(charge: dict[str, Any], connector_id: str, tenant_id: str) -> ConnectorDocument:
    """Convert a raw Stripe Charge object into a ConnectorDocument."""
    charge_id = charge.get("id", "")
    amount = charge.get("amount", 0)
    currency = charge.get("currency", "usd").upper()
    status = charge.get("status", "unknown")
    description = charge.get("description", "")

    title = f"Stripe charge: {amount / 100:.2f} {currency} — {status}"
    content = (
        f"Charge ID: {charge_id}\n"
        f"Amount: {amount} {currency}\n"
        f"Status: {status}\n"
        f"Description: {description}\n"
        f"Customer: {charge.get('customer', 'none')}"
    )

    return ConnectorDocument(
        source_id=charge_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://dashboard.stripe.com/payments/{charge_id}",
        metadata={
            "amount": amount,
            "currency": currency.lower(),
            "status": status,
            "customer_id": charge.get("customer", ""),
        },
    )
