"""Connection state is derived from the token record, and derived once.

🚨 The rule was written twice, in two endpoints, and neither copy looked at
`expires_at` or `refresh_token` — fields the token record has carried from the
beginning. One asked the in-memory registry; the other asked only whether the
access_token string was non-empty. So an expired token with no way to refresh
reported itself Connected, and a valid one on a pod that had not loaded it
reported "Ready to connect".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from services.connection_state import (
    CONNECTED,
    EXPIRED,
    PENDING,
    connection_state,
    is_connected,
    token_state,
)


@dataclass
class _Token:
    access_token: str = "at"
    refresh_token: str | None = None
    expires_at: datetime | None = None


def test_no_token_is_pending() -> None:
    assert token_state(None) == PENDING
    assert token_state(_Token(access_token="")) == PENDING


def test_a_refresh_token_outranks_an_expired_access_token() -> None:
    """🚨 The important one. A refresh token means a new access token can always
    be minted, so an access token that aged out five minutes ago is not a
    disconnection — and must not send the user back through consent."""
    stale = _Token(refresh_token="rt", expires_at=datetime.now(UTC) - timedelta(hours=5))
    assert token_state(stale) == CONNECTED


def test_an_expired_token_with_no_refresh_is_expired_not_pending() -> None:
    """ "Never connected" and "connected, and the credential aged out" need
    different words on the card; only one of them is the user's fault."""
    dead = _Token(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    assert token_state(dead) == EXPIRED


def test_a_token_with_no_expiry_is_taken_at_face_value() -> None:
    assert token_state(_Token()) == CONNECTED


def test_a_naive_timestamp_does_not_raise() -> None:
    """The store writes UTC; comparing a naive timestamp against an aware `now`
    raises, which would surface as a 500 on the connectors page rather than as a
    wrong badge."""
    naive = _Token(expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1))
    assert token_state(naive) == EXPIRED


def test_a_static_credential_connector_proves_itself_through_the_live_instance() -> None:
    """Slack has no OAuth token — it authenticates with a bot token in config."""
    assert connection_state(None, "connected") == CONNECTED
    assert connection_state(None, "authenticated") == CONNECTED
    assert is_connected(None, "connected")


def test_a_live_instance_that_is_not_authenticated_does_not_rescue_a_missing_token() -> None:
    assert connection_state(None, "pending") == PENDING
    assert not is_connected(None, "pending")


def test_an_enum_like_status_is_accepted() -> None:
    """Connectors fall back to their own AuthStatus enum when the shared SDK is
    not importable, so the value is compared, never the identity."""

    class _Enumish:
        value = "CONNECTED"

    assert connection_state(None, _Enumish()) == CONNECTED


def test_the_rule_is_not_reimplemented_in_the_gateway() -> None:
    """Two copies is how the two endpoints came to disagree in the first place."""
    src = (_CORE / "gateway.py").read_text()
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "token.access_token" not in code, (
        "the gateway is deciding connectedness itself again instead of asking services/connection_state.py"
    )


def test_a_rejected_refresh_token_stops_reading_as_connected() -> None:
    """🚨 The hole in "refresh_token present → connected".

    A refresh token is not permanent: Google expires them after seven days while
    an app is in Testing, Microsoft after ninety days idle, and either can be
    revoked. A non-empty field says the credential once existed, never that it
    still works — so once the provider has rejected it, the field is worth
    nothing.
    """
    alive_looking = _Token(refresh_token="rt")
    assert token_state(alive_looking) == CONNECTED
    assert token_state(alive_looking, refresh_rejected=True) == EXPIRED
    assert not is_connected(alive_looking, None, True)


def test_a_stale_live_instance_cannot_override_a_rejection() -> None:
    """The instance was built before the provider said no, and it is the opinion
    of one pod. The recorded rejection is the newer fact."""
    assert connection_state(_Token(refresh_token="rt"), "connected", True) == EXPIRED


def test_a_rejection_is_recorded_only_for_a_credential_failure() -> None:
    """A timeout or a DNS blip is not evidence the credential is dead, and
    marking it would send someone through consent to fix a network problem."""
    from shared.base_connector import _is_credential_rejection

    assert _is_credential_rejection(ValueError("invalid_grant: Token has been expired or revoked."))
    assert _is_credential_rejection(RuntimeError("AADSTS700082: refresh token expired due to inactivity"))
    assert not _is_credential_rejection(TimeoutError("read timed out"))
    assert not _is_credential_rejection(ConnectionError("Temporary failure in name resolution"))


def test_a_successful_save_clears_the_rejection() -> None:
    """A connector that re-authorised must not keep showing "needs sign-in"."""
    src = (_CORE / "services" / "connector_store.py").read_text()
    save = src[src.index("async def save_connector_tokens") : src.index("async def mark_refresh_failed")]
    assert "clear_refresh_failure" in save


def test_the_rejection_is_persisted_not_just_held_in_memory() -> None:
    """It was written to `self._status` and lost on the next pod restart — the
    card went back to Connected on its own, having learned nothing."""
    sdk = (_CORE / "shared" / "base_connector.py").read_text()
    start = sdk.index("Token refresh failed")
    window = sdk[start : start + 1600]
    assert "mark_refresh_failed" in window
