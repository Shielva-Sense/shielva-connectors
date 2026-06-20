# Setup Instructions: Xero Accounting

## Overview

The Xero connector integrates your Xero accounting organisation with the Shielva platform. Once connected, Shielva syncs invoices, contacts, and chart-of-accounts entries into your knowledge base for search, reporting, and automation. The connector uses Xero's OAuth 2.0 PKCE flow — your team never shares a password with Shielva; instead, Xero issues short-lived access tokens that the connector renews automatically via a refresh token.

This connector requires a Xero developer app with the OAuth 2.0 client credentials (Client ID and Client Secret).

---

## Prerequisites

Before you begin, make sure you have:

- A **Xero account** with administrator access to the organisation you want to connect
- A **Xero Developer account** at [developer.xero.com](https://developer.xero.com) — free, separate from your Xero subscription
- A **Xero App** created in the Developer Portal (Web App type, OAuth 2.0)
- The Shielva **redirect URI** provided by your platform administrator — you will add it to the Xero app's redirect URIs before connecting

---

## Step-by-Step Configuration

### Step 1: Create a Xero App

1. Sign in to the [Xero Developer Portal](https://developer.xero.com/myapps/).
2. Click **New app**.
3. Fill in:
   - **App name**: `Shielva` (or any descriptive name)
   - **Integration type**: **Web app**
   - **Company or application URL**: your company URL
   - **OAuth 2.0 redirect URI**: paste the redirect URI provided by Shielva (format: `https://app.shielva.ai/connectors/xero/callback`). Click **Add URI**.
4. Accept the terms and click **Create app**.

---

### Step 2: Client ID (`client_id`) — **Required**

1. In the Xero Developer Portal, open your newly created app.
2. On the **Configuration** tab, copy the **Client ID** field.
   - Format: a long alphanumeric string, e.g. `ABCD1234EF5678ABCD1234EF5678ABCD`
3. Paste it into the **Client ID** field in Shielva.

> **Tip:** The Client ID is not a secret and is safe to copy into a text editor.

---

### Step 3: Client Secret (`client_secret`) — **Required**

1. On the same **Configuration** tab, scroll to **Client secret**.
2. Click **Generate a secret**. Xero shows the secret **once** — copy it immediately.
3. Paste it into the **Client Secret** field in Shielva. This field is stored encrypted.

> **Warning:** If you close the dialog without copying, click **Generate a secret** again — the previous secret is immediately invalidated. If you regenerate the secret later, you must update this field in Shielva.

---

### Step 4: Redirect URI (`redirect_uri`) — **Optional**

- **Default**: Shielva uses the platform-configured redirect URI automatically.
- Only fill in this field if your Shielva deployment uses a custom redirect URI that differs from the default.
- The value must exactly match one of the redirect URIs registered in Step 1.
- Example: `https://app.shielva.ai/connectors/xero/callback`

---

### Step 5: Complete OAuth2 Authorization

After entering your credentials and saving the connector, Shielva will present an **Authorize** button. Clicking it will:

1. Redirect you to the Xero login page.
2. Ask you to select the Xero organisation you want to connect.
3. Show the requested permissions (read accounting transactions and contacts).
4. Redirect back to Shielva with an authorization code.

Shielva automatically exchanges the code for tokens and begins the initial sync.

---

## Permissions (Scopes)

The connector requests the following OAuth2 scopes:

| Scope | Purpose |
|---|---|
| `accounting.transactions` | Read invoices and financial transactions |
| `accounting.contacts` | Read contacts (customers, suppliers) |
| `offline_access` | Obtain a refresh token for unattended token renewal |

These are read-only scopes — the connector cannot create, update, or delete any data in your Xero organisation.

---

## What Gets Synced

| Resource | Xero Endpoint | Incremental Support |
|---|---|---|
| Invoices | `GET /api.xro/2.0/Invoices` | Yes — `If-Modified-Since` header |
| Contacts | `GET /api.xro/2.0/Contacts` | Yes — `If-Modified-Since` header |
| Accounts | `GET /api.xro/2.0/Accounts` | No — always full fetch |

- **Full sync**: fetches all records regardless of age.
- **Incremental sync**: fetches only records modified after the last sync timestamp.

---

## Troubleshooting

### "client_id and client_secret are required"

You submitted the connector form with one or both credential fields empty. Return to the connector settings and fill in both the **Client ID** and **Client Secret**.

### "Token expired — re-authorize the connector"

Xero refresh tokens expire after 60 days of inactivity. Click **Re-authorize** in the connector settings to complete the OAuth flow again. Your synced data is preserved.

### "Connection refused" / network errors

Verify that your Shielva deployment can reach `api.xero.com` and `identity.xero.com` on port 443. Check your firewall or egress allowlist if you are running Shielva on-premises.

### "Forbidden (403)" after authorization

The Xero user who authorized the app may not have access to the organisation, or the app's scopes may have been changed. Revoke and re-authorize the connector.

### Rate limiting (429)

Xero enforces a limit of 60 API calls per minute per app. The connector automatically retries with a 60-second backoff. If you see persistent rate-limit errors during large syncs, reduce the sync frequency in your Shielva schedule settings.

---

## Security Notes

- Client secrets are stored AES-256-GCM encrypted at rest in the Shielva platform.
- Access tokens are kept only in memory and are never logged.
- Refresh tokens are encrypted in the Shielva credential store and rotated automatically when Xero issues a new one.
- The connector uses PKCE (Proof Key for Code Exchange) to protect the authorization code flow against interception attacks.
- All communication with Xero APIs uses TLS 1.2+.

---

## Revocation

To disconnect Xero from Shielva:

1. In Shielva, go to **Connectors → Xero → Settings** and click **Disconnect**.
2. Optionally, revoke the app's access in Xero: **Settings → Connected apps** in your Xero organisation, find Shielva, and click **Disconnect**.

Revoking access in Xero immediately invalidates the access and refresh tokens — the Shielva connector will show **Offline** on the next health check.
