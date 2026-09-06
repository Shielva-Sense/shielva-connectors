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
    CREDENTIAL_MODE_KEY,
    MODE_MANAGED,
    MODE_SELF,
    apply_platform_app,
    credential_mode,
    persistable_config,
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
    assert apply_platform_app("slack", {}) == {
        "client_id": "cid",
        "client_secret": "sec",
        CREDENTIAL_MODE_KEY: MODE_MANAGED,
    }


def test_slack_without_env_is_not_one_click() -> None:
    assert platform_app_available("slack") is False


def test_teams_without_env_is_not_one_click() -> None:
    assert platform_app_available("microsoft_teams") is False


def test_a_registered_app_enables_one_click(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMS_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("TEAMS_APP_CLIENT_SECRET", "sec")

    assert platform_app_available("microsoft_teams") is True
    assert apply_platform_app("microsoft_teams", {}) == {
        "client_id": "cid",
        "client_secret": "sec",
        CREDENTIAL_MODE_KEY: MODE_MANAGED,
    }


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

    assert out == {
        "client_id": "theirs",
        "client_secret": "theirs-secret",
        CREDENTIAL_MODE_KEY: MODE_SELF,
    }


def test_a_partial_config_is_never_completed_from_the_platform_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🚨 This used to complete the pair, and the result could not authenticate.

    A client_id from the customer's app paired with a client_secret from ours is
    not a credential — the halves belong to different OAuth apps. Supplying one
    of these fields now means "I am bringing my own", so the config is left
    alone and the provider reports the field that is actually missing.
    """
    monkeypatch.setenv("TEAMS_APP_CLIENT_ID", "platform")
    monkeypatch.setenv("TEAMS_APP_CLIENT_SECRET", "platform-secret")

    out = apply_platform_app("microsoft_teams", {"client_id": "theirs"})

    assert out["client_id"] == "theirs", "what they gave stands"
    assert "client_secret" not in out, "half of our app must never be paired with half of theirs"


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


def test_every_path_that_needs_credentials_applies_the_platform_app() -> None:
    """🚨 install had it; check and deploy did not.

    /connectors/check is the endpoint the UI calls FIRST — it is what turns
    Connect into a consent URL. With the fill only on install, a customer
    clicking Connect got "Missing or invalid credentials" and was asked for a
    client_id and client_secret the platform already holds, which is the exact
    thing platform apps exist to prevent. Deploy needs it for the same reason:
    it regenerates the consent URL for anything not yet connected.
    """
    from pathlib import Path

    body = (Path(__file__).resolve().parents[1] / "gateway.py").read_text()

    def _handler(start_marker: str, end_marker: str) -> str:
        start = body.index(start_marker)
        return body[start : body.index(end_marker, start)]

    check = _handler('@app.post("/connectors/check")', '@app.post("/connectors/check/device-poll")')
    assert "apply_platform_app(" in check

    install = _handler('@app.post(\n    "/connectors/{connector_type}/install"', "async def check_connector_connection")
    assert "apply_platform_app(" in install


# ── managed vs self ──────────────────────────────────────────────────────────
# 🚨 The override was correct but unreachable. "The caller's values win" only
# helps if the caller can type them, and as soon as a platform app existed the
# UI stopped rendering the credential fields at all — so an organisation that
# MUST use its own registration (app inventory, admin-consent policy, API quota)
# had nowhere to put them.


def test_managed_is_the_default_where_we_have_an_app(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_ID", "pid")
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_SECRET", "psec")
    assert credential_mode("google_calendar", {}) == MODE_MANAGED
    assert apply_platform_app("google_calendar", {})["client_id"] == "pid"


def test_self_is_the_only_option_where_we_have_none(monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_CALENDAR_APP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_APP_CLIENT_SECRET", raising=False)
    assert credential_mode("google_calendar", {}) == MODE_SELF


def test_choosing_self_never_substitutes_our_app(monkeypatch) -> None:
    """🚨 The whole point. An org that chose its own app must get an error
    naming the field it forgot — not a consent screen carrying our name."""
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_ID", "pid")
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_SECRET", "psec")
    out = apply_platform_app("google_calendar", {CREDENTIAL_MODE_KEY: MODE_SELF})
    assert "client_id" not in out
    assert "client_secret" not in out


def test_self_keeps_the_organisations_own_values(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_ID", "pid")
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_SECRET", "psec")
    out = apply_platform_app(
        "google_calendar", {CREDENTIAL_MODE_KEY: MODE_SELF, "client_id": "theirs", "client_secret": "also-theirs"}
    )
    assert out["client_id"] == "theirs"
    assert out["client_secret"] == "also-theirs"


def test_an_unknown_mode_falls_back_rather_than_failing(monkeypatch) -> None:
    """Every install that predates this field has no mode at all; rejecting an
    unrecognised value would break them, and the cost of guessing wrong here is
    a consent screen, not a data leak."""
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_ID", "pid")
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_SECRET", "psec")
    assert credential_mode("google_calendar", {CREDENTIAL_MODE_KEY: "nonsense"}) == MODE_MANAGED
    assert credential_mode("google_calendar", {CREDENTIAL_MODE_KEY: ""}) == MODE_MANAGED
    assert credential_mode("google_calendar", {CREDENTIAL_MODE_KEY: "SELF"}) == MODE_SELF


# ── provider-shared credentials ──────────────────────────────────────────────
# One Google Cloud OAuth client serves every Google API, and one Azure
# registration serves all of Graph — scopes are requested per authorization, not
# baked into the client. Without sharing, the same client_id/secret is sealed
# once per connector type and a rotation means editing every copy and learning
# about the one you missed at re-auth.


def test_a_provider_app_covers_a_listed_connector(monkeypatch) -> None:
    for env in ("GOOGLE_CALENDAR_APP_CLIENT_ID", "GOOGLE_CALENDAR_APP_CLIENT_SECRET"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("GOOGLE_APP_CLIENT_ID", "shared-id")
    monkeypatch.setenv("GOOGLE_APP_CLIENT_SECRET", "shared-secret")
    assert platform_app_available("google_calendar", "google") is True
    assert apply_platform_app("google_calendar", {}, "google")["client_id"] == "shared-id"


def test_a_per_type_app_overrides_the_provider_one(monkeypatch) -> None:
    """So one connector can move to its own registration — a separate Google
    project for a heavier API quota — without disturbing the others."""
    monkeypatch.setenv("GOOGLE_APP_CLIENT_ID", "shared-id")
    monkeypatch.setenv("GOOGLE_APP_CLIENT_SECRET", "shared-secret")
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_ID", "calendar-id")
    monkeypatch.setenv("GOOGLE_CALENDAR_APP_CLIENT_SECRET", "calendar-secret")
    assert apply_platform_app("google_calendar", {}, "google")["client_id"] == "calendar-id"


def test_a_provider_app_does_not_enable_an_unlisted_connector(monkeypatch) -> None:
    """Listing is what enables a connector; a provider app only supplies the
    credentials. Otherwise registering one Google app would quietly turn every
    Google connector in the catalogue into a platform app."""
    monkeypatch.setenv("GOOGLE_APP_CLIENT_ID", "shared-id")
    monkeypatch.setenv("GOOGLE_APP_CLIENT_SECRET", "shared-secret")
    # Not in _PLATFORM_APPS at all.
    assert platform_app_available("dropbox", "dropbox") is False
    assert apply_platform_app("dropbox", {}, "dropbox") == {}


def test_gmail_is_managed_and_that_carries_a_cost(monkeypatch) -> None:
    """🚨 Recorded deliberately, because it is a commitment rather than a config
    line. Gmail's scopes are RESTRICTED — not merely sensitive — so a public app
    using them needs Google's verification AND an annual third-party security
    assessment, paid by whoever owns the app. That is now us rather than each
    customer.

    It is listed because the alternative was worse: a console where Calendar
    connects in one click and Gmail demands a Google Cloud project reads as
    broken, and customers do not know which scopes Google considers restricted.
    """
    monkeypatch.setenv("GOOGLE_APP_CLIENT_ID", "shared-id")
    monkeypatch.setenv("GOOGLE_APP_CLIENT_SECRET", "shared-secret")
    assert platform_app_available("google_gmail_connector", "google") is True


def test_an_empty_entry_means_use_the_provider_app(monkeypatch) -> None:
    """🚨 The sentinel that nearly did nothing. An entry with an empty map means
    "managed, credentials from the provider" — and the lookup guarded on
    falsiness, so every connector added that way read as unregistered."""
    monkeypatch.setenv("GOOGLE_APP_CLIENT_ID", "shared-id")
    monkeypatch.setenv("GOOGLE_APP_CLIENT_SECRET", "shared-secret")
    for connector in ("google_drive", "google_sheets", "google_analytics"):
        assert platform_app_available(connector, "google") is True
        assert apply_platform_app(connector, {}, "google")["client_id"] == "shared-id"


def test_the_eight_console_connectors_are_all_managed(monkeypatch) -> None:
    """Every connector the console offers connects in one click once its app is
    registered. The point is the absence of odd ones out: a catalogue where
    seven connect instantly and the eighth demands a Google Cloud project reads
    as broken, not as a considered distinction.
    """
    for env in (
        "GOOGLE_APP_CLIENT_ID",
        "GOOGLE_APP_CLIENT_SECRET",
        "MICROSOFT_APP_CLIENT_ID",
        "MICROSOFT_APP_CLIENT_SECRET",
        "CALENDLY_APP_CLIENT_ID",
        "CALENDLY_APP_CLIENT_SECRET",
        "SLACK_APP_CLIENT_ID",
        "SLACK_APP_CLIENT_SECRET",
    ):
        monkeypatch.setenv(env, "x")
    console = [
        ("google_calendar", "google"),
        ("google_drive", "google"),
        ("google_gmail_connector", "google"),
        ("microsoft_teams", "microsoft"),
        ("outlook_calendar", "microsoft"),
        ("outlook_mail", "microsoft"),
        ("calendly", "calendly"),
        ("slack", "slack"),
    ]
    unmanaged = [t for t, prov in console if not platform_app_available(t, prov)]
    assert unmanaged == []


def test_self_mode_still_wins_over_a_provider_app(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_APP_CLIENT_ID", "shared-id")
    monkeypatch.setenv("GOOGLE_APP_CLIENT_SECRET", "shared-secret")
    out = apply_platform_app("google_calendar", {CREDENTIAL_MODE_KEY: MODE_SELF}, "google")
    assert "client_id" not in out


def test_every_consent_url_path_honours_the_mode() -> None:
    """🚨 reauthorize did not, and it is the path that runs most: re-auth happens
    whenever the refresh token is gone, which for a Google app still in Testing
    is every seven days. It worked only because install had COPIED our
    credentials into the tenant's stored config — so rotating a platform secret
    would leave every managed connector re-authorising with the old one.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "gateway.py").read_text().splitlines()
    starts = [i for i, ln in enumerate(src) if ln.startswith("@app.")]
    for i, ln in enumerate(src):
        if "get_oauth_url(" not in ln or ln.strip().startswith("#"):
            continue
        start = max((h for h in starts if h < i), default=0)
        handler = "\n".join(src[start:i])
        assert "apply_platform_app(" in handler, f"consent URL built without the platform app at line {i + 1}"
        assert re.search(r"_provider_of\(", handler), f"platform app applied without a provider at line {i + 1}"


def test_a_managed_install_never_persists_our_secret(monkeypatch) -> None:
    """🚨 Our client secret used to be copied into every tenant's stored config.

    Two costs, and the second is the one that bites: the secret existed in as
    many places as there were installs, and a rotation reached none of them —
    the stale copy was "already set" and won at the next re-auth.
    """
    monkeypatch.setenv("GOOGLE_APP_CLIENT_ID", "platform-id")
    monkeypatch.setenv("GOOGLE_APP_CLIENT_SECRET", "platform-secret")

    merged = apply_platform_app("google_drive", {"folder_id": "abc"}, "google")
    assert merged["client_secret"] == "platform-secret"  # used for the exchange

    saved = persistable_config("google_drive", merged, "google")
    assert "client_secret" not in saved
    assert "client_id" not in saved
    assert saved["folder_id"] == "abc"
    # The decision survives, so the next read does not have to guess.
    assert saved[CREDENTIAL_MODE_KEY] == MODE_MANAGED


def test_a_self_install_keeps_the_credentials_the_customer_supplied(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_APP_CLIENT_ID", "platform-id")
    monkeypatch.setenv("GOOGLE_APP_CLIENT_SECRET", "platform-secret")

    theirs = {"client_id": "their-id", "client_secret": "their-secret"}
    merged = apply_platform_app("google_drive", theirs, "google")
    saved = persistable_config("google_drive", merged, "google")
    assert saved["client_id"] == "their-id"
    assert saved["client_secret"] == "their-secret"
    assert saved[CREDENTIAL_MODE_KEY] == MODE_SELF


def test_a_managed_config_reads_back_as_managed(monkeypatch) -> None:
    """The inference reads "carries credentials" as "a human supplied them", so a
    merge that leaves ours behind flips a managed install to self on the next
    read — and the platform app then stops being applied to it at all."""
    monkeypatch.setenv("MICROSOFT_APP_CLIENT_ID", "platform-id")
    monkeypatch.setenv("MICROSOFT_APP_CLIENT_SECRET", "platform-secret")

    merged = apply_platform_app("microsoft_teams", {}, "microsoft")
    saved = persistable_config("microsoft_teams", merged, "microsoft")
    assert credential_mode("microsoft_teams", saved, "microsoft") == MODE_MANAGED
    # ...and a rotation reaches it, because nothing stale is stored.
    monkeypatch.setenv("MICROSOFT_APP_CLIENT_SECRET", "rotated-secret")
    assert apply_platform_app("microsoft_teams", saved, "microsoft")["client_secret"] == "rotated-secret"


def test_every_persist_path_goes_through_persistable_config() -> None:
    """🚨 Seven copies of one filter is how the strip was written and then not
    used. A new persist site that hand-rolls the dict is the only way our secret
    can start spreading again, so the shape is pinned rather than the sites.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "gateway.py").read_text().splitlines()
    starts = [i for i, ln in enumerate(src) if ln.startswith("@app.")]
    for i, ln in enumerate(src):
        if "store_credentials(" not in ln or "def " in ln or ln.strip().startswith("#"):
            continue
        # The WHOLE handler, not the lines above the call: `store_credentials(`
        # opens a multi-line call and its argument — the filtered config — sits
        # on the lines below it.
        start = max((h for h in starts if h < i), default=0)
        end = min((h for h in starts if h > i), default=len(src))
        handler = "\n".join(src[start:end])
        assert "persistable_config(" in handler, (
            f"credentials persisted without persistable_config at gateway.py:{i + 1}"
        )
