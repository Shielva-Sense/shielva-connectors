# Help Scout Connector — Setup Guide

## Overview

The Help Scout connector uses **OAuth 2.0 Client Credentials** to authenticate with the Help Scout API v2. You need to create an OAuth app in Help Scout to obtain an **App ID** (client_id) and **App Secret** (client_secret).

---

## Step 1 — Create a Help Scout OAuth App

1. Log in to your Help Scout account at [https://secure.helpscout.net](https://secure.helpscout.net).
2. Click your **avatar / name** in the top-right corner.
3. Select **Your Profile** from the dropdown.
4. In the left sidebar, click **My Apps**.
5. Click **Create My App**.
6. Fill in the form:
   - **App Name**: `Shielva Integration` (or any descriptive name)
   - **Redirection URL**: `https://shielva.ai/oauth/callback` (required by the form; not used by client credentials flow)
7. Click **Create**.

---

## Step 2 — Copy your App ID and App Secret

After creating the app, Help Scout displays two values:

| Field | Where to find it |
|-------|-----------------|
| **App ID** | Shown as "App ID" on the app detail page |
| **App Secret** | Shown as "App Secret" — copy it now, it will not be shown again |

> **Important**: The App Secret is shown only once. Copy it immediately and store it securely. If you lose it, you must regenerate it (which invalidates the old secret).

---

## Step 3 — No scopes required

Help Scout's Client Credentials grant automatically grants access to all resources the app owner's account can access. You do **not** need to configure any OAuth scopes manually.

---

## Step 4 — Install in Shielva

1. In the Shielva integration builder, navigate to **Integrations → Help Scout**.
2. Enter:
   - **App ID**: the `App ID` from Step 2
   - **App Secret**: the `App Secret` from Step 2
3. Click **Install**.

The connector will:
1. Exchange your credentials for a bearer token via `POST https://api.helpscout.net/v2/oauth2/token`.
2. Call `GET /users/me` to verify the token and identify the connected user.
3. Display the connected user's name on success.

---

## What gets synced

| Resource | API endpoint | Notes |
|----------|-------------|-------|
| Conversations | `GET /conversations` | All statuses (active, pending, closed, spam); HAL paginated |
| Customers | `GET /customers` | Full customer profiles; HAL paginated |
| Mailboxes | `GET /mailboxes` | All mailboxes the account can access |
| Users | `GET /users` | All team members / agents |
| Tags | Via conversation metadata | Tags embedded in conversation records |

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `OAuth token request failed (401)` | Invalid App ID or App Secret | Re-check values from Help Scout → My Apps |
| `OAuth token request failed (400)` | Malformed request / wrong grant_type | Ensure you are using the App ID and Secret (not OAuth tokens from another flow) |
| `Help Scout API is reachable` but no conversations | Account has no conversations | Check that the OAuth app owner's account has access to the target mailboxes |
| Rate limit errors | Too many API calls | The connector retries automatically with exponential backoff (max 3 attempts) |

---

## Security notes

- App credentials (App ID + Secret) are stored encrypted in the Shielva vault and never logged.
- Tokens are refreshed automatically before expiry (5-second pre-expiry buffer).
- The Client Credentials flow does not involve any user redirect — all authentication happens server-side.
