# Setup Instructions: Google Gmail

## Overview

The Google Gmail connector allows the Shielva platform to ingest email messages from a Gmail account, keep the knowledge base in sync as messages are added or deleted, and perform message management operations such as reading, labeling, moving, and deleting messages and threads. It is intended for administrators who want to make a Gmail inbox searchable and manageable through the Shielva platform. The connector uses OAuth 2.0, so users authorize access by clicking "Connect" in the Shielva UI — no manual token copying is required.

---

## Prerequisites

Before configuring the connector, make sure you have:

- A Google account with access to the Gmail inbox you want to connect.
- Access to the **Google Cloud Console** (console.cloud.google.com) for the Google Cloud project that will own the OAuth credentials.
- Permission to create OAuth 2.0 credentials in that project (typically the Project Owner or Editor role).
- The Shielva connector install page open in a separate browser tab.

---

## Step-by-Step Configuration

### Step 1: Create a Google Cloud Project (if you don't already have one)

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Click the project drop-down at the top of the page → **New Project**.
3. Enter a name (e.g. "Shielva Gmail Connector") and click **Create**.
4. Wait for the project to be created, then select it from the drop-down.

### Step 2: Enable the Gmail API

1. In the left sidebar, go to **APIs & Services → Library**.
2. Search for **Gmail API**.
3. Click **Gmail API** → **Enable**.

### Step 3: Configure the OAuth Consent Screen

1. In the left sidebar, go to **APIs & Services → OAuth consent screen**.
2. Select **External** (or **Internal** if this is a Google Workspace organization) → **Create**.
3. Fill in the required fields:
   - **App name**: e.g. "Shielva Gmail Connector"
   - **User support email**: your email address
   - **Developer contact information**: your email address
4. Click **Save and Continue** through the remaining screens. You can add test users if the app is in External mode.

### Step 4: Create OAuth 2.0 Credentials — get `client_id` and `client_secret`

1. In the left sidebar, go to **APIs & Services → Credentials**.
2. Click **+ Create Credentials → OAuth client ID**.
3. Under **Application type**, select **Web application**.
4. Under **Authorized redirect URIs**, click **+ Add URI** and paste the Redirect URI shown in the Shielva connector install page.
5. Click **Create**.
6. A dialog appears showing your **Client ID** and **Client Secret**. Copy both values — you will paste them into the Shielva install form.

**Step 4a: OAuth Client ID** (`client_id`)
- Copy the long string ending in `.apps.googleusercontent.com`.
- Paste it into the **OAuth Client ID** field in Shielva.

**Step 4b: OAuth Client Secret** (`client_secret`)
- Copy the short alphanumeric string (starts with `GOCSPX-` or similar).
- Paste it into the **OAuth Client Secret** field in Shielva.

> These two fields are **required**. The connector will not be able to start the OAuth flow without them.

### Step 5: (Optional) OAuth Scopes — `scopes`

Leave this field blank to use the default scope (`https://www.googleapis.com/auth/gmail.modify`), which covers reading, labeling, trashing, and modifying messages.

If you need additional capabilities, enter a space-separated list of scopes:
- Add `https://mail.google.com/` to enable permanent deletion of messages and threads.

Example value: `https://www.googleapis.com/auth/gmail.modify https://mail.google.com/`

### Step 6: (Optional) Authorization URL — `authorization_url`

Leave this field blank to use the standard Google authorization endpoint (`https://accounts.google.com/o/oauth2/v2/auth`).

Only set this if your organization proxies or overrides the OAuth authorization endpoint.

### Step 7: (Optional) Token URL — `token_url`

Leave this field blank to use the standard Google token endpoint (`https://oauth2.googleapis.com/token`).

Only set this if your organization routes OAuth token exchange through a custom endpoint.

### Step 8: (Optional) Base API URL — `base_url`

Leave this field blank to use the standard Gmail API base URL (`https://gmail.googleapis.com/gmail/v1`).

Only set this if you are routing Gmail API calls through a proxy or self-hosted endpoint.

### Step 9: (Optional) Allow Permanent Delete — `allow_permanent_delete`

- **Default**: disabled (false).
- When **enabled**, the connector can permanently delete messages and threads. This action is **unrecoverable**.
- To enable permanent delete, you must also add `https://mail.google.com/` to the **Scopes** field (Step 5).

### Step 10: (Optional) Rate Limit — `rate_limit_per_min`

- **Default**: 250 requests per minute (the Gmail API default quota).
- Enter a lower number if your Google Cloud project has a reduced quota or you want to throttle API usage.

### Step 11: (Optional) Pagination Type — `pagination_type`

- **Default**: `cursor` (Gmail pageToken-based pagination).
- Leave blank unless instructed otherwise by Shielva support.

### Step 12: (Optional) API Version — `api_version`

- **Default**: `v1`.
- Leave blank unless instructed otherwise by Shielva support.

### Step 13: Authorize the Connection

1. After filling in all required fields (and any optional ones), click **Install Connector** in Shielva.
2. Shielva will redirect you to Google's sign-in page.
3. Sign in with the Gmail account you want to connect.
4. Review the permission request and click **Allow**.
5. You will be redirected back to Shielva. The connector status should change to **Connected**.

---

## Testing the Connection

After authorization:

1. On the connector detail page in Shielva, click **Health Check** (or wait for the automatic health check to run).
2. A green **Connected** status with the authorized email address confirms the connector is working.
3. Trigger a manual sync by clicking **Sync Now** to verify messages are being ingested into the knowledge base.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| "client_id and client_secret are required install fields" | `client_id` or `client_secret` field is blank | Re-open the install form and paste both values from the Google Cloud Console Credentials page |
| "Token exchange failed: redirect_uri_mismatch" | The redirect URI in Google Cloud does not match the one Shielva provided | Copy the exact Redirect URI from the Shielva install page and add it under **Authorized redirect URIs** in the Google Cloud OAuth client |
| "Token exchange failed: invalid_client" | Wrong `client_id` or `client_secret` | Delete and re-create the OAuth client in Google Cloud Console, then update the Shielva fields |
| "Insufficient scopes" (health check shows DEGRADED) | The authorized user did not grant the required Gmail permissions | Re-authorize by clicking **Reconnect** and accepting all requested permissions |
| "Permanent delete is disabled" error | `allow_permanent_delete` is not enabled | Enable the **Allow Permanent Delete** field and ensure `https://mail.google.com/` is in the **Scopes** field |
| Rate limit errors during sync | API quota exceeded | Lower the **Rate Limit** field value or request a Gmail API quota increase in Google Cloud Console under **APIs & Services → Quotas** |
| Connector shows OFFLINE after working | Access token expired and refresh failed | Click **Reconnect** to re-authorize. If the problem persists, verify the `client_id` and `client_secret` are still valid in Google Cloud Console |
