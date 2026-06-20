# Microsoft Teams Connector — Setup Guide

This guide walks you through registering an Azure AD application and obtaining the OAuth2 credentials required by the Shielva Microsoft Teams connector.

---

## 1. Register an Azure AD Application

1. Go to the [Azure Portal](https://portal.azure.com) and sign in with your Microsoft account.
2. Navigate to **Azure Active Directory** → **App registrations**.
3. Click **New registration**.
4. Enter an **Application name** (e.g., "Shielva Teams Connector").
5. Under **Supported account types**, select:
   - **Accounts in any organizational directory (Any Azure AD directory - Multitenant)** — for multi-tenant use.
   - Or **Accounts in this organizational directory only** — for single-tenant use.
6. Under **Redirect URI**, select **Web** and enter your redirect URI (e.g., `https://your-domain.com/oauth/callback`).
7. Click **Register**.

After registration, note the following values from the **Overview** page:
- **Application (client) ID** — this is your `client_id`
- **Directory (tenant) ID** — this is your `tenant_hint` (optional)

---

## 2. Create a Client Secret

1. In your app registration, navigate to **Certificates & secrets** in the left sidebar.
2. Click **New client secret**.
3. Enter a **Description** (e.g., "Shielva Connector Secret") and choose an **Expiry** period.
4. Click **Add**.
5. **Copy the secret Value immediately** — it will not be shown again.

This value is your `client_secret`.

---

## 3. Configure API Permissions

1. In your app registration, navigate to **API permissions** in the left sidebar.
2. Click **Add a permission** → **Microsoft Graph** → **Delegated permissions**.
3. Add the following permissions:

| Permission | Description |
|------------|-------------|
| `Team.ReadBasic.All` | Read basic information about teams |
| `Channel.ReadBasic.All` | Read basic information about channels |
| `ChannelMessage.Read.All` | Read all channel messages |
| `offline_access` | Maintain access via refresh token |

4. Click **Add permissions**.
5. Click **Grant admin consent for [Your Organization]** to grant consent for all users.

---

## 4. Configure the Connector in Shielva

Enter the following values when prompted:

| Field | Value | Required |
|-------|-------|----------|
| **Client ID** | Application (client) ID from Step 1 | Yes |
| **Client Secret** | Secret value from Step 2 | Yes |
| **Azure Tenant ID** | Directory (tenant) ID from Step 1 | No (defaults to "common") |
| **Redirect URI** | The redirect URI you registered in Step 1 | No |

---

## 5. Complete OAuth2 Authorization

After entering your credentials:

1. Click **Authorize** — you will be redirected to Microsoft's sign-in page.
2. Sign in with a Microsoft account that has access to the Teams you want to sync.
3. Review and accept the requested permissions.
4. You will be redirected back to Shielva with an authorization code.
5. Shielva will exchange the code for an access token automatically.

---

## 6. Verify the Connection

Use the **Health Check** function to verify the connector is properly connected. It calls `GET /me` on the Microsoft Graph API and returns the connected user's display name.

---

## Notes

- **Token Refresh**: The connector uses `offline_access` scope to obtain a refresh token. Access tokens expire in ~1 hour; the connector refreshes them automatically.
- **Permissions**: Only channels and messages the authorized user has access to will be synced.
- **Rate Limits**: Microsoft Graph API enforces rate limits (typically 10,000 requests per 10 minutes per application). The connector retries on 429 responses with exponential backoff.
- **ChannelMessage.Read.All**: This permission requires admin consent in most Microsoft 365 tenants.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `AADSTS700016: Application not found` | Verify the Client ID matches the registered Azure AD application |
| `AADSTS70011: Invalid scope` | Ensure all four permissions are added and admin consent granted |
| `401 Unauthorized` | Access token has expired — trigger re-authorization |
| `403 Forbidden` | The user does not have permission to access the requested team or channel |
| `429 Too Many Requests` | Rate limit exceeded — the connector will retry automatically |
