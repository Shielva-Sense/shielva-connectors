"""The gateway must call connectors the way the SDK says connectors are written.

Two contract violations shipped at once, and each disguised the other:

  install() takes 1 positional argument but 2 were given
  authorize() got an unexpected keyword argument 'auth_code'

The first was caught and logged as a warning, so the method then ran against an
unhydrated connector and reported `401 Unauthorized: {}` — an authentication
error for a connector whose token was stored and valid.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

_SRC = (_CORE / "gateway.py").read_text()


def test_install_is_never_called_with_an_argument() -> None:
    """install() takes no arguments. Config belongs on connector.config, which is
    where install() and every request path already look."""
    offenders = [
        (i + 1, ln.strip())
        for i, ln in enumerate(_SRC.splitlines())
        if re.search(r"\.install\(\s*[^)\s]", ln) and "_install_with" not in ln
    ]
    assert offenders == [], f"install() called with an argument at {offenders}"


def test_the_hydrate_path_goes_through_the_helper() -> None:
    assert "_install_with(connector, _stored)" in _SRC


def test_a_wheel_upgrade_drops_live_instances_of_the_old_class() -> None:
    """Upgrading a wheel replaces the CLASS and does nothing to objects already
    built from the old one — so consent came back to a stale instance and died
    on the very argument the new wheel was published to accept."""
    assert "def evict_type(" in _SRC
    start = _SRC.index("async def _ensure_connector_installed(")
    body = _SRC[start : start + 6000]
    assert "registry.evict_type(" in body, (
        "the upgrade path must drop live instances of the replaced class, "
        "or the new wheel only reaches connectors nobody had installed"
    )


def test_the_sdk_can_complete_an_exchange_without_a_per_connector_override() -> None:
    """get_oauth_url() has always been generic; the exchange raised
    NotImplementedError and left every connector to reimplement the same form
    POST. Most never did, so the console sent users to consent with nothing to
    receive them."""
    sdk = (_CORE / "shared" / "base_connector.py").read_text()
    start = sdk.index("    async def authorize(self, auth_code")
    body = sdk[start : start + 6000]
    # The docstring names the old behaviour, so look at code only.
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith(("#", '"""', "🚨")))
    assert "raise NotImplementedError" not in code
    assert "grant_type" in body
    assert "authorization_code" in body
    assert "error_description" in body, "the provider's reason must survive a failed exchange"
