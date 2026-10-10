"""The OAuth `state` of one consent flow: unguessable, single-use, and bound to a tenant.

🚨 WHY THE CALLBACK COMPLETES ON THE SERVER. The consent popup used to hand the
code back to the console through `window.opener.postMessage`, and the console
then asked us to exchange it. That link is severed by any provider page that
sends a `Cross-Origin-Opener-Policy` header — GoHighLevel's Marketplace does —
so the popup read "Authorization Successful", the code reached us in the
redirect, and nothing ever exchanged it: the connector sat on "pending" and
nothing said why.

So the redirect itself is enough. The consent URL carries
`state = "<connector_id>.<nonce>"`; the nonce is stored here with the tenant
and connector it was minted for; the callback consumes it ONCE and completes the
exchange. The console still receives its postMessage when the opener survives,
and it matches the flow by the `<connector_id>.` prefix.

🚨 A STATE THAT IS ONLY THE CONNECTOR ID PROVES NOTHING. It is predictable, so a
callback trusting it would bind whichever account's code arrived to that
tenant's connector (OAuth login-CSRF). Only a nonce minted here, unexpired and
unused, completes anything server-side.

🚨 THE STATE NAMES THE TENANT — THE BROWSER'S WORKSPACE DOES NOT. The callback
used to refuse whenever the `X-Tenant-ID` the gateway stamped from the browser's
`.shielva.ai` session cookie differed from the tenant in the state. That cookie
is shared by every product and holds whichever workspace signed in LAST, while
abs.shielva.ai authenticates with its own per-app Bearer token. One person with
two workspaces, booking for one in ABS and signed in to ARC as the other, got
"This sign-in was started from another workspace" on every attempt
(2026-10-10, Google Calendar). The tenant has only ever come from here, so the
comparison protected no data; what it stood in for is "the browser finishing
the consent belongs to whoever started it". That is now checked directly: the
person who asked for the consent URL (`initiator`, the gateway-verified email)
is recorded, and the callback compares PEOPLE, not workspaces.
"""

from __future__ import annotations

import json
import secrets
from typing import NamedTuple

import structlog

from services.redis_service import redis_service

logger = structlog.get_logger(__name__)

#: Long enough to pick an account and read a consent screen; short enough that a
#: leaked link is soon worthless.
TTL_SECONDS = 15 * 60
_KEY = "oauth:state:{nonce}"


class Claim(NamedTuple):
    """What a consumed state was minted for."""

    tenant_id: str
    connector_id: str
    #: The person who asked for the consent URL, normalised; "" when the caller
    #: did not say (a service call with no person, or a state minted before this).
    initiator: str = ""


def normalise_person(email: str | None) -> str:
    """The one form an initiator is stored and compared in."""
    return (email or "").strip().lower()


async def mint(tenant_id: str, connector_id: str, initiator: str = "") -> str:
    """The `state` for a new consent URL. Plain `connector_id` when Redis is down —
    the popup route still works then; only server-side completion is lost.

    `initiator` is the email of the person asking, as the gateway (or the calling
    service) verified it; "" when there is none."""
    nonce = secrets.token_urlsafe(24)
    record = {"tenant_id": tenant_id, "connector_id": connector_id}
    person = normalise_person(initiator)
    if person:
        record["initiator"] = person
    try:
        await redis_service.set(_KEY.format(nonce=nonce), json.dumps(record), expire=TTL_SECONDS)
    except Exception as exc:
        logger.warning("oauth_state.mint_unstored", connector_id=connector_id, error=str(exc)[:160])
        return connector_id
    if redis_service.client is None:
        logger.warning("oauth_state.mint_unstored", connector_id=connector_id, error="redis unavailable")
        return connector_id
    return f"{connector_id}.{nonce}"


def _unclaimed(connector_id: str, reason: str) -> None:
    # 🚨 Never the nonce: until it is spent it is a bearer secret for this flow.
    logger.warning("oauth_state.unclaimed", connector_id=connector_id, reason=reason)


async def consume(state: str) -> Claim | None:
    """What a state minted here was minted for — once. None otherwise.

    A bare connector id is the legacy console hand-off, not a miss; every other
    miss is logged with its reason."""
    connector_id, sep, nonce = (state or "").rpartition(".")
    if not sep or not nonce or not connector_id:
        return None
    if redis_service.client is None:
        await redis_service.connect()
    if redis_service.client is None:
        _unclaimed(connector_id, "store_unavailable")
        return None
    raw = await redis_service.client.getdel(_KEY.format(nonce=nonce))
    if not raw:
        # Older than TTL_SECONDS, already spent, or never ours.
        _unclaimed(connector_id, "unknown_or_expired")
        return None
    try:
        found = json.loads(raw)
    except ValueError:
        _unclaimed(connector_id, "unreadable_record")
        return None
    if found.get("connector_id") != connector_id:
        # A nonce is bound to the connector it was minted for; a state that
        # pairs it with another is not ours.
        _unclaimed(connector_id, "connector_mismatch")
        return None
    return Claim(str(found["tenant_id"]), connector_id, normalise_person(found.get("initiator")))
