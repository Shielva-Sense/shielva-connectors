# Google Analytics 4 Connector — Setup Guide

This guide walks you through connecting Shielva to Google Analytics 4 (GA4) using the official GA4 Data API and Admin API.

---

## Prerequisites

- A Google account with access to Google Analytics 4
- A GA4 property (Universal Analytics properties are not supported)
- Access to Google Cloud Console for creating OAuth 2.0 credentials

---

## Step 1 — Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** → **New Project**
3. Enter a project name (e.g., `Shielva Analytics`) and click **Create**

---

## Step 2 — Enable Required APIs

Your Cloud project must have both GA4 APIs enabled.

1. In Cloud Console, navigate to **APIs & Services** → **Library**
2. Search for and enable **Google Analytics Data API** (`analyticsdata.googleapis.com`)
3. Search for and enable **Google Analytics Admin API** (`analyticsadmin.googleapis.com`)

---

## Step 3 — Create OAuth 2.0 Credentials

1. Navigate to **APIs & Services** → **Credentials**
2. Click **+ Create Credentials** → **OAuth 2.0 Client IDs**
3. If prompted, configure the **OAuth consent screen** first:
   - Set **User Type** to **External** (or **Internal** for G Suite accounts)
   - Fill in the required App name, support email, and developer contact email
   - On the **Scopes** step, add the scope: `https://www.googleapis.com/auth/analytics.readonly`
   - Save and continue through the wizard
4. Back on **Create OAuth client ID**:
   - **Application type**: Select **Web application**
   - **Name**: e.g., `Shielva GA4 Connector`
   - **Authorized redirect URIs**: Add your Shielva OAuth callback URL (provided during connector setup)
   - Click **Create**
5. Google will display your **Client ID** and **Client Secret** — copy both values

---

## Step 4 — Find Your GA4 Property ID

1. Open [Google Analytics](https://analytics.google.com/)
2. Select your GA4 property from the property picker (top left)
3. Navigate to **Admin** (gear icon, bottom left)
4. Under the **Property** column, click **Property Settings**
5. Your **Property ID** is the numeric ID shown at the top (e.g., `123456789`)

> Note: The Property ID is a pure number — not a tracking ID like `G-XXXXXXXX`.

---

## Step 5 — Install the Connector in Shielva

1. In Shielva ARC, go to **Connectors** → **Add Connector** → **Google Analytics**
2. Enter the following fields:
   - **OAuth Client ID**: The Client ID from Step 3
   - **OAuth Client Secret**: The Client Secret from Step 3
   - **Redirect URI** (optional): Your OAuth callback URL if not auto-populated
3. Click **Install** to save the credentials

---

## Step 6 — Authorize the Connector (OAuth Flow)

1. After install, click **Authorize** on the connector page
2. You will be redirected to Google's OAuth consent screen
3. Sign in with the Google account that has access to your GA4 property
4. Grant the `analytics.readonly` permission
5. You will be redirected back to Shielva with the connector connected

---

## Step 7 — Set Your GA4 Property ID

After OAuth authorization:

1. In the connector settings, enter your **Property ID** (from Step 4) in the `property_id` field
2. Save the configuration

---

## Step 8 — Run a Health Check

Click **Health Check** in the connector settings. The connector will call the GA4 Admin API and confirm the number of accounts accessible under your token.

---

## Required OAuth Scopes

| Scope | Purpose |
|-------|---------|
| `https://www.googleapis.com/auth/analytics.readonly` | Read-only access to GA4 data, properties, and reports |

---

## What the Connector Syncs

| Data | Source | Description |
|------|--------|-------------|
| Analytics report rows | GA4 Data API `runReport` | Sessions, active users, pageviews, bounce rate for last 30 days |
| Dimensions & metrics | GA4 Data API `metadata` | Available dimensions and metrics for a property |
| Properties | GA4 Admin API `properties` | GA4 property metadata |
| Accounts | GA4 Admin API `accounts` | GA4 account list |

---

## Troubleshooting

**"Authentication failed" on health check**
- The access token may have expired. Re-authorize the connector via the **Authorize** button.
- Verify the OAuth Client ID and Secret are correct in the connector settings.

**"403 Forbidden" when running reports**
- Ensure the Google account used for OAuth has access to the GA4 property.
- Confirm the `analytics.readonly` scope was granted during the OAuth flow.
- Check that the GA4 Data API and Admin API are enabled in your Cloud project.

**"Resource not found" for property**
- Verify the Property ID is the numeric ID (not `G-XXXXXXXX`).
- Confirm the GA4 property exists and is accessible to the authorized account.

**"429 Rate Limited"**
- The GA4 API has quota limits. The connector retries automatically with exponential backoff.
- For large properties, consider reducing sync frequency.

---

## Security Notes

- The connector uses read-only scope (`analytics.readonly`) — it cannot modify any GA4 data.
- OAuth credentials are encrypted at rest using Shielva Vault (AES-256-GCM).
- Tokens are stored in-memory only during API calls and never written to logs.
- Refresh tokens allow the connector to obtain new access tokens without re-authorization.

---

## API Reference

- [GA4 Data API Reference](https://developers.google.com/analytics/devguides/reporting/data/v1)
- [GA4 Admin API Reference](https://developers.google.com/analytics/devguides/config/admin/v1)
- [OAuth 2.0 for Web Server Applications](https://developers.google.com/identity/protocols/oauth2/web-server)
- [GA4 Dimensions & Metrics Explorer](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema)
