# Drift Connector — Setup Guide

## Overview

The Drift connector syncs conversations, contacts, accounts, and messages from your Drift workspace into Shielva via the Drift REST API v1. It uses OAuth 2.0 Authorization Code flow for authentication.

---

## Step 1 — Create a Drift OAuth App

1. Log in to your Drift account at [app.drift.com](https://app.drift.com).
2. Go to **Settings** (gear icon, bottom-left).
3. Navigate to **Integrations → App Settings** (or **Developer Apps** depending on your plan tier).
4. Click **New App** to create a new OAuth application.
5. Give the app a name (e.g., "Shielva Integration") and fill in the description.

---

## Step 2 — Configure OAuth Scopes

During app creation or under the app's **Scopes** tab, add the following scopes:

| Scope | Purpose |
|-------|---------|
| `conversation_read` | Read conversations and messages |
| `contact_read` | Read contacts |

Save the app configuration.

---

## Step 3 — Gather your install fields

Once the app is created, copy the following values:

| Field | Where to find it | Example |
|-------|-----------------|---------|
| **Client ID** | App overview page under "App Credentials" | `a1b2c3d4e5f6...` |
| **Client Secret** | App overview page under "App Credentials" (click Show) | `secret_abc123...` |
| **Redirect URI** | Set in the app's "Redirect URLs" section — must match the URI you enter here | `https://app.shielva.com/oauth/drift/callback` |

The Redirect URI must be registered in your Drift app. Use the Shielva ACP redirect URI shown on the connector install screen.

---

## Step 4 — Install the Connector in Shielva ACP

1. In the Shielva ACP, navigate to **Integrations → Drift**.
2. Enter:
   - **Client ID** — from Step 3
   - **Client Secret** — from Step 3
   - **Redirect URI** — from Step 3 (optional if using the default)
3. Click **Install**.
4. You will be redirected to the Drift authorization page to grant access.
5. After authorizing, you will be redirected back to Shielva. Status shows **ONLINE** on success.

---

## Step 5 — Run your first sync

Once installed, trigger a sync from the Integrations dashboard or via the API:

```python
async with DriftConnector(config={
    "access_token": "<your_oauth_access_token>",
    "client_id": "<your_client_id>",
    "client_secret": "<your_client_secret>",
}) as conn:
    result = await conn.sync(full=True)
    print(f"Synced {result.documents_synced} documents")
```

---

## OAuth Flow Details

- **Authorization URL**: `https://dev.drift.com/authorize`
- **Token URL**: `https://driftapi.com/auth/token`
- **Grant type**: Authorization Code
- **Scopes**: `conversation_read`, `contact_read`

---

## What gets synced

| Resource | Description |
|----------|-------------|
| Conversations | All Drift conversations (open, closed, pending) |
| Contacts | All Drift contacts (known visitors, leads) |
| Accounts | All Drift accounts (companies) |
| Messages | Messages within each conversation |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `INVALID_CREDENTIALS` on install | Wrong client ID / secret or expired token | Re-authorize via the OAuth flow |
| `MISSING_CREDENTIALS` | `access_token` not yet obtained | Complete the OAuth flow first |
| 429 Rate Limited | Drift API rate limit exceeded | The connector retries automatically with backoff |
| Conversations missing | Scope `conversation_read` not granted | Re-create the app with both scopes |

---

## Security notes

- Access tokens are stored encrypted in the Shielva vault and never logged.
- All requests go over HTTPS to `https://driftapi.com`.
- Rotate tokens by re-authorizing the app from the ACP Integrations page.
- The client secret is stored in the Shielva secrets store, not in plaintext.
