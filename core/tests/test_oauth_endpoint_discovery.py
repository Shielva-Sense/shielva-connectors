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


def test_a_connector_declaring_nothing_falls_back_to_its_provider() -> None:
    """🚨 google_sheets 500'd on install declaring no endpoint anywhere. Google's
    authorize endpoint is one fact shared by drive, calendar, gmail and sheets;
    requiring each connector to restate it is what made the omission possible."""
    from shared.base_connector import _provider_endpoint

    class _Bare:
        config = {"provider": "google"}

    assert _provider_endpoint(_Bare(), "auth") == "https://accounts.google.com/o/oauth2/v2/auth"
    assert _provider_endpoint(_Bare(), "token") == "https://oauth2.googleapis.com/token"


def test_the_microsoft_fallback_excludes_personal_accounts() -> None:
    """`common` admits personal Microsoft accounts, which Graph then refuses
    AFTER sign-in with "personal account not allowed" — the exact dead end this
    console already hit once."""
    from shared.base_connector import _provider_endpoint

    class _Bare:
        config = {"provider": "microsoft"}

    assert "/organizations/" in _provider_endpoint(_Bare(), "auth")
    assert "/common/" not in _provider_endpoint(_Bare(), "auth")


def test_the_connectors_own_endpoint_still_wins_over_the_provider_default() -> None:
    c = _OutlookLike(azure_tenant="9f00a0de-0000-0000-0000-000000000000")
    c.config = {"provider": "microsoft"}
    assert "9f00a0de" in _discover_endpoint(c, _AUTH_URI_ALIASES, _META)


def test_the_microsoft_authority_is_configurable(monkeypatch) -> None:
    """🚨 Microsoft answered AADSTS53003 for /organizations/ and issued a token
    for the directory's own authority in the same second — a Conditional Access
    policy blocking the multi-tenant path. The user's sign-in read "You don't
    have the required permissions to access this org", which names neither the
    policy nor the authority."""
    from shared.base_connector import _provider_endpoint

    class _Pinned:
        config = {"provider": "microsoft", "azure_authority": "629c248d-a791-4921-9590-db05e5863356"}

    assert "629c248d-a791-4921-9590-db05e5863356" in _provider_endpoint(_Pinned(), "auth")
    assert "/organizations/" not in _provider_endpoint(_Pinned(), "token")


def test_the_default_authority_is_still_multi_tenant() -> None:
    """Unset means `organizations`: a work/school directory, and never `common`,
    which admits personal accounts that Graph refuses only after sign-in."""
    from shared.base_connector import _provider_endpoint

    class _Default:
        config = {"provider": "microsoft"}

    assert "/organizations/" in _provider_endpoint(_Default(), "auth")


def test_a_connector_that_names_its_own_azure_tenant_is_honoured() -> None:
    """outlook_mail calls it azure_tenant, sharepoint calls it tenant_hint. They
    mean one thing; which name a connector chose is an accident of when it was
    generated."""
    from shared.base_connector import _provider_endpoint

    class _ByTenantHint:
        config = {"provider": "microsoft", "tenant_hint": "contoso.onmicrosoft.com"}

    assert "contoso.onmicrosoft.com" in _provider_endpoint(_ByTenantHint(), "auth")
