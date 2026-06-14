# Setup Instructions: Google Gmail Connector

## Overview

The Google Gmail connector lets Shielva read, manage, and delete email messages from a Gmail mailbox using the Google Gmail REST API. It authenticates via OAuth2, so end users authorize Shielva to access their Gmail account through a standard Google consent screen — no passwords are shared. Once connected, the connector can ingest emails, move messages to Trash, permanently delete individual or batches of messages, and apply or remove labels. This connector is intended for workspace administrators or end users who want to index or automate actions on Gmail content within Shielva.

---

## Prerequisites

Before starting, make sure you have:

- A **Google account** with access to the Gmail mailbox you want to connect.
- Access to the **Google Cloud Console** (console.cloud.google.com) with permission to create or manage a project.
- A **GCP project** with the Gmail API enabled (see Step 1 below).
- An **OAuth2 client** of type "Web application" created in that project (see Step 2 below).
- The Shielva platform **redirect URI** provided by your Shielva administrator — you will paste it into the GCP Console during setup.

---

## Step-by-Step Configuration

### Step 1: Enable the Gmail API in Google Cloud Console

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in.
2. Select or create a GCP project using the project selector at the top of the page.
3. In the left-hand navigation, click **APIs & Services** → **Library**.
4. Search for **Gmail API** and click the result.
5. Click **Enable**. Wait for the status to change to "Enabled".

---

### Step 2: Create an OAuth2 Client Credential

1. In the left-hand navigation, click **APIs & Services** → **Credentials**.
2. Click **+ Create Credentials** → **OAuth client ID**.
3. If prompted, click **Configure consent screen** first:
   - Choose **Internal** (if this is a Google Workspace org) or **External**.
   - Fill in the required fields (App name, support email, developer contact).
   - Under **Scopes**, add the following scopes (click "Add or remove scopes"):
     - `https://www.googleapis.com/auth/gmail.modify`
     - `https://mail.google.com/`
   - Save and return to the Credentials page.
4. For **Application type**, select **Web application**.
5. Under **Authorized redirect URIs**, click **+ Add URI** and paste the Shielva redirect URI provided by your administrator.
6. Click **Create**.
7. A dialog will show your **Client ID** and **Client Secret** — copy both now (you will need them in Steps 3 and 4 below).

---

### Step 3: OAuth Client ID (`client_id`)

- **Where to find it**: The "Client ID" value shown after creating the OAuth credential in Step 2, or visible in **APIs & Services → Credentials** by clicking the pencil icon next to your OAuth client.
- **Format**: A long string ending in `.apps.googleusercontent.com` — for example: `123456789-abcdefg.apps.googleusercontent.com`
- **Tip**: Copy it exactly — do not include any surrounding quotes or spaces.

**In Shielva**, paste this value into the **OAuth Client ID** field.

---

### Step 4: OAuth Client Secret (`client_secret`)

- **Where to find it**: The "Client Secret" value shown immediately after creating the OAuth credential, or by clicking **Download JSON** on the Credentials page and reading the `client_secret` field.
- **Tip**: If you did not copy it during creation, click the pencil icon on your OAuth client in the Credentials list and then click **Reset Secret** to generate a new one (the old secret will stop working immediately).

**In Shielva**, paste this value into the **OAuth Client Secret** field.

---

### Step 5 (Optional): OAuth Scopes (`scopes`)

- **What it is**: The set of Gmail API permissions Shielva will request during OAuth authorization.
- **Default**: `https://www.googleapis.com/auth/gmail.modify https://mail.google.com/` — covers reading, trashing, labeling, and permanently deleting messages.
- **Format**: Space-separated scope URLs.
- **When to change**: Only if your organization restricts which scopes are allowed, or if you need read-only access (`https://www.googleapis.com/auth/gmail.readonly`). Narrowing the scope will disable trash and delete operations.

**In Shielva**, leave the **OAuth Scopes** field blank to use the defaults, or enter a space-separated list of scope URLs.

---

### Step 6 (Optional): Authorization URL (`auth_url`)

- **What it is**: The Google OAuth2 authorization endpoint that Shielva redirects users to during the consent flow.
- **Default**: `https://accounts.google.com/o/oauth2/v2/auth`
- **When to change**: Only if your organization uses a custom identity provider. Leave blank in almost all cases.

**In Shielva**, leave the **Authorization URL** field blank to use the Google default.

---

### Step 7 (Optional): Token URL (`token_url`)

- **What it is**: The endpoint Shielva calls to exchange the OAuth2 authorization code for tokens.
- **Default**: `https://oauth2.googleapis.com/token`
- **When to change**: Only for non-standard Google setups. Leave blank in almost all cases.

**In Shielva**, leave the **Token URL** field blank to use the Google default.

---

### Step 8 (Optional): Base API URL (`base_url`)

- **What it is**: The root URL for Gmail REST API calls.
- **Default**: `https://gmail.googleapis.com`
- **When to change**: Only if your organization uses a VPC Service Control perimeter or a custom API proxy. Leave blank in almost all cases.

**In Shielva**, leave the **Base API URL** field blank to use the Google default.

---

### Step 9 (Optional): Rate Limit (`rate_limit_per_min`)

- **What it is**: Maximum Gmail API quota units per minute the connector will consume.
- **Default**: Blank (uses the Google-imposed project quota).
- **Format**: A whole number, for example `250`.
- **When to change**: If you want to throttle Shielva's API usage to avoid affecting other applications sharing the same GCP project.

**In Shielva**, enter a number in the **Rate Limit (quota units/min)** field, or leave it blank.

---

### Step 10 (Optional): Pagination Type (`pagination_type`)

- **What it is**: The pagination strategy used when listing messages.
- **Default**: `page_token` (Gmail's `nextPageToken` cursor-based mechanism).
- **When to change**: Do not change this unless instructed by Shielva support.

**In Shielva**, leave the **Pagination Type** field blank to use the default.

---

### Step 11 (Optional): API Version (`api_version`)

- **What it is**: The Gmail REST API version the connector will call.
- **Default**: `v1` (the current stable version).
- **When to change**: Do not change this unless instructed by Shielva support.

**In Shielva**, leave the **API Version** field blank to use `v1`.

---

## Testing the Connection

1. After saving all fields, click **Authorize** (or **Connect**) in the Shielva connector setup page.
2. You will be redirected to a Google sign-in and consent screen.
3. Sign in with the Gmail account you want to connect, then click **Allow** to grant Shielva the requested permissions.
4. You will be redirected back to Shielva. The connector status should change to **Connected**.
5. Click **Run Health Check** (if available) to confirm the connector can reach the Gmail API.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| "client_id is required" on save | The Client ID field was left blank | Paste the Client ID from the GCP Credentials page |
| "client_secret is required" on save | The Client Secret field was left blank | Paste the Client Secret; reset it in GCP if lost |
| Redirect to Google fails with "redirect_uri_mismatch" | The redirect URI in GCP does not match Shielva's URI | In GCP, add the exact Shielva redirect URI to "Authorized redirect URIs" and save |
| Google consent screen shows "Access blocked: app has not been verified" | OAuth consent screen is in test mode | Add the target Gmail account as a test user in **APIs & Services → OAuth consent screen → Test users** |
| Connector status shows "Token Expired" | Refresh token was revoked or the session expired | Re-authorize by clicking **Authorize** again |
| 403 "Insufficient scope" when trashing or deleting emails | Granted scopes do not include `gmail.modify` or `https://mail.google.com/` | Re-authorize after clearing the Scopes field to use the defaults |
| 404 "Message not found" error | The message ID no longer exists in the mailbox | The message was already deleted — no action needed |
| Rate limit errors (429) | Too many API calls per minute | Set **Rate Limit (quota units/min)** to a lower value, or request a quota increase in GCP |
