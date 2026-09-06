"""Where an OAuth connector's authorize/token endpoints are found.

🚨 outlook_mail reported "auth_uri is not set" and returned a 500 on install,
for a connector that declares both endpoints. Microsoft's endpoints are
per-Azure-tenant templates, so the module constant is
`https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize` — a template,
not a URL — and the only resolved value lives on the instance, built in
__init__. The search looked at the class, the module and the packaged metadata,
and never at the object it was passed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from shared.base_connector import (
    _AUTH_URI_ALIASES,
    _TOKEN_URI_ALIASES,
    _discover_endpoint,
)

_META = ("authorization_url", "auth_uri", "authorize_url")


class _OutlookLike:
    """Shaped like outlook_mail: templates on the module, resolved on self."""

    def __init__(self, azure_tenant: str = "common") -> None:
        self.auth_url = f"https://login.microsoftonline.com/{azure_tenant}/oauth2/v2.0/authorize"
        self.token_url = f"https://login.microsoftonline.com/{azure_tenant}/oauth2/v2.0/token"


def test_the_resolved_instance_endpoint_is_found() -> None:
    c = _OutlookLike()
    assert _discover_endpoint(c, _AUTH_URI_ALIASES, _META) == c.auth_url
    assert _discover_endpoint(c, _TOKEN_URI_ALIASES, ("token_url", "token_uri")) == c.token_url


def test_the_instance_wins_so_consent_and_exchange_agree() -> None:
    """A connector installed against one Azure tenant must not be sent to consent
    at another. The instance value is what its own authorize() posts to."""

    class _WithStaleClassConstant(_OutlookLike):
        AUTH_URI = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"

    c = _WithStaleClassConstant(azure_tenant="7c4b6e9c-0000-0000-0000-000000000000")
    assert "7c4b6e9c" in _discover_endpoint(c, _AUTH_URI_ALIASES, _META)


def test_an_unformatted_template_is_never_returned() -> None:
    """Returning it produces a consent screen at a literal "{tenant}" — the
    plausible-looking wrong URL the discovery exists to avoid."""

    class _OnlyATemplate:
        AUTH_URI = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"

    assert _discover_endpoint(_OnlyATemplate(), _AUTH_URI_ALIASES, _META) is None
