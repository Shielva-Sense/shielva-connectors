# Constant Contact Connector — Setup Guide

## Overview

The Constant Contact connector syncs contacts, email campaigns, and contact lists from your Constant Contact account into the Shielva knowledge base using the Constant Contact v3 API and OAuth 2.0 Authorization Code flow.

---

## Prerequisites

- A Constant Contact account with API access
- A registered application in the [Constant Contact Developer Portal](https://developer.constantcontact.com/)
- Your application's **Client ID** and **Client Secret**

---

## Step 1: Create a Constant Contact Application

1. Log in to the [Constant Contact Developer Portal](https://developer.constantcontact.com/).
2. Navigate to **My Applications** and click **New Application**.
3. Enter a name for your application (e.g., "Shielva Integration").
4. Under **Redirect URIs**, add your Shielva redirect URI (e.g., `https://your-shielva-domain.com/oauth/callback`).
5. Note your **API Key** (this is the Client ID) and generate a **Client Secret**.
6. Under **Scopes**, enable:
   - `contact_data` — Read/write access to contacts
   - `campaign_data` — Read access to email campaigns
   - `account_read` — Read access to account information

---

## Step 2: Install the Connector

Provide the following fields in the Shielva connector install form:

| Field | Required | Description |
|-------|----------|-------------|
| `client_id` | Yes | Your Constant Contact API Key (Client ID) |
| `client_secret` | Yes | Your Constant Contact Client Secret |
| `redirect_uri` | No | OAuth callback URL registered in your application |

---

## Step 3: Complete OAuth Authorization

1. After installing, click **Authorize** to initiate the OAuth 2.0 flow.
2. You will be redirected to Constant Contact's authorization page.
3. Log in with your Constant Contact account and grant the requested permissions.
4. You will be redirected back to Shielva with an authorization code.
5. Shielva exchanges the code for an access token and refresh token automatically.

---

## Step 4: Verify the Connection

Run a **Health Check** to confirm connectivity:

- **HEALTHY** — API is reachable and the access token is valid.
- **DEGRADED (TOKEN_EXPIRED)** — Access token has expired; re-authorize the connector.
- **OFFLINE** — No access token present; complete the OAuth flow.

---

## Step 5: Sync Data

Trigger a **Sync** to pull all contacts and email campaigns into the knowledge base.

### What gets synced

| Resource | API Endpoint | Document Type |
|----------|-------------|---------------|
| Contacts | `GET /v3/contacts` | `contact` |
| Email Campaigns | `GET /v3/emails` | `email_campaign` |

### Pagination

The connector uses Constant Contact's **cursor-based pagination** via `_links.next.href`. All pages are fetched automatically during sync.

---

## Authentication Details

| Property | Value |
|----------|-------|
| Auth Type | OAuth 2.0 Authorization Code |
| Authorization URL | `https://authz.constantcontact.com/oauth2/default/v1/authorize` |
| Token URL | `https://authz.constantcontact.com/oauth2/default/v1/token` |
| Scopes | `contact_data campaign_data account_read` |

---

## Troubleshooting

### "No access token — complete OAuth to connect"
The OAuth flow has not been completed. Click **Authorize** in the connector settings and complete the flow.

### "Token expired or invalid"
The access token has expired. Re-authorize the connector by clicking **Authorize** again.

### "Network error"
Check your network connectivity and confirm the Constant Contact API is reachable at `https://api.cc.email`.

### "client_id is required" or "client_secret is required"
Re-enter your API credentials from the Constant Contact Developer Portal.

---

## API Reference

- [Constant Contact v3 API Docs](https://developer.constantcontact.com/api_guide/index.html)
- [OAuth 2.0 Authorization](https://developer.constantcontact.com/api_guide/server_flow.html)
- [Contacts API](https://developer.constantcontact.com/api_guide/contacts_overview.html)
- [Email Campaigns API](https://developer.constantcontact.com/api_guide/emails_overview.html)
