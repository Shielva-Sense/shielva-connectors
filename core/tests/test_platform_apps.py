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

from services.platform_apps import (
    apply_platform_app,
    platform_app_available,
    platform_app_fields,
    platform_app_types,
    platform_credentials,
)


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


def test_slack_is_one_click_once_the_app_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slack implements OAuth v2 now, so the platform app reaches it.

    It was excluded while REQUIRED_CONFIG_KEYS was ["bot_token"] and there was
    no exchange — a Connect button then led nowhere.
    """
    monkeypatch.setenv("SLACK_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_APP_CLIENT_SECRET", "sec")

    assert platform_app_available("slack") is True
    assert apply_platform_app("slack", {}) == {"client_id": "cid", "client_secret": "sec"}


def test_slack_without_env_is_not_one_click() -> None:
    assert platform_app_available("slack") is False


def test_teams_without_env_is_not_one_click() -> None:
    assert platform_app_available("microsoft_teams") is False


def test_a_registered_app_enables_one_click(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMS_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("TEAMS_APP_CLIENT_SECRET", "sec")

    assert platform_app_available("microsoft_teams") is True
    assert apply_platform_app("microsoft_teams", {}) == {"client_id": "cid", "client_secret": "sec"}


def test_half_a_credential_pair_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """🚨 All-or-nothing. A client_id with no secret produces an OAuth error
    that reads like the customer got something wrong."""
    monkeypatch.setenv("TEAMS_APP_CLIENT_ID", "cid")

    assert platform_app_available("microsoft_teams") is False
    assert platform_credentials("microsoft_teams") == {}
    assert apply_platform_app("microsoft_teams", {}) == {}


def test_a_blank_env_var_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty value set in a manifest is the common way this goes wrong."""
    monkeypatch.setenv("TEAMS_APP_CLIENT_ID", "  ")
    monkeypatch.setenv("TEAMS_APP_CLIENT_SECRET", "sec")

    assert platform_app_available("microsoft_teams") is False


def test_a_tenants_own_app_is_never_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    """🚨 The rule that matters most. Some enterprises require their own app
    registration for audit; substituting ours would silently change which
    identity is acting on their workspace."""
    monkeypatch.setenv("TEAMS_APP_CLIENT_ID", "platform")
    monkeypatch.setenv("TEAMS_APP_CLIENT_SECRET", "platform-secret")

    out = apply_platform_app("microsoft_teams", {"client_id": "theirs", "client_secret": "theirs-secret"})

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
    monkeypatch.setenv("TEAMS_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("TEAMS_APP_CLIENT_SECRET", "sec")

    out = apply_platform_app("microsoft_teams", {"redirect_uri": "https://x/cb"})

    assert out["redirect_uri"] == "https://x/cb"


def test_an_unknown_connector_type_gets_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a partial config that fails later with a confusing error."""
    monkeypatch.setenv("TEAMS_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("TEAMS_APP_CLIENT_SECRET", "sec")

    assert platform_app_available("jira") is False
    assert apply_platform_app("jira", {"a": 1}) == {"a": 1}


def test_the_original_config_is_not_mutated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller's dict is the request body; mutating it would leak platform
    credentials into anything that logs or re-reads the request."""
    monkeypatch.setenv("TEAMS_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("TEAMS_APP_CLIENT_SECRET", "sec")
    original = {"redirect_uri": "https://x/cb"}

    apply_platform_app("microsoft_teams", original)

    assert original == {"redirect_uri": "https://x/cb"}


# ── calendars ────────────────────────────────────────────────────────────────
# 🚨 Both calendars asked the CUSTOMER for client_id and client_secret as
# required fields, so connecting one meant registering a Google Cloud project or
# an Azure app first — a developer task standing in front of a product feature.
# It is also how a redirect_uri pointing at Shielva's SSO callback got pasted in:
# that was the URI already registered on the app the customer happened to have.


def test_a_customer_is_never_asked_for_calendar_credentials(monkeypatch) -> None:
    for connector, env_id, env_secret in (
        ("google_calendar", "GOOGLE_CALENDAR_APP_CLIENT_ID", "GOOGLE_CALENDAR_APP_CLIENT_SECRET"),
        ("outlook_calendar", "OUTLOOK_CALENDAR_APP_CLIENT_ID", "OUTLOOK_CALENDAR_APP_CLIENT_SECRET"),
    ):
        monkeypatch.setenv(env_id, "platform-id")
        monkeypatch.setenv(env_secret, "platform-secret")
        assert platform_app_available(connector) is True
        assert platform_app_fields(connector) == ["client_id", "client_secret"]
        filled = apply_platform_app(connector, {})
        assert filled["client_id"] == "platform-id"
        assert filled["client_secret"] == "platform-secret"


def test_an_enterprise_bringing_its_own_calendar_app_still_wins(monkeypatch) -> None:
    """Some enterprises require their own registration for audit; quietly
    substituting ours is a silent change of identity on their workspace."""
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_ID", "platform-id")
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_SECRET", "platform-secret")
    out = apply_platform_app("google_calendar", {"client_id": "their-own", "client_secret": "theirs"})
    assert out["client_id"] == "their-own"
    assert out["client_secret"] == "theirs"


def test_unregistered_calendars_behave_exactly_as_before(monkeypatch) -> None:
    """This ships before the OAuth apps exist: with the env unset the install
    falls back to asking for credentials, rather than half-filling a config that
    fails later with an error that reads like the customer's fault."""
    for env in (
        "GOOGLE_CALENDAR_APP_CLIENT_ID",
        "GOOGLE_CALENDAR_APP_CLIENT_SECRET",
        "OUTLOOK_CALENDAR_APP_CLIENT_ID",
        "OUTLOOK_CALENDAR_APP_CLIENT_SECRET",
    ):
        monkeypatch.delenv(env, raising=False)
    for connector in ("google_calendar", "outlook_calendar"):
        assert platform_app_available(connector) is False
        assert platform_app_fields(connector) == []
        assert apply_platform_app(connector, {}) == {}


def test_half_a_credential_pair_is_not_used(monkeypatch) -> None:
    """A lone client_id produces an OAuth error that reads like the customer
    mistyped something."""
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_ID", "platform-id")
    monkeypatch.delenv("GOOGLE_CALENDAR_APP_CLIENT_SECRET", raising=False)
    assert platform_app_available("google_calendar") is False
    assert apply_platform_app("google_calendar", {}) == {}


def test_the_endpoint_list_is_derived_from_the_map_not_a_literal() -> None:
    """🚨 The bug this pins, which cost a whole build cycle.

    /connectors/platform-apps carried its own tuple —
    ("slack", "microsoft_teams", "whatsapp") — a second copy of this module's
    keys. Adding the calendars to the map therefore changed nothing the UI could
    see: the map said they were platform apps, the endpoint never asked about
    them, and customers were still required to supply client_id and
    client_secret with nothing to indicate anything was wrong.
    """
    import re
    from pathlib import Path

    gateway = Path(__file__).resolve().parents[1] / "gateway.py"
    body = gateway.read_text()
    handler = body[body.index('@app.get("/connectors/platform-apps")') : body.index('@app.get("/connectors/types")')]
    assert "platform_app_types()" in handler
    # No literal tuple of connector names left beside it.
    assert not re.search(r'\(\s*"slack"\s*,', handler)


def test_every_mapped_type_is_offered_once_registered(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_SECRET", "secret")
    types = platform_app_types()
    assert "google_calendar" in types
    assert "outlook_calendar" in types
    assert "slack" in types
    assert [t for t in types if platform_app_available(t)] == ["google_calendar"]
