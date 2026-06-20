# Zendesk Sell Connector — Setup Guide

## Overview

The Zendesk Sell connector syncs contacts, leads, deals, notes, tasks, and pipelines
from Zendesk Sell (formerly Base CRM) into the Shielva knowledge base using the
Zendesk Sell REST API v3 with OAuth 2.0 Authorization Code flow.

---

## Step 1: Register an OAuth Application in Zendesk Sell

1. Log into your Zendesk Sell account at [https://app.futuresimple.com](https://app.futuresimple.com).
2. Click your profile avatar (top-right corner) and select **Settings**.
3. In the left sidebar navigate to **OAuth** → **Developer Apps**.
4. Click **Register Application**.
5. Fill in the application details:
   - **Application Name**: Shielva Integration (or any name you choose)
   - **Description**: Shielva knowledge base connector for Zendesk Sell
   - **Redirect URI**: `https://app.shielva.com/oauth/callback`
     (replace with your actual Shielva tenant redirect URI if different)
6. Click **Save**.
7. You will be shown a **Client ID** and **Client Secret** — copy both values immediately.
   The Client Secret is only shown once.

---

## Step 2: Install the Connector in Shielva

In the Shielva ACP (Admin Control Panel):

1. Navigate to **Connectors** → **Add Connector** → **Zendesk Sell**.
2. Enter the following install fields:
   - **Client ID** — paste the value from Step 1
   - **Client Secret** — paste the value from Step 1
   - **Redirect URI** — `https://app.shielva.com/oauth/callback` (must match Step 1)
3. Click **Authorize** — you will be redirected to the Zendesk Sell OAuth consent page.
4. Click **Allow** to grant Shielva read access to your Zendesk Sell account.
5. You will be redirected back to Shielva, which exchanges the authorization code
   for an access token automatically.
6. The connector status will show **Connected** when successful.

---

## Step 3: Verify the Connection

After authorization:

1. In Shielva ACP → Connectors → Zendesk Sell, click **Health Check**.
2. The status should show **Healthy** with a green indicator.
3. If you see **Invalid Credentials**, your access token may have expired —
   click **Re-authorize** to restart the OAuth flow.

---

## Step 4: Run Your First Sync

1. In Shielva ACP → Connectors → Zendesk Sell, click **Sync Now**.
2. The sync will fetch all contacts, leads, deals, notes, tasks, and pipelines.
3. Progress and document counts are displayed in real time.
4. On completion, the synced records are searchable in the Shielva knowledge base.

---

## OAuth 2.0 Flow Details

| Parameter | Value |
|-----------|-------|
| Authorization URL | `https://api.getbase.com/oauth2/authorize` |
| Token URL | `https://api.getbase.com/oauth2/token` |
| Scopes | `read` |
| Grant Type | Authorization Code |

---

## Synced Resources

| Resource | Zendesk Sell API Endpoint |
|----------|--------------------------|
| Contacts | `GET /v3/contacts` |
| Leads | `GET /v3/leads` |
| Deals | `GET /v3/deals` |
| Notes | `GET /v3/notes` |
| Tasks | `GET /v3/tasks` |
| Pipelines | `GET /v3/pipelines` |

---

## Troubleshooting

**"Authentication failed" after re-authorization:**
Access tokens from Zendesk Sell do not expire by default but may be revoked if your
OAuth application is updated. Re-authorize the connector from the Shielva ACP.

**"Rate limited" during sync:**
Zendesk Sell enforces API rate limits. The connector automatically retries with
exponential backoff. For large accounts (10,000+ records) the initial sync may take
several minutes.

**Contacts not appearing after sync:**
Ensure your OAuth application has the `read` scope approved. Check the Zendesk Sell
Settings → OAuth → Developer Apps page to confirm your application is active.

---

## Support

For help with the Shielva connector, contact support@shielva.com or visit
[https://docs.shielva.com/connectors/zendesk-sell](https://docs.shielva.com/connectors/zendesk-sell).
