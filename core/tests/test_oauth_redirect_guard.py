"""A redirect_uri that points anywhere but the connector callback is refused.

🚨 The failure this prevents, seen live with Google Calendar. `redirect_uri` is
an optional install field, and somebody reasonably pastes whichever URI is
already registered on their OAuth client — for Google that is usually Shielva's
own SSO callback, /auth/oidc/google/callback. The provider accepts it (it IS
registered), sends the user there, and the SIGN-IN handler receives a
connector's state and answers

    {"code":"http_400","message":"Invalid state parameter or session expired"}

as raw JSON on a login route. Nothing in that mentions connectors, and there is
no thread back to the field that caused it.
"""

from __future__ import annotations

import pytest
from core.gateway import _validated_redirect
from fastapi import HTTPException

DEFAULT = "https://api.shielva.ai/connectors/oauth/callback"


def test_blank_falls_back_to_the_deployment_default() -> None:
    assert _validated_redirect("", DEFAULT) == DEFAULT
    assert _validated_redirect("   ", DEFAULT) == DEFAULT


def test_the_sso_callback_is_refused_with_the_fix_in_the_message() -> None:
    with pytest.raises(HTTPException) as e:
        _validated_redirect("https://api.shielva.ai/auth/oidc/google/callback", DEFAULT)
    assert e.value.status_code == 400
    detail = str(e.value.detail)
    # The message has to carry the URL to register, or it just says "no".
    assert "https://api.shielva.ai/connectors/oauth/callback" in detail


def test_another_host_is_allowed_on_the_right_path() -> None:
    """The host is deliberately not pinned — a deployment on its own domain, or
    behind an enterprise proxy, is legitimate. The PATH is what decides which
    handler receives the code."""
    other = "https://connect.acme.example/connectors/oauth/callback"
    assert _validated_redirect(other, DEFAULT) == other


def test_a_trailing_slash_is_still_the_callback() -> None:
    assert _validated_redirect(DEFAULT + "/", DEFAULT) == DEFAULT + "/"


@pytest.mark.parametrize("bad", ["not-a-url", "/connectors/oauth/callback", "ftp://x/connectors/oauth/callback"])
def test_a_non_absolute_or_non_http_url_is_refused(bad: str) -> None:
    with pytest.raises(HTTPException):
        _validated_redirect(bad, DEFAULT)


# ── who owns the redirect ────────────────────────────────────────────────────


def test_a_managed_install_ignores_a_supplied_redirect() -> None:
    """🚨 The 400 this removes. The install form shows a redirect URI field with
    a placeholder, somebody reasonably types it, and the guard refused the very
    value the console had proposed — a request rejected for following the UI.

    On a managed install the redirect belongs to Shielva's app, so a value from
    the customer is not a choice they get to make. Ignored, not rejected.
    """
    from core.gateway import _redirect_for

    for supplied in (
        "https://app.shielva.ai/connectors/callback",  # the placeholder
        "https://localhost:8000/connectors/oauth/callback",
        "https://api.shielva.ai/auth/oidc/google/callback",  # the SSO callback
        "",
    ):
        assert _redirect_for(supplied, DEFAULT, "managed") == DEFAULT


def test_a_self_install_still_owns_and_is_checked() -> None:
    """It genuinely is theirs, so a wrong path must still be refused — that is
    what stops consent landing on a handler that knows nothing about it."""
    from core.gateway import _redirect_for

    own = "https://connect.acme.example/connectors/oauth/callback"
    assert _redirect_for(own, DEFAULT, "self") == own
    with pytest.raises(HTTPException):
        _redirect_for("https://api.shielva.ai/auth/oidc/google/callback", DEFAULT, "self")
