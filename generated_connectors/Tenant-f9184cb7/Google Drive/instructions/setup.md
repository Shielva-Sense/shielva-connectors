# Setup Instructions: Google Drive

## Overview

The Google Drive connector integrates your organization's Google Drive with the Shielva platform. Once connected, Shielva can list, search, and sync files and shared drives into your knowledge base, and can export Google Docs content as plain text or PDF. The connector uses Google's OAuth 2.0 Authorization Code flow — your team never shares passwords with Shielva; instead, Google issues a short-lived access token that the connector renews automatically using a refresh token.

This connector requires a Google Cloud project with the Google Drive API enabled and an OAuth 2.0 client credential pair (Client ID and Client Secret).

---

## Prerequisites

Before you begin, make sure you have:

- A **Google account** with access to the Drive you want to connect
- A **Google Cloud project** — create one at [console.cloud.google.com](https://console.cloud.google.com) if you do not have one
- The **Google Drive API enabled** in that project (API Library → search "Google Drive API" → Enable)
- An **OAuth 2.0 Client ID** configured for a Web Application (see Step 1 below)
- The Shielva **redirect URI** provided by your platform administrator — you will add it to the OAuth client's authorized redirect URIs before connecting

---

## Step-by-Step Configuration

### Step 1: OAuth2 Client ID (`client_id`) — **Required**

1. Open [Google Cloud Console](https://console.cloud.google.com) and select your project.
2. In the left sidebar, go to **APIs & Services → Credentials**.
3. Click **+ Create Credentials** → **OAuth client ID**.
4. Set the **Application type** to **Web application**.
5. Under **Authorized redirect URIs**, click **+ Add URI** and paste the redirect URI provided by Shielva. Click **Save**.
6. After creation, Google displays your **Client ID** in the format `xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.apps.googleusercontent.com`. Copy it.
7. Paste this value into the **OAuth2 Client ID** field in Shielva.

> **Tip:** The Client ID is not a secret — it is safe to copy into a text editor. The Client Secret (Step 2) must be kept confidential.

---

### Step 2: OAuth2 Client Secret (`client_secret`) — **Required**

1. On the same **Credentials** page in Google Cloud Console, find your OAuth 2.0 client and click its name to open it.
2. Scroll down to find the **Client secret** field and click the copy icon, or click **Download JSON**.
3. Paste the client secret into the **OAuth2 Client Secret** field in Shielva. This field is stored encrypted.

> **Common mistake:** If you regenerate the client secret in Google Cloud Console, you must update this field in Shielva — the old secret immediately stops working.

---

### Step 3: Redirect URI (`redirect_uri`) — **Optional**

- **Default:** The platform uses its built-in callback URL (e.g. `https://app.shielva.ai/connectors/callback`).
- Only fill in this field if your Google Cloud OAuth client is registered with a custom redirect URI different from the platform default.
- The value you enter here must **exactly match** one of the Authorized redirect URIs registered in Google Cloud Console — including the protocol (`https://`), hostname, and path.

---

## Completing the OAuth Authorization

After saving your credentials, click **Connect** in the Shielva connector dashboard. You will be redirected to Google's consent screen. Sign in with the Google account whose Drive you want to connect, review the requested permissions (`drive.readonly` and `userinfo.email`), and click **Allow**.

Shielva will receive an authorization code, exchange it for access and refresh tokens, and store them securely. The connector renews the access token automatically before it expires — you will not need to re-authorize unless you explicitly revoke access in your Google account settings or regenerate your client secret.

---

## Requested Scopes

| Scope | Purpose |
|---|---|
| `https://www.googleapis.com/auth/drive.readonly` | Read-only access to all files and shared drives |
| `https://www.googleapis.com/auth/userinfo.email` | Read the authenticated user's email address (used for health check and audit) |

> **Note:** The `drive.readonly` scope grants access to all files the authorized user can see, including files in shared drives. It does not allow creating, modifying, or deleting files.

---

## Testing the Connection

1. After OAuth completes, the connector status badge should show **Connected** (green).
2. Click **Run Health Check** on the connector card — a successful check confirms the access token is valid and returns the authenticated user email.
3. Click **Sync Now** to pull your first batch of Drive files. Check the sync log for the file count and any errors.
4. To fetch a specific file, open **APIs → get_file**, enter a `file_id` (visible in the Drive URL as the long alphanumeric string after `/d/`), and click **Run**.
5. To export a Google Doc as plain text, open **APIs → export_file**, enter the `file_id` and set `mime_type` to `text/plain`, and click **Run**.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | Access token expired and refresh failed | Click **Re-authorize** on the connector card and complete the OAuth flow again |
| `invalid_client` during OAuth | Wrong Client ID or Client Secret | Double-check both values against Google Cloud Console → Credentials |
| `redirect_uri_mismatch` during OAuth | Shielva's redirect URI not registered in Google | Add the exact redirect URI shown in the error to your OAuth client's Authorized redirect URIs |
| `Access Not Configured` | Drive API not enabled in the project | Go to Google Cloud Console → APIs & Services → Library → search "Google Drive API" → Enable |
| `403 Forbidden` on list_files | OAuth consent screen in Testing mode, user not added as test user | In OAuth consent screen, add the Google account as a test user, or publish the app |
| `invalid_grant` during token refresh | Refresh token revoked or expired | Re-authorize the connector |
| Connector shows **Missing Credentials** | `client_id` or `client_secret` is blank | Fill in both required fields and click **Save** |
| Export fails with `403` | The file is not a Google Docs/Sheets/Slides file | Only Google-native document types can be exported; binary files (PDF, DOCX) should be downloaded, not exported |
