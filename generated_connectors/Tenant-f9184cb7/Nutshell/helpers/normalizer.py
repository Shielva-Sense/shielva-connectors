"""Transforms raw Nutshell JSON-RPC results into typed wrappers + dicts."""
from __future__ import annotations

from typing import Any, Dict

from models import NutshellAccount, NutshellContact, NutshellLead


def normalize_contact(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized contact dict suitable for downstream ingestion."""
    contact = NutshellContact.from_raw(raw or {})
    return {
        "id": contact.id,
        "rev": contact.rev,
        "display_name": contact.name,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "emails": contact.emails,
        "phones": contact.phones,
        "accounts": contact.accounts,
        "custom_fields": contact.custom_fields,
        "created_time": contact.created_time,
        "modified_time": contact.modified_time,
        "raw": contact.raw,
    }


def normalize_lead(raw: Dict[str, Any]) -> Dict[str, Any]:
    lead = NutshellLead.from_raw(raw or {})
    return {
        "id": lead.id,
        "rev": lead.rev,
        "description": lead.description,
        "confidence": lead.confidence,
        "value": lead.value,
        "status": lead.status,
        "primary_account": lead.primary_account,
        "contacts": lead.contacts,
        "created_time": lead.created_time,
        "modified_time": lead.modified_time,
        "raw": lead.raw,
    }


def normalize_account(raw: Dict[str, Any]) -> Dict[str, Any]:
    account = NutshellAccount.from_raw(raw or {})
    return {
        "id": account.id,
        "rev": account.rev,
        "name": account.name,
        "industry": account.industry,
        "territory": account.territory,
        "created_time": account.created_time,
        "modified_time": account.modified_time,
        "raw": account.raw,
    }
