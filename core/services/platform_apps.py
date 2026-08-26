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
# 🚨 Slack is NOT here, and that is not an oversight.
#
# The Slack connector declares AUTH_TYPE = "oauth2" but implements no OAuth
# exchange — no get_oauth_url, no authorize — and REQUIRED_CONFIG_KEYS is
# ["bot_token"]. Injecting a client_id/client_secret would therefore do
# nothing: the connector never reads them, there is no consent screen to send
# anyone to, and install would still fail asking for a bot token.
#
# Listing it here would make the UI show a Connect button that leads nowhere,
# which is worse than the token form it replaces. One-click Slack needs the
# OAuth v2 flow implemented in the connector first (authorize → exchange code →
# store bot_token); until then Slack shows the form, honestly.
_PLATFORM_APPS: dict[str, dict[str, str]] = {
    "microsoft_teams": {
        "client_id": "TEAMS_APP_CLIENT_ID",
        "client_secret": "TEAMS_APP_CLIENT_SECRET",
    },
    "whatsapp": {
        "app_id": "WHATSAPP_APP_ID",
        "app_secret": "WHATSAPP_APP_SECRET",
    },
}


def platform_app_available(connector_type: str) -> bool:
    """Whether a customer can connect this type with one click.

    The UI asks this before offering a Connect button, so an unregistered app
    shows the credential form rather than a button that leads to a broken
    consent screen.
    """
    keys = _PLATFORM_APPS.get(connector_type)
    if not keys:
        return False
    return all(os.getenv(env, "").strip() for env in keys.values())


def platform_credentials(connector_type: str) -> dict[str, str]:
    """The platform app's credentials for this type, or {} if unregistered."""
    keys = _PLATFORM_APPS.get(connector_type)
    if not keys:
        return {}
    creds = {field: os.getenv(env, "").strip() for field, env in keys.items()}
    # All-or-nothing: half a credential pair produces an OAuth error that reads
    # like the customer's fault.
    return creds if all(creds.values()) else {}


def apply_platform_app(connector_type: str, config: dict) -> dict:
    """Fill in the platform app's credentials for anything the caller omitted.

    🚨 The caller's own values always win. A tenant that brings its own app
    registration — which some enterprises require for audit — must keep it, and
    quietly substituting ours would be a silent change of identity on their
    workspace.
    """
    creds = platform_credentials(connector_type)
    if not creds:
        return config
    merged = dict(config)
    for field, value in creds.items():
        if not str(merged.get(field, "")).strip():
            merged[field] = value
    return merged
