"""Transforms raw Vanta API responses into NormalizedDocument objects.

One function per resource type — keep the selection logic free of
if/elif chains so new resources can be added by adding a function.

NormalizedDocument.id is multi-tenant-prefixed:
    f"{connector_id}_{resource}_{source_id}" — collisions are impossible across
tenants because connector_id is provisioned per (tenant_id, service) pair.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.base_connector import NormalizedDocument

_SOURCE = "vanta_connector"


def _parse_iso8601(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


def normalize_framework(
    framework: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    fid = str(framework.get("id", ""))
    name = framework.get("name", "") or f"Vanta Framework {fid}"
    description = framework.get("description", "") or ""
    return NormalizedDocument(
        id=f"{connector_id}_framework_{fid}",
        source_id=fid,
        title=name,
        content=description or name,
        content_type="text",
        source_url=f"https://app.vanta.com/frameworks/{fid}",
        created_at=_parse_iso8601(framework.get("createdAt")),
        updated_at=_parse_iso8601(framework.get("updatedAt")),
        source=_SOURCE,
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "slug": framework.get("slug", ""),
            "status": framework.get("status", ""),
            "progress": framework.get("progress"),
            "certification_status": framework.get("certificationStatus", ""),
            "kind": "vanta.framework",
        },
    )


def normalize_control(
    control: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    cid = str(control.get("id", ""))
    name = control.get("name", "") or f"Vanta Control {cid}"
    description = control.get("description", "") or ""
    return NormalizedDocument(
        id=f"{connector_id}_control_{cid}",
        source_id=cid,
        title=name,
        content=description or name,
        content_type="text",
        source_url=f"https://app.vanta.com/controls/{cid}",
        created_at=_parse_iso8601(control.get("createdAt")),
        updated_at=_parse_iso8601(control.get("updatedAt") or control.get("lastTestedAt")),
        source=_SOURCE,
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "framework_id": control.get("frameworkId", ""),
            "control_owner_id": control.get("controlOwnerId", ""),
            "status": control.get("status", ""),
            "severity": control.get("severity", ""),
            "kind": "vanta.control",
        },
    )


def normalize_vendor(
    vendor: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    vid = str(vendor.get("id", ""))
    name = vendor.get("name", "") or f"Vanta Vendor {vid}"
    description = vendor.get("description", "") or ""
    website = vendor.get("websiteUrl", "") or ""
    return NormalizedDocument(
        id=f"{connector_id}_vendor_{vid}",
        source_id=vid,
        title=name,
        content=description or name,
        content_type="text",
        source_url=f"https://app.vanta.com/vendors/{vid}",
        author=vendor.get("ownerEmail", "") or "",
        created_at=_parse_iso8601(vendor.get("createdAt")),
        updated_at=_parse_iso8601(vendor.get("updatedAt")),
        source=_SOURCE,
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "website_url": website,
            "owner_email": vendor.get("ownerEmail", ""),
            "risk_level": vendor.get("riskLevel", ""),
            "status": vendor.get("status", ""),
            "kind": "vanta.vendor",
        },
    )


def normalize_personnel(
    person: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    pid = str(person.get("id", ""))
    email = person.get("email", "") or ""
    display_name = person.get("displayName", "") or person.get("name", "") or email
    content = (
        f"Person: {display_name}\n"
        f"Email: {email}\n"
        f"Role: {person.get('role', '')}\n"
        f"Active: {person.get('isActive', True)}"
    )
    return NormalizedDocument(
        id=f"{connector_id}_personnel_{pid}",
        source_id=pid,
        title=display_name or f"Vanta Personnel {pid}",
        content=content,
        content_type="text",
        source_url=f"https://app.vanta.com/personnel/{pid}",
        author=email,
        created_at=_parse_iso8601(person.get("createdAt")),
        updated_at=_parse_iso8601(person.get("updatedAt")),
        source=_SOURCE,
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "email": email,
            "is_active": person.get("isActive", True),
            "role": person.get("role", ""),
            "employment_status": person.get("employmentStatus", ""),
            "kind": "vanta.personnel",
        },
    )


# Back-compat aliases for older imports.
normalize_person = normalize_personnel


def normalize_finding(
    finding: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    fid = str(finding.get("id", ""))
    title = finding.get("title", "") or f"Vanta Finding {fid}"
    description = finding.get("description", "") or ""
    return NormalizedDocument(
        id=f"{connector_id}_finding_{fid}",
        source_id=fid,
        title=title,
        content=description or title,
        content_type="text",
        source_url=f"https://app.vanta.com/findings/{fid}",
        author=finding.get("assigneeEmail", "") or "",
        created_at=_parse_iso8601(finding.get("createdAt")),
        updated_at=_parse_iso8601(finding.get("updatedAt")),
        source=_SOURCE,
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "severity": finding.get("severity", ""),
            "status": finding.get("status", ""),
            "control_id": finding.get("controlId", ""),
            "kind": "vanta.finding",
        },
    )
