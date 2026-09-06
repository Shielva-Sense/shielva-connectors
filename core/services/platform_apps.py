"""Platform-owned OAuth apps, so a customer never handles a credential.

🚨 The problem this exists to remove. Connecting Slack meant the customer
created their own Slack app, found the bot token, and pasted it in; Teams meant
an Azure app registration for client_id and client_secret. That is a developer
task standing in front of a product feature, and most customers will simply not
do it.

A platform app inverts that: Shielva registers ONE Slack app and ONE Azure app,
their credentials live here in the pod's environment, and the customer clicks
Connect and approves a consent screen. Which is what every other product does,
and what people expect.

The credentials are read from the environment, never from the request, so a
tenant cannot supply or read another's. A tenant that DOES bring its own app
still wins — some enterprises require their own registration for audit, and
silently overriding that with ours would be worse than the setup cost.

Empty env means the platform app is not registered yet: install then behaves
exactly as it did before, asking the customer for their own credentials. So
this can ship before the apps exist, and turning them on is a config change
rather than a deploy.
"""

from __future__ import annotations

import os

# Env var per connector type. Only these keys are ever injected — an unknown
# connector type gets nothing, rather than a partial config that fails later
# with a confusing error.
_PLATFORM_APPS: dict[str, dict[str, str]] = {
    "slack": {
        "client_id": "SLACK_APP_CLIENT_ID",
        "client_secret": "SLACK_APP_CLIENT_SECRET",
    },
    "microsoft_teams": {
        "client_id": "TEAMS_APP_CLIENT_ID",
        "client_secret": "TEAMS_APP_CLIENT_SECRET",
    },
    "whatsapp": {
        "app_id": "WHATSAPP_APP_ID",
        "app_secret": "WHATSAPP_APP_SECRET",
    },
    # 🚨 Calendars belong here for the same reason Slack does, and their absence
    # was doing real damage: both asked the CUSTOMER for client_id and
    # client_secret as required fields. That means every customer registering
    # their own Google Cloud project or Azure app before they can book a call
    # back — a developer task standing in front of a product feature — and it is
    # also how a redirect_uri pointing at Shielva's SSO callback got pasted in,
    # because that was the URI already registered on the app they had.
    #
    # With the platform app, a customer clicks Connect, approves consent, and
    # any Google or Microsoft account links.
    "google_calendar": {
        "client_id": "GOOGLE_CALENDAR_APP_CLIENT_ID",
        "client_secret": "GOOGLE_CALENDAR_APP_CLIENT_SECRET",
    },
    "outlook_calendar": {
        "client_id": "OUTLOOK_CALENDAR_APP_CLIENT_ID",
        "client_secret": "OUTLOOK_CALENDAR_APP_CLIENT_SECRET",
    },
    # The rest of each provider's family. No per-type env of their own: they
    # resolve through _PROVIDER_APPS, so one Google client and one Azure
    # registration cover all of them and a rotation is one edit.
    #
    # 🚨 google_gmail_connector is the one with a price attached. Gmail's scopes
    # are RESTRICTED, not merely sensitive, so a public app using them needs
    # Google's verification AND an annual third-party security assessment — paid
    # for by whoever owns the app, which is now us rather than each customer.
    # Listed because the alternative is a console where Calendar connects in one
    # click and Gmail demands a Google Cloud project, which reads as broken.
    "google_gmail_connector": {},
    "google_drive": {},
    "google_sheets": {},
    "google_analytics": {},
    "outlook_mail": {},
    "sharepoint": {},
    # No provider app covers Calendly, so this one keeps its own pair. Listed
    # for the same reason as the rest: a console where seven connectors connect
    # in one click and the eighth demands a developer registration reads as
    # broken rather than as a considered distinction.
    "calendly": {
        "client_id": "CALENDLY_APP_CLIENT_ID",
        "client_secret": "CALENDLY_APP_CLIENT_SECRET",
    },
}


#: How a connector's OAuth credentials are sourced. Stored on the connector's
#: config so a later re-auth or deploy cannot silently switch identity.
MODE_MANAGED = "managed"
MODE_SELF = "self"
CREDENTIAL_MODE_KEY = "credential_mode"


def default_credential_mode(connector_type: str, provider: str | None = None) -> str:
    """Managed wherever we have an app, self everywhere else."""
    return MODE_MANAGED if platform_app_available(connector_type, provider) else MODE_SELF


def credential_mode(connector_type: str, config: dict, provider: str | None = None) -> str:
    """Which app this install uses — ours or the customer's.

    🚨 An explicit choice, because the implicit one was unreachable. The rule
    used to be "the caller's values win", which is correct but invisible: as
    soon as a platform app existed the UI stopped rendering the credential
    fields at all, so an organisation that MUST use its own registration — for
    app-inventory, admin-consent policy or API quota reasons — had nowhere to
    type them.

    An unknown value is treated as managed rather than rejected: the failure
    mode of guessing wrong here is a consent screen, not a data leak, and a
    hard error would break every install that predates this field.
    """
    chosen = str(config.get(CREDENTIAL_MODE_KEY) or "").strip().lower()
    if chosen == MODE_SELF:
        return MODE_SELF
    if chosen == MODE_MANAGED:
        return MODE_MANAGED
    return default_credential_mode(connector_type, provider)


def platform_app_types() -> list[str]:
    """Every connector type that CAN have a platform app.

    🚨 Exists because the /connectors/platform-apps endpoint carried its own
    literal ("slack", "microsoft_teams", "whatsapp"). That is a second copy of
    this module's keys, and it drifted the moment the calendars were added here:
    the map said they were platform apps, the endpoint never asked about them,
    and the UI went on demanding client_id and client_secret from customers with
    nothing to show that anything was wrong.
    """
    return sorted(_PLATFORM_APPS)


#: One app per PROVIDER, because a Google Cloud OAuth client serves every Google
#: API and one Azure registration serves all of Graph — scopes are requested per
#: authorization, not baked into the client. Without this the same client_id and
#: secret would be sealed once per connector type, and a rotation would mean
#: editing five copies and finding out about the one you missed at re-auth.
#:
#: 🚨 A provider app does NOT enable a connector on its own. The type must still
#: be listed in _PLATFORM_APPS. Auto-enabling everything a provider covers would
#: quietly turn Gmail into a platform app the moment a Google app was registered,
#: and Gmail's scopes are RESTRICTED — that is an annual third-party security
#: assessment, taken on by whoever owns the app. That has to be a decision.
_PROVIDER_APPS: dict[str, dict[str, str]] = {
    "google": {
        "client_id": "GOOGLE_APP_CLIENT_ID",
        "client_secret": "GOOGLE_APP_CLIENT_SECRET",
    },
    "microsoft": {
        "client_id": "MICROSOFT_APP_CLIENT_ID",
        "client_secret": "MICROSOFT_APP_CLIENT_SECRET",
    },
}


def _read(env_map: dict[str, str]) -> dict[str, str]:
    """All-or-nothing: half a credential pair produces an OAuth error that reads
    like the customer's fault."""
    creds = {field: os.getenv(env, "").strip() for field, env in env_map.items()}
    return creds if all(creds.values()) else {}


def platform_app_available(connector_type: str, provider: str | None = None) -> bool:
    """Whether a customer can connect this type with one click.

    The UI asks this before offering a Connect button, so an unregistered app
    shows the credential form rather than a button that leads to a broken
    consent screen.
    """
    return bool(platform_credentials(connector_type, provider))


def platform_credentials(connector_type: str, provider: str | None = None) -> dict[str, str]:
    """The platform app's credentials for this type, or {} if unregistered.

    Per-type environment wins, then the provider-wide app. The per-type override
    exists so one connector can be moved onto its own registration — a different
    Google project for a heavier API quota, say — without disturbing the others.

    `provider` comes from the connector catalogue, which already declares it.
    Deriving it from the type name, or keeping a second type-to-provider map
    here, is the same mistake as the endpoint that kept its own copy of this
    module's keys and silently drifted from it.
    """
    # 🚨 `is None`, not falsiness. An entry with an EMPTY map is the marker for
    # "managed, credentials come from the provider app" — and `if not keys`
    # treated that as "not registered at all", so every connector added that way
    # silently stayed unmanaged.
    keys = _PLATFORM_APPS.get(connector_type)
    if keys is None:
        return {}
    return _read(keys) or _read(_PROVIDER_APPS.get((provider or "").strip().lower(), {}))


def apply_platform_app(connector_type: str, config: dict, provider: str | None = None) -> dict:
    """Fill in the platform app's credentials for anything the caller omitted.

    🚨 The caller's own values always win. A tenant that brings its own app
    registration — which some enterprises require for audit — must keep it, and
    quietly substituting ours would be a silent change of identity on their
    workspace.

    Skipped entirely when the install is marked `self`: an organisation that has
    chosen its own app must get an error naming the credential it forgot, not
    ours quietly standing in for it. That is the difference between "you missed a
    field" and a consent screen carrying the wrong company's name.
    """
    if credential_mode(connector_type, config or {}, provider) == MODE_SELF:
        return config
    creds = platform_credentials(connector_type, provider)
    if not creds:
        return config
    merged = dict(config)
    for field, value in creds.items():
        if not str(merged.get(field, "")).strip():
            merged[field] = value
    return merged


def platform_app_fields(connector_type: str, provider: str | None = None) -> list[str]:
    """Which install fields the platform app supplies for this type.

    The UI must not ask a customer for a credential we already hold — that is
    how someone ends up registering their own Azure app to use ours. It also
    must not guess the field NAMES: slack and teams use client_id/client_secret,
    whatsapp uses app_id/app_secret. So the mapping answers, not the caller.
    """
    return sorted(platform_credentials(connector_type, provider))
