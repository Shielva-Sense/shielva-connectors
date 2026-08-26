"""Platform-owned OAuth apps — the thing that removes credential setup.

Connecting Slack meant the customer registered their own Slack app and pasted a
bot token; Teams meant an Azure app registration. That is a developer task
standing in front of a product feature. These pin the rules that make one-click
safe: a tenant's own app is never overridden, a half-configured platform app is
treated as absent, and credentials are never handed back over the wire.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from services.platform_apps import apply_platform_app, platform_app_available, platform_credentials


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for v in (
        "SLACK_APP_CLIENT_ID",
        "SLACK_APP_CLIENT_SECRET",
        "TEAMS_APP_CLIENT_ID",
        "TEAMS_APP_CLIENT_SECRET",
        "WHATSAPP_APP_ID",
        "WHATSAPP_APP_SECRET",
    ):
        monkeypatch.delenv(v, raising=False)


def test_no_platform_app_means_no_one_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before the apps are registered, the UI must keep showing the credential
    form — a Connect button here would lead to a broken consent screen."""
    assert platform_app_available("slack") is False
    assert apply_platform_app("slack", {"bot_token": "x"}) == {"bot_token": "x"}


def test_a_registered_app_enables_one_click(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_APP_CLIENT_SECRET", "sec")

    assert platform_app_available("slack") is True
    assert apply_platform_app("slack", {}) == {"client_id": "cid", "client_secret": "sec"}


def test_half_a_credential_pair_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """🚨 All-or-nothing. A client_id with no secret produces an OAuth error
    that reads like the customer got something wrong."""
    monkeypatch.setenv("SLACK_APP_CLIENT_ID", "cid")

    assert platform_app_available("slack") is False
    assert platform_credentials("slack") == {}
    assert apply_platform_app("slack", {}) == {}


def test_a_blank_env_var_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty value set in a manifest is the common way this goes wrong."""
    monkeypatch.setenv("SLACK_APP_CLIENT_ID", "  ")
    monkeypatch.setenv("SLACK_APP_CLIENT_SECRET", "sec")

    assert platform_app_available("slack") is False


def test_a_tenants_own_app_is_never_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    """🚨 The rule that matters most. Some enterprises require their own app
    registration for audit; substituting ours would silently change which
    identity is acting on their workspace."""
    monkeypatch.setenv("SLACK_APP_CLIENT_ID", "platform")
    monkeypatch.setenv("SLACK_APP_CLIENT_SECRET", "platform-secret")

    out = apply_platform_app("slack", {"client_id": "theirs", "client_secret": "theirs-secret"})

    assert out == {"client_id": "theirs", "client_secret": "theirs-secret"}


def test_a_partially_supplied_config_is_completed_not_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAMS_APP_CLIENT_ID", "platform")
    monkeypatch.setenv("TEAMS_APP_CLIENT_SECRET", "platform-secret")

    out = apply_platform_app("microsoft_teams", {"client_id": "theirs"})

    assert out["client_id"] == "theirs", "what they gave stands"
    assert out["client_secret"] == "platform-secret", "what they omitted is filled"


def test_other_config_keys_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_APP_CLIENT_SECRET", "sec")

    out = apply_platform_app("slack", {"redirect_uri": "https://x/cb"})

    assert out["redirect_uri"] == "https://x/cb"


def test_an_unknown_connector_type_gets_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a partial config that fails later with a confusing error."""
    monkeypatch.setenv("SLACK_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_APP_CLIENT_SECRET", "sec")

    assert platform_app_available("jira") is False
    assert apply_platform_app("jira", {"a": 1}) == {"a": 1}


def test_the_original_config_is_not_mutated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller's dict is the request body; mutating it would leak platform
    credentials into anything that logs or re-reads the request."""
    monkeypatch.setenv("SLACK_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_APP_CLIENT_SECRET", "sec")
    original = {"redirect_uri": "https://x/cb"}

    apply_platform_app("slack", original)

    assert original == {"redirect_uri": "https://x/cb"}
