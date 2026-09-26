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
"""

from __future__ import annotations

import json
import secrets

import structlog

from services.redis_service import redis_service

logger = structlog.get_logger(__name__)

#: Long enough to pick an account and read a consent screen; short enough that a
#: leaked link is soon worthless.
TTL_SECONDS = 15 * 60
_KEY = "oauth:state:{nonce}"


async def mint(tenant_id: str, connector_id: str) -> str:
    """The `state` for a new consent URL. Plain `connector_id` when Redis is down —
    the popup route still works then; only server-side completion is lost."""
    nonce = secrets.token_urlsafe(24)
    try:
        await redis_service.set(
            _KEY.format(nonce=nonce),
            json.dumps({"tenant_id": tenant_id, "connector_id": connector_id}),
            expire=TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("oauth_state.mint_unstored", connector_id=connector_id, error=str(exc)[:160])
        return connector_id
    if redis_service.client is None:
        logger.warning("oauth_state.mint_unstored", connector_id=connector_id, error="redis unavailable")
        return connector_id
    return f"{connector_id}.{nonce}"


async def consume(state: str) -> tuple[str, str] | None:
    """`(tenant_id, connector_id)` for a state minted here — once. None otherwise."""
    connector_id, sep, nonce = (state or "").rpartition(".")
    if not sep or not nonce or not connector_id:
        return None
    if redis_service.client is None:
        await redis_service.connect()
    if redis_service.client is None:
        return None
    raw = await redis_service.client.getdel(_KEY.format(nonce=nonce))
    if not raw:
        return None
    try:
        found = json.loads(raw)
    except ValueError:
        return None
    if found.get("connector_id") != connector_id:
        # A nonce is bound to the connector it was minted for; a state that
        # pairs it with another is not ours.
        return None
    return str(found["tenant_id"]), connector_id
