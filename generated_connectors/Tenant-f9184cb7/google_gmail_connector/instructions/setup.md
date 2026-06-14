# Setup Instructions: Google Gmail

## Overview

The Google Gmail connector allows the Shielva platform to ingest email messages from a Gmail inbox. It reads message metadata and body previews from the authenticated user's INBOX using the Gmail REST API v1. After setup, Shielva can run full or incremental syncs to keep its knowledge base up to date with incoming email.

This connector uses the **OAuth2 Authorization Code** flow. You will need to create a Google Cloud project and register an OAuth2 client — no manual token generation is required. Once your credentials are entered, Shielva will redirect you through Google's consent screen automatically.

---

## Prerequisites

Before starting, make sure you have:

- A **Google account** that owns or has access to the Gmail inbox you want to connect.
- A **Google Cloud project** (free tier is sufficient). Create one at [Google Cloud Console](https://console.cloud.google.com/).
- The **Gmail API enabled** on your Google Cloud project.
- Permission to create **OAuth2 credentials** (Client ID and Client Secret) in that project.

---

## Step-by-Step Configuration

### Step 1: Enable the Gmail API

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and select your project.
2. In the left menu, click **APIs & Services** → **Library**.
3. Search for **"Gmail API"** and click on it.
4. Click **Enable**. If it already shows **Manage**, it is already enabled.

---

### Step 2: Create an OAuth2 Client ID (`client_id`)

1. In the left menu, click **APIs & Services** → **Credentials**.
2. Click **+ Create Credentials** at the top, then select **OAuth client ID**.
3. If prompted, complete the **OAuth consent screen** first:
   - Set **User type** to **External** (unless this is an internal organization app).
   - Fill in **App name**, **User support email**, and **Developer contact information**.
   - Under **Scopes**, add `https://www.googleapis.com/auth/gmail.readonly`.
   - Save and continue until the consent screen is published.
4. Back at **Create OAuth client ID**, set **Application type** to **Web application**.
5. Under **Authorized redirect URIs**, add the redirect URI provided by Shielva (shown on the connector setup page).
6. Click **Create**.
7. Copy the **Client ID** value — this is your `client_id`.

   Example format: `123456789012-abcdefghijklmnopqrstuvwxyz123456.apps.googleusercontent.com`

---

### Step 3: Copy the OAuth Client Secret (`client_secret`)

1. On the same **OAuth 2.0 Client IDs** page (from Step 2), click the pencil icon next to your client.
2. Find the **Client secret** field and click **Copy**.
3. Store this value securely — you will not be able to see it again without regenerating it.

   Example format: `GOCSPX-aBcDeFgHiJkLmNoPqRsTuVwXyZ`

---

### Step 4: Enter credentials in Shielva

On the Shielva connector setup page, fill in:

| Field | Where to get it | Required |
|---|---|---|
| **OAuth Client ID** (`client_id`) | GCP Console → APIs & Services → Credentials → your OAuth client | ✅ Required |
| **OAuth Client Secret** (`client_secret`) | GCP Console → APIs & Services → Credentials → your OAuth client | ✅ Required |

The following fields are **optional** and have sensible defaults — leave them blank unless you need to override them:

| Field | Default | When to change |
|---|---|---|
| **OAuth Scopes** (`scopes`) | `https://www.googleapis.com/auth/gmail.readonly` | Only if your use case requires additional Gmail scopes |
| **Authorization URL** (`auth_url`) | `https://accounts.google.com/o/oauth2/v2/auth` | Do not change unless directed by Shielva support |
| **Token URL** (`token_url`) | `https://oauth2.googleapis.com/token` | Do not change unless directed by Shielva support |
| **Base API URL** (`base_url`) | `https://gmail.googleapis.com` | Do not change unless directed by Shielva support |
| **Rate Limit (quota units/min)** (`rate_limit_per_min`) | Google-imposed default | Set a lower number if you are hitting Gmail quota limits |
| **Pagination Type** (`pagination_type`) | `page_token` | Do not change |
| **API Version** (`api_version`) | `v1` | Do not change |

---

### Step 5: Authorize with Google

1. After saving your credentials, click **Authorize** on the Shielva connector page.
2. You will be redirected to a Google sign-in and consent screen.
3. Sign in with the Google account whose Gmail inbox you want to connect.
4. Click **Allow** to grant Shielva read-only access to Gmail.
5. You will be redirected back to Shielva. The connector status should change to **Connected**.

---

## Testing the Connection

After completing authorization:

1. On the connector detail page, click **Health Check** (or **Test Connection**).
2. If the status shows **Healthy — Connected as user@gmail.com**, the connection is working.
3. Click **Sync Now** → **Full Sync** to run an initial sync and verify that emails are ingested.

---

## Troubleshooting

### "Invalid client_id or client_secret"
- Double-check that you copied the credentials from the correct GCP project and the correct OAuth2 client.
- Make sure you did not copy the **Client ID** into the **Client Secret** field by mistake.
- If you regenerated the client secret, enter the new value — the old one is no longer valid.

### "Redirect URI mismatch"
- The redirect URI registered in your GCP OAuth client must exactly match the URI shown on the Shielva connector setup page.
- Go to GCP Console → **APIs & Services** → **Credentials** → your OAuth client → **Authorized redirect URIs** and add the exact URI Shielva displays.

### "Access blocked: this app is not verified"
- Your OAuth consent screen is in **Testing** mode and the authorizing account is not listed as a test user.
- Go to GCP Console → **APIs & Services** → **OAuth consent screen** → **Test users** → add the Gmail account you are authorizing.
- Alternatively, publish the consent screen to allow any Google account.

### "Token expired — re-authorize required"
- The refresh token has been revoked (e.g. the user changed their Google password or revoked app access).
- Go to [myaccount.google.com/permissions](https://myaccount.google.com/permissions), remove the Shielva app, then re-authorize from the Shielva connector page.

### "Quota exceeded (HTTP 429)"
- Your GCP project has hit the Gmail API quota limit.
- Go to GCP Console → **APIs & Services** → **Quotas** → search **Gmail API** and request a quota increase.
- You can also reduce the sync frequency in Shielva or set a lower `rate_limit_per_min` value.
