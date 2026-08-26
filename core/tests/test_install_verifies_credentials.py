"""The install gate: a connector must not be installed on credentials that fail.

Pasting an arbitrary string as a Slack token used to produce a card reading
"Connected". The gateway called connector.install(), got back a result saying
the credentials were rejected, and then registered the connector, persisted its
config and returned 200 anyway — nothing ever read the result. The first sign
of trouble was a sync or a send failing later, on a different screen.

These cases pin the gate, and the two things that make it easy to get wrong:

  * PENDING is legitimate. An OAuth connector cannot be authenticated before
    the user has been through consent, so rejecting it would make OAuth
    impossible to install at all.
  * The comparison must be on the status VALUE. Connectors fall back to their
    OWN AuthStatus enum when the shared SDK is not importable, so an identity
    comparison against the SDK enum is False for exactly the connectors that
    most need checking — and a gate that always passes is worse than none.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class _ForeignAuthStatus(str, Enum):
    """A connector's OWN enum — deliberately not the shared SDK's."""

    CONNECTED = "connected"
    PENDING = "pending"
    INVALID_CREDENTIALS = "invalid_credentials"
    FAILED = "failed"
    MISSING_CREDENTIALS = "missing_credentials"


@dataclass
class _Result:
    auth_status: object
    health: str = "healthy"
    message: str = ""


def _gate(result: _Result) -> bool:
    """The gateway's ACTUAL decision function.

    🚨 Imported, not reimplemented. The first version of this test copied the
    comparison into the test file, which meant it could keep passing while the
    real gate drifted underneath it — and it left the gate itself uncovered, so
    the quality gate read 0% on the change that mattered most.

    It lives in services.install_gate rather than gateway so that reaching it
    does not mean importing the whole application.
    """
    from services.install_gate import install_auth_ok

    return install_auth_ok(result.auth_status)


@pytest.mark.parametrize(
    "status",
    [
        _ForeignAuthStatus.INVALID_CREDENTIALS,
        _ForeignAuthStatus.FAILED,
        _ForeignAuthStatus.MISSING_CREDENTIALS,
    ],
)
def test_rejected_credentials_do_not_install(status: _ForeignAuthStatus) -> None:
    assert not _gate(_Result(status)), f"{status.value} was accepted as a working install"


def test_a_connected_install_is_accepted() -> None:
    assert _gate(_Result(_ForeignAuthStatus.CONNECTED))


def test_oauth_pending_is_accepted_because_consent_has_not_happened_yet() -> None:
    """Rejecting PENDING would make every OAuth connector impossible to install:
    the authorization URL is only produced after this point."""
    assert _gate(_Result(_ForeignAuthStatus.PENDING))


def test_the_gate_reads_a_foreign_enum_correctly() -> None:
    """The regression that would silently disable the gate.

    _ForeignAuthStatus.INVALID_CREDENTIALS is not the SDK's enum member, so an
    identity check would treat it as "not a failure" and let it through.
    """
    foreign = _ForeignAuthStatus.INVALID_CREDENTIALS
    try:
        from shared.base_connector import AuthStatus as SdkAuthStatus
    except ImportError:
        pytest.skip("shared SDK not importable in this environment")

    assert foreign is not SdkAuthStatus.INVALID_CREDENTIALS, "premise: different enums"
    assert foreign.value == SdkAuthStatus.INVALID_CREDENTIALS.value, "premise: same value"
    assert not _gate(_Result(foreign)), "a foreign enum slipped past the gate"


def test_a_plain_string_status_is_handled() -> None:
    """Not every connector returns an enum."""
    assert not _gate(_Result("invalid_credentials"))
    assert _gate(_Result("connected"))
