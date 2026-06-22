# Microsoft Power BI Connector — Setup Guide

This guide walks you through registering an Azure AD application and connecting it to your Power BI workspace so that the Shielva connector can sync dashboards, reports, and datasets on your behalf.

---

## Prerequisites

- An active Microsoft 365 / Azure subscription with Power BI Pro or Premium
- Global Administrator or Application Administrator role in Azure Active Directory (Entra ID)
- A Power BI workspace with at least one dashboard, report, or dataset

---

## Step 1 — Register an Azure AD Application

1. Sign in to the [Azure Portal](https://portal.azure.com).
2. Navigate to **Azure Active Directory** (or **Microsoft Entra ID**) → **App registrations** → **New registration**.
3. Fill in:
   - **Name**: `Shielva Power BI Connector` (or any descriptive name)
   - **Supported account types**: *Accounts in this organizational directory only (Single tenant)*
   - **Redirect URI**: Select **Web** and enter the callback URL provided in the Shielva connector installation form (e.g. `https://your-shielva-instance.example.com/callback`)
4. Click **Register**.
5. On the **Overview** page, copy:
   - **Application (client) ID** → used as `client_id`
   - **Directory (tenant) ID** → used as `tenant_id_azure`

---

## Step 2 — Add API Permissions

1. In your new app registration, go to **API permissions** → **Add a permission**.
2. Select **Power BI Service**.
3. Choose **Delegated permissions** and tick the following:
   - `Report.Read.All`
   - `Dashboard.Read.All`
   - `Dataset.Read.All`
   - `Workspace.Read.All`
4. Click **Add permissions**.
5. Click **Grant admin consent for [your organisation]** and confirm.

> Without admin consent the OAuth flow will prompt every user individually and may be blocked by conditional access policies.

---

## Step 3 — Create a Client Secret

1. In the app registration, go to **Certificates & secrets** → **Client secrets** → **New client secret**.
2. Enter a description (e.g. `shielva-connector`) and choose an expiry period.
3. Click **Add** and **immediately copy the secret Value** (it is shown only once).
   - This value is used as `client_secret` in the connector install form.

---

## Step 4 — Install the Connector in Shielva

Fill in the following fields in the Shielva ACP connector installation form:

| Field | Value |
|-------|-------|
| Application (client) ID | Copied from Azure Portal in Step 1 |
| Client Secret | The secret value from Step 3 |
| Azure Tenant ID | Copied from Azure Portal in Step 1 |

Click **Authorize** to start the OAuth consent flow. Sign in with a Power BI user account and grant the requested permissions. You will be redirected back to Shielva on success.

---

## Entities Synced

| Entity | API endpoint | Fields synced |
|--------|-------------|---------------|
| Dashboards | `GET /v1.0/myorg/dashboards` | Name, embed URL, read-only flag, workspace ID |
| Reports | `GET /v1.0/myorg/reports` | Name, type, dataset ID, embed URL, workspace ID |
| Datasets | `GET /v1.0/myorg/datasets` | Name, configured by, refreshable, storage mode, workspace ID |
| Workspaces | `GET /v1.0/myorg/groups` | Name, type, capacity ID |

---

## API Reference

- [Power BI REST API overview](https://learn.microsoft.com/en-us/rest/api/power-bi/)
- [Register an app in Azure AD](https://learn.microsoft.com/en-us/azure/active-directory/develop/quickstart-register-app)
- [Power BI permissions reference](https://learn.microsoft.com/en-us/power-bi/developer/embedded/embed-service-principal)
- [Microsoft Identity Platform OAuth 2.0](https://learn.microsoft.com/en-us/azure/active-directory/develop/v2-oauth2-auth-code-flow)

---

## Troubleshooting

| Error | Likely cause | Fix |
|-------|-------------|-----|
| `Authentication failed (401)` | Token expired or revoked | Re-authorize the connector |
| `Forbidden (403)` | Missing Power BI API permissions or admin consent | Grant API permissions and admin consent in Azure Portal |
| `Resource not found (404)` | No dashboards/reports in the workspace | Ensure the authenticated user has access to at least one workspace with content |
| `Rate limited (429)` | Too many API calls | The connector retries automatically; reduce sync frequency if the error persists |
| `Token refresh failed` | Client secret expired | Create a new client secret and update the connector configuration |
