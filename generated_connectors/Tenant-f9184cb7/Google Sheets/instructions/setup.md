# Google Sheets Connector — Setup Guide

## Prerequisites

- A Google account with access to Google Sheets
- A Google Cloud project (free tier is sufficient)

---

## Step 1: Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Click **Select a project** → **New Project**
3. Name it (e.g. "Shielva Integration") and click **Create**

---

## Step 2: Enable Required APIs

In your Google Cloud project:

1. Navigate to **APIs & Services → Library**
2. Search for and enable **Google Sheets API**
3. Search for and enable **Google Drive API**

Both APIs must be enabled or the connector will receive 403 errors.

---

## Step 3: Create OAuth 2.0 Credentials

1. Navigate to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth client ID**
3. If prompted, configure the OAuth consent screen first:
   - User Type: **External** (or Internal for Workspace orgs)
   - App name, support email, and developer contact are required
   - Add scopes:
     - `https://www.googleapis.com/auth/spreadsheets.readonly`
     - `https://www.googleapis.com/auth/drive.readonly`
4. Back in Create OAuth client ID:
   - Application type: **Web application**
   - Name: "Shielva Connector" (any name)
   - Authorized redirect URIs: Add the URI Shielva will provide during the OAuth flow (e.g. `https://your-shielva-instance/oauth/callback/google_sheets`)
5. Click **Create**
6. Copy the **Client ID** and **Client Secret** shown

---

## Step 4: Install in Shielva

1. Open **Shielva ACP → Integrations → Google Sheets**
2. Click **Connect**
3. Enter:
   - **OAuth Client ID**: the Client ID from step 3
   - **OAuth Client Secret**: the Client Secret from step 3
   - **Redirect URI** (optional): leave blank to use the default, or enter the exact URI you added to Google Cloud Console
4. Click **Install** — status will show **Pending** until the OAuth flow is completed
5. Click **Authorize with Google** to complete the OAuth flow
6. Sign in with the Google account that owns the spreadsheets you want to sync
7. Grant the requested permissions (read-only Sheets and Drive access)
8. Status changes to **Connected**

---

## Step 5: Verify

The health check will call `GET https://www.googleapis.com/oauth2/v2/userinfo` and display the email address of the connected account. A **Connected** status confirms the token is valid.

---

## Required OAuth Scopes

| Scope | Purpose |
|-------|---------|
| `https://www.googleapis.com/auth/spreadsheets.readonly` | Read spreadsheet data and metadata |
| `https://www.googleapis.com/auth/drive.readonly` | List all Google Sheets files accessible to the account |

---

## Sync Behavior

- The `sync()` operation lists all Google Sheets files accessible to the authorized account via the Drive API
- For each spreadsheet, all sheets are fetched and every data row is normalized into a `ConnectorDocument`
- Documents are keyed by stable SHA-256 IDs so re-syncing the same row produces the same ID
- One `ConnectorDocument` is created per spreadsheet (metadata) plus one per data row
- The first row of each sheet is treated as the header row; data starts at row 2

---

## Troubleshooting

### Status shows "Pending" after install

The OAuth flow has not been completed. Click **Authorize with Google** to initiate the OAuth redirect. Ensure the redirect URI registered in Google Cloud Console exactly matches the one Shielva uses.

### 401 Unauthorized

The access token has expired (Google OAuth tokens typically expire after 1 hour). Re-authorize via **ACP → Integrations → Google Sheets → Re-authorize**. If you have a refresh token configured, Shielva will automatically renew.

### 403 Forbidden

The authorized account does not have the required scopes or the APIs are not enabled.

- Check that both **Google Sheets API** and **Google Drive API** are enabled in Google Cloud Console
- Re-authorize to ensure the latest scopes are granted
- If the OAuth consent screen is in "Testing" mode, only test users can authorize — add your account as a test user or publish the consent screen

### Spreadsheet not appearing in sync

The spreadsheet must be shared with (or owned by) the Google account used to authorize the connector. Spreadsheets not accessible to the account will not appear in Drive API results.

### "This app isn't verified" warning during OAuth

Your OAuth consent screen is in testing mode. Either:
- Add the authorizing account as a test user in the OAuth consent screen configuration, or
- Submit the app for Google verification (required for production use with external users)

---

## Security Notes

- The connector requests **read-only** scopes — it cannot modify, delete, or create spreadsheets
- OAuth tokens are stored encrypted in the Shielva vault and never logged
- The `client_secret` is stored encrypted and never transmitted to the browser
