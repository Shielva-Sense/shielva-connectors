# Slack Connector — Setup Guide

This guide walks you through configuring a Slack App and obtaining the Bot User OAuth Token required by the Shielva Slack connector.

---

## 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and sign in to your Slack account.
2. Click **Create New App**.
3. Choose **From scratch**.
4. Enter an **App Name** (e.g. "Shielva Connector") and select the **Workspace** you want to connect.
5. Click **Create App**.

---

## 2. Add Bot Token Scopes

1. In your new app's settings, navigate to **OAuth & Permissions** in the left sidebar.
2. Scroll down to **Scopes → Bot Token Scopes**.
3. Click **Add an OAuth Scope** and add the following scopes:

| Scope | Purpose |
|-------|---------|
| `channels:read` | List public channels |
| `channels:history` | Read message history in public channels |
| `users:read` | List workspace members |
| `groups:read` | List private channels (optional — needed for private_channel type) |
| `groups:history` | Read history in private channels (optional) |

Minimum required (public channels only): `channels:read`, `channels:history`, `users:read`.

---

## 3. Install App to Workspace

1. Still on **OAuth & Permissions**, scroll up and click **Install to Workspace**.
2. Review the permission request and click **Allow**.
3. After approval, copy the **Bot User OAuth Token** — it starts with `xoxb-`.

---

## 4. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **Bot Token** | `xoxb-your-token-here` |
| **Channel Types** | `public_channel` (default) — see below for options |

### Channel Types

The `channel_types` field accepts a comma-separated list:

| Value | Description |
|-------|-------------|
| `public_channel` | All public channels in the workspace (default) |
| `private_channel` | Private channels the bot is a member of (requires `groups:read` + `groups:history`) |
| `mpim` | Group direct messages |
| `im` | Direct messages with individual users |

Example: `public_channel,private_channel`

---

## 5. Invite Bot to Private Channels (if needed)

If you are syncing `private_channel` type, the bot must be explicitly invited to each private channel:

1. Open the private channel in Slack.
2. Type `/invite @YourAppName` and press Enter.

The bot can only read history from channels it is a member of.

---

## 6. Verify the Connection

Once installed, the connector calls `auth.test` to confirm the token is valid. A successful health check returns the workspace name.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `invalid_auth` | Wrong or revoked token | Re-copy the Bot User OAuth Token from **OAuth & Permissions** |
| `not_authed` | Token not included in request | Verify the token starts with `xoxb-` |
| `missing_scope` | Required scope not added | Go to **OAuth & Permissions → Bot Token Scopes**, add the scope, then reinstall the app |
| `channel_not_found` | Bot not in the channel or channel archived | Invite the bot to the channel or unarchive it |
| `account_inactive` | Workspace has been deactivated | Contact your Slack workspace admin |
| `ratelimited` | Too many API requests | Slack Tier 3 allows ~50 req/min. The connector retries automatically with exponential back-off |
| No messages returned | Channel has no messages in the sync window | The default window is the last 30 days. Messages older than that are not fetched |

### Slack API Rate Limits

| Tier | Limit | Endpoints |
|------|-------|-----------|
| Tier 1 | ~1 req/min | Rarely used |
| Tier 2 | ~20 req/min | `users.list` |
| Tier 3 | ~50 req/min | `conversations.list`, `conversations.history` |
| Tier 4 | ~100 req/min | `auth.test`, `users.info` |

The connector includes automatic retry with exponential back-off for `ratelimited` responses.

---

## Security Notes

- The Bot Token grants access to all channels the bot is invited to. Store it securely — do not share it or commit it to version control.
- Tokens can be revoked at any time from **OAuth & Permissions → Revoke Tokens**.
- Shielva stores the token encrypted at rest using the vault's AES-256-GCM encryption.
