from __future__ import annotations

from typing import Any

from helpers.utils import sha256_id
from models import ConnectorDocument


def normalize_contact(
    contact: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw SendGrid marketing contact into a ConnectorDocument.

    source_id = SHA-256("contact:{id}")[:16]
    """
    contact_id = str(contact.get("id", ""))
    source_id = sha256_id(f"contact:{contact_id}")
    email = contact.get("email", "") or ""
    first_name = contact.get("first_name", "") or ""
    last_name = contact.get("last_name", "") or ""
    created_at = contact.get("created_at", "") or ""
    updated_at = contact.get("updated_at", "") or ""
    list_ids: list[str] = contact.get("list_ids", []) or []

    display_name = " ".join(p for p in [first_name, last_name] if p) or email or contact_id
    title = f"SendGrid contact: {display_name}"

    content_parts = [f"Contact ID: {contact_id}", f"Email: {email}"]
    if first_name:
        content_parts.append(f"First name: {first_name}")
    if last_name:
        content_parts.append(f"Last name: {last_name}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")
    if list_ids:
        content_parts.append(f"List IDs: {', '.join(list_ids)}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://mc.sendgrid.com/contacts",
        metadata={
            "object_type": "contact",
            "sendgrid_id": contact_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "created_at": created_at,
            "updated_at": updated_at,
            "list_ids": list_ids,
        },
    )


def normalize_template(
    template: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw SendGrid template into a ConnectorDocument.

    source_id = SHA-256("template:{id}")[:16]
    """
    template_id = str(template.get("id", ""))
    source_id = sha256_id(f"template:{template_id}")
    name = template.get("name", "") or f"Template {template_id}"
    generation = template.get("generation", "") or ""
    versions: list[dict[str, Any]] = template.get("versions", []) or []

    active_version_id = ""
    for v in versions:
        if v.get("active") == 1:
            active_version_id = str(v.get("id", ""))
            break

    title = f"SendGrid template: {name}"
    content_parts = [
        f"Template ID: {template_id}",
        f"Name: {name}",
        f"Generation: {generation}",
    ]
    if active_version_id:
        content_parts.append(f"Active version ID: {active_version_id}")
    if versions:
        content_parts.append(f"Version count: {len(versions)}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://mc.sendgrid.com/dynamic-templates/{template_id}",
        metadata={
            "object_type": "email_template",
            "sendgrid_id": template_id,
            "name": name,
            "generation": generation,
            "active_version_id": active_version_id,
            "version_count": len(versions),
        },
    )
