# Setup Instructions: Google Gmail

## Overview

The Gmail connector integrates your Google Gmail account with the Shielva platform. Once connected, Shielva can read, search, and sync your emails, send messages on your behalf, and perform email management operations (such as moving messages to Trash). The connector uses Google's OAuth2 Authorization Code flow — you grant access by clicking "Authorize" and logging in with your Google account in a popup window. No tokens need to be copied or pasted manually.

---

## Prerequisites

Before you begin, ensure you have:

- A **Google account** with Gmail enabled.
- Access to **Google Cloud Console** (https://console.cloud.google.com) to create an OAuth2 application.
- The **Owner** or **Editor** role on the Google Cloud project (or sufficient permission to create credentials).
- The **Gmail API enabled** in your Google Cloud project.

---

## Step-by-Step Configuration

### Step 1: Enable the Gmail API

1. Go to [Google Cloud Console](https://console.cloud.google.com) and select (or create) a project.
2. In the left-hand menu, click **APIs & Services → Library**.
3. Search for **"Gmail API"** and click on it.
4. Click the **Enable** button. Wait for the API to activate.

---

### Step 2: Create OAuth2 Credentials

1. In the left-hand menu, click **APIs & Services → Credentials**.
2. Click **+ Create Credentials** at the top and select **OAuth client ID**.
3. If prompted, click **Configure Consent Screen** first:
   - Choose **Internal** (for G Suite/Workspace) or **External** (for personal accounts).
   - Fill in the required fields (App name, support email, developer contact email).
   - Under **Scopes**, click **Add or Remove Scopes** and add:
     - `https://www.googleapis.com/auth/gmail.modify`
     - `https://www.googleapis.com/auth/gmail.send`
   - Save and continue until the consent screen is published.
4. Return to **APIs & Services → Credentials → + Create Credentials → OAuth client ID**.
5. Set **Application type** to **Web application**.
6. Under **Authorized redirect URIs**, add the redirect URI provided by Shielva (shown in the connector setup form).
7. Click **Create**.
8. A dialog appears showing your **Client ID** and **Client Secret**. Copy both values.

---

### Step 3: Client ID (`client_id`) — Required

**Label:** Google OAuth2 Client ID

- **Where to find it:** In the dialog shown after creating the OAuth client (Step 2), or in **APIs & Services → Credentials** — click the pencil icon next to your OAuth client to see the Client ID.
- **Format:** A long alphanumeric string ending in `.apps.googleusercontent.com`, e.g. `1234567890-abcdefghijk.apps.googleusercontent.com`.
- **Paste this value** into the **Google OAuth2 Client ID** field in the Shielva connector setup form.

---

### Step 4: Client Secret (`client_secret`) — Required

**Label:** Google OAuth2 Client Secret

- **Where to find it:** In the same dialog as the Client ID (Step 2 above), or by clicking **Download JSON** on the credential and reading the `client_secret` field.
- **Format:** A short alphanumeric string, e.g. `GOCSPX-abc123XYZ`.
- **Paste this value** into the **Google OAuth2 Client Secret** field. This field is masked for security.

> **Tip:** Treat the Client Secret like a password — do not share it or commit it to source control.

---

### Step 5: OAuth2 Scopes (`scopes`) — Optional

**Label:** OAuth2 Scopes

- **Default:** If left blank, the connector uses `https://www.googleapis.com/auth/gmail.modify` and `https://www.googleapis.com/auth/gmail.send`.
- **When to set:** Only override this if your organization restricts specific scopes or you need a narrower permission set (e.g. `https://www.googleapis.com/auth/gmail.readonly` for read-only access).
- **Format:** Space-separated scope URLs, e.g. `https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send`.

---

### Step 6: Authorization URL (`auth_url`) — Optional

**Label:** Authorization URL

- **Default:** `https://accounts.google.com/o/oauth2/v2/auth` (Google's standard OAuth2 authorization endpoint).
- **When to set:** Only change this if you are using a non-standard Google identity provider or are testing against a staging environment.
- Leave blank for standard Gmail accounts.

---

### Step 7: Token URL (`token_url`) — Optional

**Label:** Token URL

- **Default:** `https://oauth2.googleapis.com/token` (Google's standard token endpoint).
- **When to set:** Only override for non-standard environments. Leave blank for standard Gmail accounts.

---

### Step 8: Gmail API Base URL (`base_url`) — Optional

**Label:** Gmail API Base URL

- **Default:** `https://gmail.googleapis.com/gmail/v1`.
- **When to set:** Leave blank unless instructed otherwise by Shielva support or your organization uses a custom API gateway.

---

### Step 9: Rate Limit (`rate_limit_per_min`) — Optional

**Label:** Rate Limit (requests/min)

- **Default:** Governed by Google's Gmail API quota (typically 250 quota units per user per second).
- **When to set:** Set a lower value (e.g. `60`) if your organization has a restricted Gmail API quota or you want to reduce API usage.
- Leave blank to use Google's default rate limits.

---

### Step 10: Pagination Type (`pagination_type`) — Optional

**Label:** Pagination Type

- **Default:** `cursor` (uses Gmail's `nextPageToken` for cursor-based pagination).
- **When to set:** Leave blank or enter `cursor`. This setting is for advanced configurations only.

---

### Step 11: API Version (`api_version`) — Optional

**Label:** API Version

- **Default:** `v1` (Gmail REST API v1).
- **When to set:** Leave blank. Only change if a new Gmail API version is released and Shielva instructs you to update.

---

### Step 12: Authorize the Connector

1. After filling in the required fields (**Client ID** and **Client Secret**), click **Save** in the Shielva connector setup form.
2. Click the **Authorize** (or **Connect**) button.
3. A Google login popup will appear. Sign in with the Gmail account you want to connect.
4. Review the requested permissions and click **Allow**.
5. You will be redirected back to Shielva. The connector status should change to **Connected**.

> **Note:** The redirect URI is handled automatically by Shielva — you do not need to enter it manually in the connector form.

---

## Testing the Connection

After completing the steps above:

1. In Shielva, navigate to your connector and click **Check Health** (or **Test Connection**).
2. The status should show **Healthy — Gmail API reachable**.
3. You can also trigger a manual **Sync** to verify emails are being fetched and indexed correctly.

---

## Troubleshooting

| Error | Likely Cause | Fix |
|---|---|---|
| `OAuth token lacks required Gmail API permissions` | The Gmail API scopes were not granted during authorization | Re-authorize and ensure you click **Allow** for all requested permissions on the Google consent screen |
| `client_id and client_secret are required` | The Client ID or Secret field was left blank | Re-open the connector setup form and enter both values |
| `Token refresh failed` | The refresh token expired or was revoked | Click **Authorize** again to re-authorize the connector |
| `Invalid messageId: {id}` | A message was deleted from Gmail before sync completed | This is normal during incremental sync — the connector will skip missing messages |
| `Too many requests` | Gmail API rate limit exceeded | Set a lower value for **Rate Limit (requests/min)** or wait for the rate limit window to reset |
| `redirect_uri_mismatch` on Google consent screen | The redirect URI in Google Cloud Console does not match Shielva's URI | In Google Cloud Console, go to **APIs & Services → Credentials**, edit your OAuth client, and add the exact redirect URI shown in the Shielva connector form |
| Gmail API not enabled | `accessNotConfigured` error | Go to **APIs & Services → Library**, search for "Gmail API", and click **Enable** |
