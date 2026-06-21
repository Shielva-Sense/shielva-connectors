# Setup Instructions: Keap

## Overview

The Keap (formerly Infusionsoft) connector integrates your Keap account with the Shielva platform. Once connected, Shielva can read and write your contacts, opportunities, orders, tags, and campaigns via the Keap REST API (v1) and OAuth 2.0.

This connector uses the **OAuth 2.0 Authorization Code** flow. Your team never shares a long-lived API key; Keap issues a short-lived access token (24h) plus a refresh token that the connector renews automatically on the next 401.

---

## Prerequisites

Before you begin, make sure you have:

- A **Keap account** (any plan that exposes the REST API — Pro, Max, Max Classic)
- **Account admin access** so you can register an OAuth application in the Keap Developer Portal
- The Shielva **redirect URI** your platform administrator provides — you will paste it into the Keap OAuth app before connecting

---

## Step-by-Step Configuration

### Step 1: Register an OAuth Application

1. Open the [Keap Developer Portal](https://keys.developer.keap.com) and sign in.
2. Click **+ New Application**. Give it a name (e.g. `Shielva Integration`).
3. In **Redirect URI**, paste the redirect URI Shielva provided. Save the app.
4. After creation, Keap shows your **Client ID** and **Client Secret**. Copy both.

### Step 2: OAuth2 Client ID (`client_id`) — **Required**

Paste the Client ID from Step 1 into the **OAuth2 Client ID** field in Shielva.

### Step 3: OAuth2 Client Secret (`client_secret`) — **Required**

Paste the Client Secret from Step 1 into the **OAuth2 Client Secret** field in Shielva. The secret is stored encrypted by the platform.

### Step 4: OAuth2 Scopes (`scopes`)

Default: `full`. Keap currently exposes a single catch-all `full` scope for the REST API. Leave the default unless Keap publishes a more granular scope list and your tenant only needs a subset.

### Step 5: OAuth2 Redirect URI (`redirect_uri`)

Must match the redirect URI you registered in the Keap Developer Portal exactly (including trailing slash). Leave blank in Shielva to let the gateway inject the value it manages at deploy time.

### Step 6: Authorize

1. Click **Connect** in the Shielva UI.
2. You will be redirected to Keap's consent screen. Sign in with the account that owns the Keap data you want Shielva to access and approve.
3. Keap will redirect back to Shielva, which will exchange the authorization code for an access token plus a refresh token. The refresh token is long-lived and lets Shielva renew access transparently.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Health check returns `401 Unauthorized` | Access token expired and refresh failed | Re-authorize the connector — your refresh token may have been revoked. |
| Health check returns `403 Forbidden` | The connected Keap account does not have permission for the requested resource | Connect with an admin-level Keap user. |
| Calls intermittently return `429 Rate limit exceeded` | Account is hitting Keap's per-minute quota | The connector retries automatically with backoff; if it persists, lower `rate_limit_per_min` or stagger sync schedules. |
| `404 Not Found` on `get_contact` / `update_contact` | The contact ID does not exist in this Keap tenant | Verify the ID via `list_contacts` first. |
| Authorization fails with `invalid_grant` | Authorization code already used or expired | Restart the OAuth flow from the Shielva UI. |
| `redirect_uri_mismatch` error during authorize | The `redirect_uri` field does not match what is registered in the Keap app | Open the Keap Developer Portal and confirm the URI matches exactly (including scheme, host, path, and trailing slash). |

---

## Verifying the Connection

After OAuth completes, the Shielva connector card should show **Healthy / Connected**. You can run a quick smoke test from the UI:

1. Click **Run health check** — should return `HEALTHY`.
2. Click **Run `list_tags`** — should return the tags defined in your Keap account.
3. Click **Run `list_contacts`** with `limit=5` — should return up to 5 contacts.

If any of those fail, consult the troubleshooting matrix above.
