# Google Calendar Connector — Setup Guide

This guide walks you through connecting Shielva to Google Calendar so that calendar events and meeting metadata are synced into your Shielva knowledge base.

---

## Prerequisites

- A Google account with access to Google Cloud Console
- A Google Cloud project (create one at https://console.cloud.google.com if you don't have one)
- The Shielva integration builder open at https://localhost:8055

---

## Step 1 — Enable the Google Calendar API

1. Go to [Google Cloud Console](https://console.cloud.google.com).
2. Select your project from the project picker at the top.
3. In the left sidebar, navigate to **APIs & Services** → **Library**.
4. Search for **Google Calendar API** and click on it.
5. Click **Enable**.

---

## Step 2 — Create OAuth 2.0 Credentials

1. In the left sidebar, go to **APIs & Services** → **Credentials**.
2. Click **+ Create Credentials** → **OAuth 2.0 Client ID**.
3. If prompted, configure the OAuth consent screen first:
   - Choose **External** (for testing) or **Internal** (for G Suite / Workspace).
   - Fill in the required fields: App name, user support email, developer contact email.
   - Add the scope: `https://www.googleapis.com/auth/calendar.readonly`.
   - Save and continue.
4. Back in Credentials, choose **Application type: Web application**.
5. Give it a name (e.g. "Shielva Calendar Connector").
6. Under **Authorized redirect URIs**, add:
   ```
   https://localhost:8000/connectors/oauth/callback
   ```
   This must match the redirect URI you configure in Shielva exactly.
7. Click **Create**.
8. A dialog appears with your **Client ID** and **Client Secret**. Copy both — you will need them in the next step.

---

## Step 3 — Install the Connector in Shielva

1. Open the Shielva integration builder.
2. Navigate to **Connectors** → **Google Calendar** → **Install**.
3. Enter the following values:
   - **OAuth Client ID**: the `client_id` from the Google Cloud Console (ends in `.apps.googleusercontent.com`)
   - **OAuth Client Secret**: the `client_secret` (starts with `GOCSPX-`)
   - **Redirect URI**: `https://localhost:8000/connectors/oauth/callback`
4. Click **Install**.

---

## Step 4 — Complete the OAuth Flow

1. After install, click **Connect with Google**.
2. You will be redirected to Google's OAuth consent screen.
3. Sign in with the Google account whose calendar you want to sync.
4. Review the requested permissions:
   - **See and download your Google Calendar data** (`calendar.readonly`)
5. Click **Allow**.
6. You will be redirected back to Shielva, and the connector status will change to **Connected**.

---

## Step 5 — Sync Events

1. In the connector detail view, click **Sync Now**.
2. The connector will fetch events from the **primary calendar** for the next 30 days.
3. Events are normalized into ConnectorDocuments and ingested into the selected knowledge base.
4. Subsequent syncs can be triggered manually or scheduled.

---

## Required OAuth Scopes

| Scope | Purpose |
|-------|---------|
| `https://www.googleapis.com/auth/calendar.readonly` | Read all calendars and events |

---

## Troubleshooting

### 401 Unauthorized — Token expired
The access token has expired and the refresh token could not renew it. Re-authorize the connector by clicking **Reconnect** in the connector settings.

### 403 Insufficient scope
The Google account granted fewer permissions than required. Re-authorize and make sure to accept all requested scopes on the consent screen. If using a Workspace account, confirm the admin has not restricted Calendar API access.

### redirect_uri_mismatch
The redirect URI configured in Shielva does not match what is registered in Google Cloud Console. They must be byte-for-byte identical. Check for trailing slashes or `http` vs `https` mismatches.

### 429 Rate limit exceeded
The Google Calendar API quota has been exceeded. The connector retries automatically with exponential backoff. If this persists, check your Google Cloud Console quota dashboard and request a quota increase if needed.

### Connector shows OFFLINE after install
The `client_id` or `client_secret` is missing or incorrect. Re-open the connector settings and re-enter both values.

### Events not appearing after sync
Confirm that:
1. The connected Google account has events in the **primary calendar** within the next 30 days.
2. The connector status shows **Connected** (not **Token expired**).
3. The knowledge base selected during sync is the correct one.
