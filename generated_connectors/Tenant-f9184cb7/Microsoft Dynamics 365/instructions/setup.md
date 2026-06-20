# Microsoft Dynamics 365 Connector — Setup Guide

This guide walks you through registering an Azure AD application and connecting it to your Dynamics 365 organisation so that the Shielva connector can sync CRM data on your behalf.

---

## Prerequisites

- An active Microsoft 365 / Azure subscription with a Dynamics 365 environment (Sales, Customer Service, or any Dataverse-backed org)
- Global Administrator or Application Administrator role in Azure Active Directory (Entra ID)
- System Administrator role in the Dynamics 365 organisation

---

## Step 1 — Register an Azure AD Application

1. Sign in to the [Azure Portal](https://portal.azure.com).
2. Navigate to **Azure Active Directory** (or **Microsoft Entra ID**) → **App registrations** → **New registration**.
3. Fill in:
   - **Name**: `Shielva Dynamics 365 Connector` (or any descriptive name)
   - **Supported account types**: *Accounts in this organizational directory only (Single tenant)*
   - **Redirect URI**: Select **Web** and enter the callback URL provided in the Shielva connector installation form (e.g. `https://your-shielva-instance.example.com/callback`)
4. Click **Register**.
5. On the **Overview** page, copy:
   - **Application (client) ID** → used as `client_id`
   - **Directory (tenant) ID** → used as `tenant_id`

---

## Step 2 — Add API Permissions

1. In your new app registration, go to **API permissions** → **Add a permission**.
2. Select **Dynamics CRM**.
3. Choose **Delegated permissions** → tick **user_impersonation**.
4. Click **Add permissions**.
5. Click **Grant admin consent for [your organisation]** and confirm.

> The `user_impersonation` permission allows the connector to act as the signed-in user when calling the Dataverse Web API. Without admin consent the OAuth flow will prompt every user individually.

---

## Step 3 — Create a Client Secret

1. In the app registration, go to **Certificates & secrets** → **Client secrets** → **New client secret**.
2. Enter a description (e.g. `shielva-connector`) and choose an expiry period.
3. Click **Add** and **immediately copy the secret Value** (it is shown only once).
   - This value is used as `client_secret` in the connector install form.

---

## Step 4 — Find Your Dynamics 365 Instance URL

1. Go to the [Power Platform Admin Center](https://admin.powerplatform.microsoft.com).
2. Select **Environments** → click your environment → **Environment URL**.
3. The URL will look like: `https://yourorg.crm.dynamics.com` (or `.crm4.dynamics.com` for Europe, `.crm3.dynamics.com` for Canada, etc.)
4. Use this value as `instance_url` in the connector install form.

> Do not include a trailing slash. The connector appends `/api/data/v9.2/` to this base URL.

---

## Step 5 — Assign Application User in Dynamics 365 (required for service accounts)

If you plan to use a service account (non-interactive authentication) rather than a delegated user:

1. In Dynamics 365, go to **Settings** → **Security** → **Users**.
2. Switch the view to **Application Users**.
3. Click **New** → fill in the **Application ID** (the `client_id` from Step 1).
4. Assign a **Security Role** (e.g. System Administrator or a custom role with Read access to contacts, accounts, leads, opportunities).

For delegated (user consent) OAuth flows used by this connector, the signed-in user's Dynamics 365 security role is applied automatically — no additional step is needed.

---

## Step 6 — Install the Connector in Shielva

Fill in the following fields in the Shielva ACP connector installation form:

| Field | Value |
|-------|-------|
| Application (client) ID | Copied from Azure Portal in Step 1 |
| Client Secret | The secret value from Step 3 |
| Azure AD Tenant ID | Copied from Azure Portal in Step 1 |
| Dynamics 365 Instance URL | Copied from Power Platform Admin Center in Step 4 |
| Redirect URI | Pre-filled by Shielva; must match the value entered in Step 1 |

Click **Authorize** to start the OAuth consent flow. Sign in with a Dynamics 365 user account and grant the requested permissions. You will be redirected back to Shielva on success.

---

## Entities Synced

| Entity | Dataverse table | Fields synced |
|--------|-----------------|---------------|
| Contacts | `contacts` | Name, email, phone, job title, account |
| Accounts | `accounts` | Name, email, phone, website, industry, revenue |
| Leads | `leads` | Name, company, email, phone, source, status |
| Opportunities | `opportunities` | Name, value, close date, probability, stage, account |
| Activities | `activitypointers` | Subject, type, created date |

---

## API Reference

- [Dataverse Web API overview](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/overview)
- [Register an app in Azure AD](https://learn.microsoft.com/en-us/azure/active-directory/develop/quickstart-register-app)
- [Dynamics CRM API permissions](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/authenticate-oauth)
- [Find your environment URL](https://learn.microsoft.com/en-us/power-platform/admin/new-environment#create-an-environment-in-the-power-platform-admin-center)

---

## Troubleshooting

| Error | Likely cause | Fix |
|-------|-------------|-----|
| `Authentication failed (401)` | Token expired or revoked | Re-authorize the connector |
| `Forbidden (403)` | Missing `user_impersonation` permission or admin consent | Grant API permissions and admin consent in Azure Portal |
| `Entity not found (404)` | Wrong instance URL | Check that the URL matches the environment in Power Platform Admin Center |
| `Rate limited (429)` | Too many API calls | The connector retries automatically; reduce sync frequency if the error persists |
| `Token refresh failed` | Client secret expired | Create a new client secret and update the connector configuration |
