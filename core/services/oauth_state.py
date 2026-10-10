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

🚨 [Sensitive] THE STATE IS THE WHOLE AUTHORISATION OF THE CALLBACK. It is a
provider redirect: it arrives from whatever browser finished the consent, with
whatever `.shielva.ai` cookie that browser happens to hold (another product,
another workspace) or none at all (abs.shielva.ai and a business's custom domain
sign in with their own Bearer token). So the gateway serves the callback
anonymously (`connectors-oauth-callback`) and the callback asks nothing of the
browser: the tenant and connector come from here, minted when an authenticated
member of that workspace asked for the consent URL. Because the nonce is
unguessable, single-use and short-lived, possessing it is what proves the
redirect belongs to that flow. Nothing in the request can name another tenant.

The residual risk this accepts: someone who starts a consent can forward the
consent link, and whoever approves it within TTL_SECONDS connects THEIR account
to the starter's workspace. That needs the victim to approve a consent screen
naming Shielva and their own account. The cookie-workspace check this replaces
only stopped a victim who happened to be signed in to a DIFFERENT workspace; one
signed in to the same workspace passed, and one with no session was stopped by
the gateway's JWT requirement, which stopped every customer signed in by Bearer
token too. The proper fix, binding the state to the browser that
opened the popup (a cookie set on the callback origin at popup start), is a
follow-up.
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


async def mint(tenant_id: str, connector_id: str) -> str:
    """The `state` for a new consent URL. Plain `connector_id` when Redis is down —
    the popup route still works then; only server-side completion is lost."""
    nonce = secrets.token_urlsafe(24)
    record = {"tenant_id": tenant_id, "connector_id": connector_id}
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
    return Claim(str(found["tenant_id"]), connector_id)
