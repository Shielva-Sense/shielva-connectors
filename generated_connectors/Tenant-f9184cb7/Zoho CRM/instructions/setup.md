# Zoho CRM Connector — Setup Guide

## Overview

The Zoho CRM connector authenticates via OAuth 2.0 using a Zoho API Console client app and syncs Leads, Contacts, and Deals into your Shielva knowledge base using the Zoho CRM REST API v6.

---

## Step 1: Create a Client App in Zoho API Console

1. Go to [https://api-console.zoho.com/](https://api-console.zoho.com/) and log in with your Zoho account.
2. Click **Add Client** (or **GET STARTED**).
3. Select **Server-based Applications** as the client type.
4. Fill in the required fields:
   - **Client Name**: `Shielva Connector` (or any label you prefer)
   - **Homepage URL**: your Shielva instance URL (e.g. `https://app.shielva.ai`)
   - **Authorized Redirect URIs**: Add the callback URL (see Step 2)
5. Click **Create**.

---

## Step 2: Set the Redirect URI

In your Zoho client app, add the following **Authorized Redirect URI**:

```
https://app.shielva.ai/oauth/callback/zoho_crm
```

If self-hosting Shielva, replace the domain with your own.

---

## Step 3: Note Your Credentials

After creating the app, you will see:

| Field | Description |
|---|---|
| **Client ID** | Starts with `1000.` — this is your `client_id` |
| **Client Secret** | Hidden by default — click to reveal; this is your `client_secret` |

Keep these safe — you will enter them in Shielva.

---

## Step 4: Choose Your Data Center

Zoho operates regional data centers. Select the one matching your Zoho account:

| Data Center | Suffix | Accounts URL |
|---|---|---|
| US (default) | `com` | https://accounts.zoho.com |
| Europe | `eu` | https://accounts.zoho.eu |
| India | `in` | https://accounts.zoho.in |
| Australia | `com.au` | https://accounts.zoho.com.au |
| Japan | `jp` | https://accounts.zoho.jp |
| China | `com.cn` | https://accounts.zoho.com.cn |

Enter the suffix (e.g. `eu`) in the **Data Center** field when installing the connector. Leave blank for US.

---

## Step 5: Authorize the Connector in Shielva

When installing the connector in Shielva:

1. Enter your **Client ID** and **Client Secret** from Step 3.
2. Optionally enter your **Redirect URI** from Step 2.
3. Optionally enter your **Data Center** suffix from Step 4.
4. Click **Connect** — Shielva will open the Zoho OAuth authorization page.
5. Log in with a CRM admin account and click **Accept**.
6. Shielva will receive the authorization code and exchange it for tokens automatically.

---

## Step 6: Verify the Connection

After authorization, Shielva runs `health_check()` automatically. A green status badge indicates the connector is **healthy** and the Zoho CRM API is reachable.

---

## OAuth Scopes

The connector requests the following scope:

| Scope | Permission |
|---|---|
| `ZohoCRM.modules.ALL` | Full read/write access to all CRM modules (Leads, Contacts, Deals, Accounts, etc.) |

---

## Data Synced

| Module | Key Fields |
|---|---|
| Leads | id, First_Name, Last_Name, Company, Email, Phone, Lead_Status, Lead_Source, Created_Time |
| Contacts | id, First_Name, Last_Name, Account_Name, Email, Phone, Title, Created_Time |
| Deals | id, Deal_Name, Stage, Amount, Closing_Date, Account_Name, Probability, Created_Time |

All records are normalized to a `ConnectorDocument` with a `source_url` pointing to the record in your Zoho CRM portal. A stable `source_id` is derived from `SHA-256(module + ":" + record_id)[:16]`.

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `INVALID_OAUTHTOKEN` (401) | `access_token` expired or revoked | Re-authorize the connector via the Shielva dashboard |
| `ACCESS_DENIED` (403) | Missing CRM module permissions | Ensure the Zoho user has CRM access and the scope `ZohoCRM.modules.ALL` was granted |
| Data center mismatch (network error) | Wrong `data_center` suffix | Verify your Zoho account data center and update the connector config |
| Rate limits (429) | Too many API calls | The connector has built-in exponential backoff; reduce sync frequency if persistent |
| Empty sync results | Zoho CRM module has no records or API user lacks visibility | Confirm records exist in CRM and the API user has appropriate profile permissions |
| `redirect_uri_mismatch` | Redirect URI not in Authorized list | Add the exact URI to your Zoho client app's Authorized Redirect URIs |

---

## Required Zoho CRM Permissions for the API User

The Zoho user account used to authorize the connector must have:

- **CRM profile** with read access to: Leads, Contacts, Deals, Accounts modules
- No IP restriction blocking Zoho API access from Shielva's servers

---

## API Base URLs (by data center)

| Data Center | API Base URL |
|---|---|
| US | `https://www.zohoapis.com/crm/v6/` |
| Europe | `https://www.zohoapis.eu/crm/v6/` |
| India | `https://www.zohoapis.in/crm/v6/` |
| Australia | `https://www.zohoapis.com.au/crm/v6/` |
| Japan | `https://www.zohoapis.jp/crm/v6/` |
| China | `https://www.zohoapis.com.cn/crm/v6/` |
