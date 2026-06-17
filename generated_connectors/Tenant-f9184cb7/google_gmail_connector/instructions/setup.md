# Setup Instructions: Google Gmail

## Overview

The Google Gmail connector integrates your organization's Gmail account with the Shielva platform. Once connected, Shielva can read, search, and sync email messages into your knowledge base, and can send or draft emails on your behalf. The connector uses Google's OAuth 2.0 Authorization Code flow — your team never shares passwords with Shielva; instead, Google issues a short-lived access token that the connector renews automatically.

This connector requires a Google Cloud project with the Gmail API enabled and an OAuth 2.0 client credential pair (Client ID and Client Secret).

---

## Prerequisites

Before you begin, make sure you have:

- A **Google account** with administrator access to the Gmail inbox you want to connect
- A **Google Cloud project** — create one at [console.cloud.google.com](https://console.cloud.google.com) if you don't have one
- The **Gmail API enabled** in that project (API Library → search "Gmail API" → Enable)
- An **OAuth 2.0 Client ID** configured for a Web Application (see Step 1 below)
- The Shielva **redirect URI** that your platform administrator provides — you will add it to the OAuth client's authorized redirect URIs before connecting

---

## Step-by-Step Configuration

### Step 1: OAuth2 Client ID (`client_id`) — **Required**

1. Open [Google Cloud Console](https://console.cloud.google.com) and select your project.
2. In the left sidebar, go to **APIs & Services → Credentials**.
3. Click **+ Create Credentials** → **OAuth client ID**.
4. Set the **Application type** to **Web application**.
5. Under **Authorized redirect URIs**, click **+ Add URI** and paste the redirect URI provided by Shielva. Click **Save**.
6. After creation, Google shows a dialog with your **Client ID** (format: `xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.apps.googleusercontent.com`). Copy it.
7. Paste this value into the **OAuth2 Client ID** field in Shielva.

> **Tip:** The Client ID is not a secret — it is safe to copy into a text editor. However, the Client Secret (Step 2) must be kept confidential.

---

### Step 2: OAuth2 Client Secret (`client_secret`) — **Required**

1. On the same **Credentials** page in Google Cloud Console, find your OAuth 2.0 client and click its name to open it.
2. Click **Download JSON** or scroll down to find the **Client secret** field. Click the copy icon.
3. Paste this value into the **OAuth2 Client Secret** field in Shielva. This field is stored encrypted.

> **Common mistake:** If you regenerate the client secret in Google Cloud Console, you must also update this field in Shielva — the old secret immediately stops working.

---

### Step 3: OAuth2 Scopes (`scopes`) — **Optional**

- **Default value:** `https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send`
- Leave this field **blank** to use the default, which grants read access, label modification, and the ability to send email.
- Only fill in this field if your organization requires a narrower scope (e.g. `gmail.readonly` only) or an additional scope not covered by the default.
- If you remove `https://www.googleapis.com/auth/gmail.send`, the **Send Email** and **Post Email** actions will fail with a permission error.
- Scopes must be space-separated.

> **Tip:** In Google Cloud Console, under **APIs & Services → OAuth consent screen**, verify that all scopes you list here appear in the **Scopes** section of your app, or Google will reject them.

---

### Step 4: OAuth2 Authorization URL (`auth_url`) — **Optional**

- **Default value:** `https://accounts.google.com/o/oauth2/auth`
- Leave blank unless your organization uses a custom Google Workspace domain that requires a different authorization endpoint. Standard Gmail accounts should always use the default.

---

### Step 5: OAuth2 Token URL (`token_url`) — **Optional**

- **Default value:** `https://oauth2.googleapis.com/token`
- Leave blank for standard Gmail. Only override if directed by your IT administrator.

---

### Step 6: Gmail API Base URL (`base_url`) — **Optional**

- **Default value:** `https://gmail.googleapis.com/gmail/v1`
- Leave blank. Override only if your organization routes Gmail API traffic through an approved proxy.

---

### Step 7: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `250` (requests per minute)
- Google's standard Gmail API quota is 250 requests per minute per user. Leave blank to use this default.
- If your Google Cloud project has been granted a higher quota tier, enter the approved limit here.

---

### Step 8: Pagination Type (`pagination_type`) — **Optional**

- **Default value:** `page_token`
- Leave blank. The Gmail API exclusively uses page-token pagination; this field exists for future compatibility.

---

### Step 9: API Version (`api_version`) — **Optional**

- **Default value:** `v1`
- Leave blank. The Gmail REST API has only one stable version (`v1`).

---

## Completing the OAuth Authorization

After saving your credentials, click **Connect** in the Shielva connector dashboard. You will be redirected to Google's consent screen. Sign in with the Gmail account you want to connect, review the requested permissions, and click **Allow**.

Shielva will receive an authorization code, exchange it for access and refresh tokens, and store them securely. The connector will renew the access token automatically before it expires — you will not need to re-authorize unless you explicitly revoke access in your Google account settings.

---

## Testing the Connection

1. After OAuth completes, the connector status badge should show **Connected** (green).
2. Click **Run Health Check** on the connector card — a successful check confirms the access token is valid and the Gmail API is reachable.
3. Click **Sync Now** to pull your first batch of inbox messages. Check the sync log for the message count and any errors.
4. To test **Send Email**, open **APIs → send_email**, fill in the `to`, `subject`, and `body` fields, and click **Run**. A sent confirmation (`{id, threadId, labelIds: ["SENT"]}`) means the connector has send permission.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | Access token expired and refresh failed | Click **Re-authorize** on the connector card; complete the OAuth flow again |
| `403 Forbidden` on **Send Email** | `gmail.send` scope missing | Re-authorize with the default scopes (Step 3 must include `gmail.send`) |
| `invalid_client` during OAuth | Wrong Client ID or Client Secret | Double-check both values against Google Cloud Console → Credentials |
| `redirect_uri_mismatch` during OAuth | Shielva's redirect URI not registered in Google | Add the redirect URI shown in the error to your OAuth client's **Authorized redirect URIs** (Step 1) |
| `invalid_scope` during OAuth | A scope in Step 3 is not recognized or not enabled | Verify scopes in Google Cloud Console → OAuth consent screen → Scopes |
| Rate limit errors during sync | Quota exceeded | Lower the sync frequency or raise your quota in Google Cloud Console |
| Connector shows **Missing Credentials** | `client_id` or `client_secret` is blank | Fill in both required fields and click **Save** |
