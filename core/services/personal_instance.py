"""A PERSON's own instance of a connector, beside the workspace's one. Single owner.

The runtime keeps ONE instance per (tenant, connector type) —
``canonical_{type}_{tenant}`` — and every caller that names a connector by TYPE
(an action schema, a flow's capability node, bauto's calendar sync) is bound to
it. That is right for a workspace's CRM or mailbox, and wrong for a calendar:
in a clinic each doctor keeps their own Google Calendar, signed in with their
own Google account, and the business's diary is a different one again.

A personal instance is the canonical id plus ``__person_<key>``:

    canonical_google_calendar_Tenant-1__person_3f9a1c0b7d2e4a61

* ``<key>`` is chosen by the CALLER (8 to 64 lowercase hex or letters/digits) and
  is opaque here: bauto sends a hash, so no name or email ends up in an id, a
  log line or a Redis key.
* It is reached ONLY by its full id. 🚨 Every lookup by TYPE skips it
  (``registry.find_authorized``, the type-alias match, the store rehydrate):
  otherwise the first doctor to connect would silently become the workspace's
  calendar, and every booking flow in the tenant would write into their diary.
* Its tokens are stored under its own id, never under the canonical one, and
  the workspace's stored client credentials are neither overwritten by its
  consent nor deleted when it is removed.

Tenant isolation is unchanged: the id still embeds the tenant, and every route
still compares ``connector.tenant_id`` with the caller's ``X-Tenant-ID``.
"""

from __future__ import annotations

import re

PERSON_MARK = "__person_"
_KEY = re.compile(r"^[a-z0-9]{8,64}$")


def valid_person(key: object) -> bool:
    """Whether ``key`` may name a person's instance."""
    return isinstance(key, str) and bool(_KEY.match(key))


def personal_connector_id(connector_type: str, tenant_id: str, person: str) -> str:
    """The instance id of ``person``'s own ``connector_type`` in ``tenant_id``. Raises ``ValueError``."""
    if not valid_person(person):
        raise ValueError("person must be 8 to 64 lowercase letters or digits")
    return f"canonical_{connector_type}_{tenant_id}{PERSON_MARK}{person}"


def is_personal(connector_id: object) -> bool:
    """Whether ``connector_id`` is a person's own instance (reached only by its full id)."""
    return isinstance(connector_id, str) and PERSON_MARK in connector_id


def type_of(connector_id: str, tenant_id: str) -> str:
    """The connector type a person's instance in ``tenant_id`` is of ("" when it is not one of theirs)."""
    tail = f"_{tenant_id}{PERSON_MARK}"
    if not (tenant_id and connector_id.startswith("canonical_") and tail in connector_id):
        return ""
    return connector_id[len("canonical_") :].split(tail, 1)[0]


__all__ = ["PERSON_MARK", "is_personal", "personal_connector_id", "type_of", "valid_person"]
