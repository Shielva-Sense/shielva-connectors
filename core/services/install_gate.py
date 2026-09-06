"""Whether an install() result means the credentials were accepted.

Its own module, and deliberately import-free, because the decision has to be
testable without booting the gateway. Living in gateway.py meant a test could
only reach it by importing the whole application — apscheduler, the scheduler,
the registry — so the first version of the test reimplemented the comparison
instead. That copy could keep passing while the real gate drifted underneath
it, and it left the gate itself uncovered.
"""

from __future__ import annotations

from typing import Any

# Statuses that mean an install may proceed. PENDING is not a failure: an OAuth
# connector cannot be authenticated until the user has been through consent, and
# the authorization URL is produced after the install call — so rejecting it
# would make every OAuth connector impossible to install at all.
INSTALLABLE_AUTH = frozenset({"connected", "authenticated", "pending"})


def install_auth_ok(auth_status: Any) -> bool:
    """True when `auth_status` means the connector may be registered.

    🚨 Compares the status VALUE, not enum identity. A connector falls back to
    its OWN AuthStatus enum when the shared SDK is not importable, so
    `auth_status is AuthStatus.CONNECTED` is False for exactly the connectors
    that most need checking — and a gate that silently passes everything is
    worse than no gate at all.

    A plain string is accepted too: not every connector returns an enum.
    """
    return getattr(auth_status, "value", str(auth_status)) in INSTALLABLE_AUTH


def install_auth_status(result: Any) -> str:
    """The auth status an install() result means, whatever shape it came in.

    🚨 `result.auth_status` is not guaranteed. Several connectors define their
    OWN InstallResult in the connector's models.py — sharepoint's carries
    success / message / install_fields / connector_type and no auth status at
    all — so reading the attribute directly raised AttributeError and turned a
    perfectly successful install into a 500 with no clue in it for the user.

    A result with no auth status still says what happened: it succeeded, and for
    an OAuth connector a successful install with no token yet IS `pending` —
    those connectors validate fields locally and leave the network round trip to
    consent. A failure maps to `failed`, which the gate below rejects.
    """
    auth = getattr(result, "auth_status", None)
    if auth is not None:
        return getattr(auth, "value", str(auth))
    return "pending" if getattr(result, "success", True) else "failed"


def install_health(result: Any) -> str:
    """The health an install() result means, whatever shape it came in.

    🚨 The sibling of install_auth_status, and it exists because fixing only
    `auth_status` left `health` one line below reading the attribute directly —
    so hubspot swapped one 500 for another. A connector-local InstallResult
    carries neither field; what it does carry is whether it succeeded.
    """
    health = getattr(result, "health", None)
    if health is not None:
        return getattr(health, "value", str(health))
    return "healthy" if getattr(result, "success", True) else "unhealthy"


def install_message(result: Any) -> str:
    """Whatever the connector said about the install, or ''."""
    return str(getattr(result, "message", "") or "")
