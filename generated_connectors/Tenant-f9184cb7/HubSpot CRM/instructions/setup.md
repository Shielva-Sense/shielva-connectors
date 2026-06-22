# HubSpot CRM Connector — Setup Guide

## Overview

This connector integrates Shielva with **HubSpot CRM** via OAuth 2.0, syncing contacts,
companies, deals, and tickets into the Shielva knowledge base.

---

## Prerequisites

- A **HubSpot account** (free or paid)
- A **HubSpot Developer Account** — create one at [developers.hubspot.com](https://developers.hubspot.com)
- Access to the HubSpot portal you want to sync

---

## Step 1 — Create a HubSpot Developer Account

1. Go to [developers.hubspot.com](https://developers.hubspot.com) and click **Get started**.
2. Sign in with your HubSpot credentials or create a new developer account.
3. Once inside the developer portal, click **Create app**.

---

## Step 2 — Create a HubSpot App

1. In the developer portal, click **Apps** in the left navigation.
2. Click **Create app** (top right).
3. Fill in:
   - **App name**: `Shielva CRM Connector` (or any name you prefer)
   - **Description**: (optional)
4. Click **Create app**.

---

## Step 3 — Configure OAuth 2.0

1. Inside your app, navigate to the **Auth** tab.
2. Under **Redirect URLs**, add your redirect URI. For local testing use:
   ```
   https://localhost/callback
   ```
   For production, use your Shielva callback URL provided by your admin.
3. Under **Scopes**, click **Add new scope** and add each of the following:
   - `crm.objects.contacts.read`
   - `crm.objects.companies.read`
   - `crm.objects.deals.read`
   - `tickets`
   - `offline_access`
4. Click **Save** after adding all scopes.

---

## Step 4 — Collect Your Credentials

After saving, HubSpot shows your app credentials on the **Auth** tab:

| Field | Where to find it |
|---|---|
| **Client ID** | Listed under **App credentials** as "Client ID" |
| **Client Secret** | Listed under **App credentials** as "Client secret" (click **Show** to reveal) |
| **Redirect URI** | The URL you added in Step 3 |
| **Portal ID** | Your HubSpot account number — visible in the URL when logged in: `app.hubspot.com/contacts/XXXXXXX` |

---

## Step 5 — Find Your Portal ID

1. Log in to [app.hubspot.com](https://app.hubspot.com).
2. Look at the URL in your browser: `https://app.hubspot.com/contacts/XXXXXXX/`.
3. The number after `/contacts/` is your **Portal ID**.

---

## Step 6 — Install the Connector in Shielva

1. Navigate to **Connectors** in the Shielva admin panel.
2. Click **Add Connector** → select **HubSpot**.
3. Fill in the install form:
   - **Client ID** — from Step 4
   - **Client Secret** — from Step 4
   - **Redirect URI** — must match what you added in HubSpot (Step 3)
   - **Portal ID** — from Step 5 (optional, but recommended)
4. Click **Install**.
5. Shielva will redirect you to HubSpot to authorize. Log in and click **Grant access**.
6. After authorization, Shielva receives an access token and begins syncing.

---

## Step 7 — Verify the Connection

After installation:
1. Go to **Connectors** → **HubSpot** → **Health Check**.
2. A green "Healthy" status confirms your access token is valid.
3. Click **Sync Now** to trigger the first sync (contacts, companies, deals, tickets).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `client_id is required` error on install | client_id field is empty | Copy the Client ID from HubSpot App → Auth tab |
| `client_secret is required` error on install | client_secret field is empty | Copy the Client Secret from HubSpot App → Auth tab |
| `HubSpot token expired or invalid` on health check | Access token expired | Re-authorize the connector in Shielva — click **Re-authenticate** |
| `HubSpot rate limit exceeded` during sync | Too many API calls in a short time | Wait a few minutes and retry; the connector has automatic exponential backoff |
| `403 Forbidden` on contacts/deals/tickets | Missing OAuth scopes | Re-add missing scopes in HubSpot App → Auth → Scopes, then re-authorize |
| `404 Not Found` on a record | Record was deleted in HubSpot | Safe to ignore — the connector skips deleted records |
| Contacts syncing but deals not appearing | `crm.objects.deals.read` scope missing | Add the scope in HubSpot app settings and re-authorize |
| Tickets not syncing | `tickets` scope missing | Add `tickets` scope in HubSpot app and re-authorize |
| Redirect URI mismatch error | URI in Shielva doesn't match HubSpot | Ensure the Redirect URI in Shielva exactly matches what's configured in HubSpot → Auth → Redirect URLs |

---

## Required OAuth Scopes

| Scope | Purpose |
|---|---|
| `crm.objects.contacts.read` | Read contact records |
| `crm.objects.companies.read` | Read company records |
| `crm.objects.deals.read` | Read deal records |
| `tickets` | Read support ticket records |
| `offline_access` | Allows token refresh without user re-login |

---

## Security Notes

- Client Secret is stored encrypted in Shielva's vault.
- Access tokens are refreshed automatically using `offline_access` + `refresh_token`.
- No write permissions are requested — this connector is read-only.
- All API calls are scoped to the authenticated portal (Portal ID).

---

## Support

For help, contact **support@shielva.com** or visit the [Shielva documentation](https://docs.shielva.com/connectors/hubspot).
