"""What "connected" means for a connector, in one place.

🚨 Connection state is DERIVED, never stored. There is no status column on a
stored connector and there should not be one: the moment you denormalise it, a
token expiring, being revoked at the provider, or being refreshed on another
replica makes the stored value a lie, and every one of those happens without a
write you control. The durable inputs are the token record and the stored
config; the answer is computed from them on read.

It lives here because it was computed in two endpoints with two different rules
— one asking the in-memory registry, the other asking only whether an
access_token string was non-empty. Neither looked at `expires_at` or
`refresh_token`, which the token record has carried from the beginning, so an
expired token with no way to refresh reported itself Connected.

Import-free on purpose, like install_gate: the rule has to be testable without
booting FastAPI, or the test ends up reimplementing it and the copy drifts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: A live instance reporting one of these is authenticated.
AUTHORISED = frozenset({"connected", "authenticated"})

CONNECTED = "connected"
EXPIRED = "expired"
PENDING = "pending"


def _expires_at(token: Any) -> datetime | None:
    value = getattr(token, "expires_at", None)
    if not isinstance(value, datetime):
        return None
    # A naive timestamp is UTC here — the store writes UTC — and comparing one
    # against an aware `now` raises rather than returning a wrong answer, which
    # would surface as a 500 on the connectors page.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def token_state(token: Any, refresh_rejected: bool = False) -> str:
    """What a stored token says about the connection.

    A refresh token is the strongest signal available — a new access token can
    be minted, so an access token that expired five minutes ago is not a
    disconnection and must not send the user back through consent.

    🚨 But its PRESENCE is not proof. Google expires refresh tokens after seven
    days while an app is in Testing, Microsoft after ninety days idle, and
    either can be revoked by an admin at any moment. A non-empty field
    therefore says the credential once existed, never that it still works —
    which is why the provider's rejection is recorded rather than re-derived.
    Once it has been, the field is worth nothing and the card must say so.
    """
    if refresh_rejected:
        return EXPIRED
    if token is None or not getattr(token, "access_token", None):
        return PENDING
    if getattr(token, "refresh_token", None):
        return CONNECTED
    expiry = _expires_at(token)
    if expiry is not None and expiry <= datetime.now(UTC):
        # 🚨 Distinct from PENDING. "Never connected" and "connected, and the
        # credential aged out" need different words on the card: one asks for a
        # first sign-in, the other says why something that worked stopped.
        return EXPIRED
    return CONNECTED


def connection_state(token: Any = None, live_status: Any = None, refresh_rejected: bool = False) -> str:
    """The connector's state, from its durable token and any live instance.

    Two proofs, because there are two kinds of connector. An OAuth connector's
    proof is the stored token. A static-credential connector — Slack on a bot
    token, anything on an API key — never has one, and reading only the token
    store reports the working ones as unconfigured.
    """
    state = token_state(token, refresh_rejected)
    if state == CONNECTED:
        return CONNECTED
    # A recorded rejection outranks a live instance. That instance was built
    # before the provider said no, and it is the stale opinion of one pod.
    if live_status is not None and not refresh_rejected:
        reported = getattr(live_status, "value", str(live_status)).lower()
        if reported in AUTHORISED:
            return CONNECTED
    return state


def is_connected(token: Any = None, live_status: Any = None, refresh_rejected: bool = False) -> bool:
    return connection_state(token, live_status, refresh_rejected) == CONNECTED
