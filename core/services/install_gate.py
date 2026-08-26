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
