# Salesforce CRM Connector — Setup Guide

## Overview

The Salesforce CRM connector authenticates via OAuth 2.0 using a Salesforce Connected App and syncs Leads, Contacts, and Opportunities into your Shielva knowledge base.

---

## Step 1: Create a Connected App in Salesforce

1. Log in to your Salesforce org.
2. Go to **Setup** (gear icon, top right) → search for **App Manager** in the Quick Find box.
3. Click **New Connected App** (top right).
4. Fill in the required fields:
   - **Connected App Name**: `Shielva Connector` (or any label you prefer)
   - **API Name**: auto-filled
   - **Contact Email**: your admin email

---

## Step 2: Enable OAuth Settings

1. Under **API (Enable OAuth Settings)**, tick the **Enable OAuth Settings** checkbox.
2. Set the **Callback URL** to:
   ```
   https://app.shielva.ai/oauth/callback/salesforce
   ```
   (If self-hosting, replace the domain with your Shielva instance URL.)

---

## Step 3: Select OAuth Scopes

Add the following scopes to **Selected OAuth Scopes**:

| Scope | Description |
|---|---|
| `api` | Access and manage your data (required for SOQL queries) |
| `refresh_token` | Allow offline access; obtain and use a refresh token |
| `offline_access` | Same as refresh_token — include for completeness |

Move each scope from **Available OAuth Scopes** to **Selected OAuth Scopes** using the **Add** arrow.

---

## Step 4: Save and Retrieve Credentials

1. Click **Save**, then **Continue**.
2. On the Connected App detail page, click **Manage Consumer Details** (you may need to verify your identity).
3. Note the following values:
   - **Consumer Key** → this is your `client_id`
   - **Consumer Secret** → this is your `client_secret`

---

## Step 5: Get Your Instance URL

Your `instance_url` is the base URL of your Salesforce org. Examples:
- Production: `https://yourcompany.salesforce.com`
- Sandbox: `https://yourcompany--sandbox.sandbox.salesforce.com`
- My Domain: `https://yourcompany.my.salesforce.com`

You can find it in **Setup → My Domain → Current My Domain URL**.

---

## Step 6: Authorize the Connector in Shielva

When installing the connector in Shielva:

1. Enter `client_id` (Consumer Key) and `client_secret` (Consumer Secret).
2. Enter `instance_url` (your org URL from Step 5).
3. Click **Connect** — Shielva will open the Salesforce OAuth authorization page.
4. Log in with an admin or API-enabled user and click **Allow**.
5. Shielva will receive the `access_token` and `refresh_token` and store them securely.

---

## Step 7: Verify the Connection

After authorization, Shielva runs `health_check()` automatically. A green status badge indicates the connector is **healthy** and the Salesforce API is reachable.

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `INVALID_SESSION_ID` (401) | `access_token` expired or revoked | Re-authorize the connector via the Shielva dashboard |
| `insufficient_scope` (403) | Connected App is missing required OAuth scopes | Return to App Manager → Connected App → Edit → add `api`, `refresh_token`, `offline_access` |
| Instance URL mismatch | `instance_url` does not match the org that issued the token | Confirm the URL in Setup → My Domain and update the connector config |
| Rate limits (429) | Too many API calls in the polling window | The connector has built-in exponential backoff and respects `Retry-After`; reduce sync frequency or increase `SYNC_PAGE_SIZE` |
| `INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY` | The API user does not have read access to Leads, Contacts, or Opportunities | Assign the API user a Profile or Permission Set with read access to those objects |
| No records returned | Connected App not approved for org-wide access | In Setup → Connected Apps OAuth Usage → click **Install** next to your app |

---

## Required Salesforce Permissions for the API User

The user account used to authorize the connector must have:

- **API Enabled** system permission
- **Read** access on: Lead, Contact, Opportunity, Account sObjects
- **View All Data** or equivalent object-level permissions if you want to sync records owned by other users

---

## Data Synced

| Object | Fields |
|---|---|
| Lead | Id, FirstName, LastName, Company, Email, Phone, Status, LeadSource, CreatedDate |
| Contact | Id, FirstName, LastName, Account.Name, Email, Phone, Title, CreatedDate |
| Opportunity | Id, Name, StageName, Amount, CloseDate, Account.Name, Probability, CreatedDate |

All records are normalized to a `ConnectorDocument` with a `source_url` pointing to the Lightning Experience record page (`/lightning/r/<Object>/<Id>/view`).
