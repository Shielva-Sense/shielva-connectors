# Outlook Calendar Connector — Setup Guide

## Prerequisites

You need a Microsoft Azure account and an app registration in the **Azure Portal** to obtain OAuth 2.0 credentials.

---

## Step 1 — Register an App in Azure Portal

1. Go to [https://portal.azure.com](https://portal.azure.com) and sign in.
2. Navigate to **Azure Active Directory → App registrations → New registration**.
3. Fill in:
   - **Name**: e.g. `Shielva Outlook Calendar`
   - **Supported account types**: Select **Accounts in any organizational directory and personal Microsoft accounts** (for broadest access) or restrict to your tenant.
   - **Redirect URI**: Leave blank for now (you'll add it in Step 3).
4. Click **Register**.

---

## Step 2 — Note Your Credentials

After registration, on the **Overview** page:

| Field | Where to find it |
|-------|-----------------|
| **Application (client) ID** | Overview page → "Application (client) ID" |
| **Directory (tenant) ID** | Overview page → "Directory (tenant) ID" (use this as `tenant_hint` for single-tenant apps) |

---

## Step 3 — Create a Client Secret

1. In your app registration, go to **Certificates & secrets → Client secrets → New client secret**.
2. Set a description and expiry (e.g. 24 months).
3. Click **Add** and **immediately copy the secret Value** — it won't be shown again.

This is your **Client Secret**.

---

## Step 4 — Add Redirect URI

1. Go to **Authentication → Add a platform → Web**.
2. Enter your Shielva redirect URI (e.g. `https://your-shielva-instance/connectors/oauth/callback`).
3. Click **Configure** then **Save**.

---

## Step 5 — Grant API Permissions

1. Go to **API permissions → Add a permission → Microsoft Graph → Delegated permissions**.
2. Search and add:
   - `Calendars.Read`
   - `offline_access` (required for refresh tokens)
3. Click **Add permissions**.
4. For tenant-wide consent (optional): click **Grant admin consent for [your org]**.

---

## Step 6 — Install the Connector in Shielva

In the Shielva ACP Install form, provide:

| Field | Value |
|-------|-------|
| **Application (client) ID** | From Azure Portal Overview → Application (client) ID |
| **Client Secret** | The secret Value you copied in Step 3 |
| **Azure AD Tenant (optional)** | Your Directory (tenant) ID for single-tenant apps; leave blank for multi-tenant / personal accounts |
| **Redirect URI (optional)** | Must match exactly what you registered in Step 4 |

---

## Step 7 — Complete OAuth Authorization

After installing, click **Authorize** in Shielva to complete the OAuth flow. You'll be redirected to Microsoft to grant `Calendars.Read` permission, then back to Shielva.

---

## Scopes Requested

| Scope | Purpose |
|-------|---------|
| `https://graph.microsoft.com/Calendars.Read` | Read calendar events |
| `offline_access` | Obtain refresh tokens for background sync |

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| 401 Unauthorized | Access token expired | Re-authorize the connector |
| 403 Forbidden | Missing Calendars.Read permission | Add the scope in Azure Portal → API permissions |
| `invalid_client` on token exchange | Wrong client_id or client_secret | Verify credentials in Azure Portal |
| `redirect_uri_mismatch` | Redirect URI mismatch | Ensure Redirect URI in ACP exactly matches Azure Portal registration |
| 429 Too Many Requests | Graph API rate limit hit | Connector retries automatically with back-off |

---

## Microsoft Graph API Reference

- Calendar API: https://learn.microsoft.com/en-us/graph/api/resources/calendar
- Events: https://learn.microsoft.com/en-us/graph/api/user-list-calendarview
- OAuth flow: https://learn.microsoft.com/en-us/azure/active-directory/develop/v2-oauth2-auth-code-flow
