# Setup Instructions: Microsoft Entra ID

## Overview

The Microsoft Entra ID connector integrates your organization's Microsoft Entra ID (formerly Azure AD) tenant with the Shielva platform via the **Microsoft Graph API**. Once connected, Shielva can manage users, groups, application registrations, service principals, directory roles, and read directory audit logs and sign-in logs.

This connector uses the **OAuth 2.0 client-credentials grant** — Shielva authenticates as an Entra ID application (not as a user), so no human sign-in is required after install. Tokens are minted from the tenant-scoped endpoint `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token` and renewed automatically before expiry.

---

## Prerequisites

Before you begin, make sure you have:

- A **Microsoft Entra ID tenant** with **Global Administrator** (or Application Administrator + Privileged Role Administrator) access
- An **App Registration** in that tenant (you will create one in Step 1 if you do not already have one)
- The application **API permissions** to call Microsoft Graph for the resources you want Shielva to manage (User.Read.All, Group.ReadWrite.All, Application.Read.All, Directory.Read.All, AuditLog.Read.All — exact set depends on which actions you will use)
- **Admin consent** granted for those permissions

---

## Step-by-Step Configuration

### Step 1: Tenant ID (`tenant_id`) — **Required**

1. Sign in to [Azure Portal](https://portal.azure.com).
2. In the search bar at the top, type **Microsoft Entra ID** and open it.
3. On the **Overview** page, copy the **Tenant ID** value (a GUID like `11111111-2222-3333-4444-555555555555`).
4. Paste this value into the **Tenant ID** field in Shielva.

> The Tenant ID is part of the OAuth token endpoint — `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token` — so it must be accurate or token requests will fail with `invalid_tenant`.

---

### Step 2: Application (Client) ID (`client_id`) — **Required**

1. In **Microsoft Entra ID → App registrations**, click **+ New registration** (or open an existing app you intend to reuse).
2. Give the app a name (e.g. `Shielva Connector`), choose **Accounts in this organizational directory only**, and click **Register**.
3. On the app's **Overview** page, copy the **Application (client) ID** GUID.
4. Paste this value into the **Application (Client) ID** field in Shielva.

---

### Step 3: Client Secret (`client_secret`) — **Required**

1. On the same App registration, open **Certificates & secrets → Client secrets**.
2. Click **+ New client secret**, give it a description, choose an expiry, and click **Add**.
3. Microsoft will show the **Value** of the new secret exactly once — copy it immediately.
4. Paste the secret **Value** (not the Secret ID) into the **Client Secret** field in Shielva. This field is stored encrypted.

> **Common mistake:** Microsoft displays both a *Value* and a *Secret ID*. The Value is what you need. The Secret ID is metadata and will not authenticate.
>
> **Rotation:** When the secret expires, generate a new one in Azure Portal and update this field — the old secret stops working immediately.

---

### Step 4: API Permissions & Admin Consent — **Required (in Azure)**

1. In the same App registration, open **API permissions**.
2. Click **+ Add a permission → Microsoft Graph → Application permissions**.
3. Select the permissions for the actions you plan to use. Recommended baseline:
   - `User.Read.All` (list/get/create/update/delete users)
   - `Group.ReadWrite.All` (manage groups + membership)
   - `Application.Read.All` (list applications and service principals)
   - `Directory.Read.All` (list directory roles)
   - `AuditLog.Read.All` (list audit + sign-in logs)
4. Click **Add permissions**.
5. Click **Grant admin consent for {tenant}** — this is what makes the application permissions actually usable. Without admin consent, every Graph call will fail with `Authorization_RequestDenied`.

---

### Step 5: OAuth2 Scope (`scopes`) — **Optional**

- **Default value:** `https://graph.microsoft.com/.default`
- Leave blank. The `.default` scope tells Microsoft to mint a token with all the application permissions that have been admin-consented (Step 4) — this is the correct behavior for the client-credentials grant.
- Only override this field if you have a very specific reason (e.g. national cloud endpoints).

---

### Step 6: Microsoft Graph Base URL (`base_url`) — **Optional**

- **Default value:** `https://graph.microsoft.com/v1.0`
- Leave blank for the commercial cloud. Override only when targeting national clouds:
  - US Gov: `https://graph.microsoft.us/v1.0`
  - China (21Vianet): `https://microsoftgraph.chinacloudapi.cn/v1.0`
  - Germany: `https://graph.microsoft.de/v1.0`

---

### Step 7: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `240`
- Microsoft Graph applies per-app and per-tenant throttling. The Graph documentation publishes per-service quotas; 240 requests/minute is a safe shared default. Raise this only if you have confirmed your tenant's quota tier.

---

## Completing Authentication

After saving the four required fields, click **Connect** in the Shielva connector dashboard. The connector will call its `authenticate()` method, which performs the client-credentials grant against `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`. There is **no interactive sign-in** — success means Microsoft accepted your client ID + secret and your app has at least one admin-consented permission.

The access token is cached in process memory and re-minted automatically about a minute before it expires.

---

## Testing the Connection

1. After authentication completes, the connector status badge should show **Connected** (green).
2. Click **Run Health Check** — a successful check confirms the token works and `/organization` is reachable.
3. Open **APIs → list_users** and run with `top=5`. A list of users (or an empty array if your tenant is brand new) confirms `User.Read.All` is consented.
4. Open **APIs → list_groups** and run with `top=5`. Confirms `Group.Read*` is consented.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AADSTS70011: The provided value for the input parameter 'scope' is not valid` | Wrong scope for client-credentials flow | Use the default `https://graph.microsoft.com/.default` |
| `AADSTS7000215: Invalid client secret` | Client secret was rotated or you copied the Secret ID instead of the Value | Generate a fresh client secret (Step 3) and update Shielva |
| `AADSTS90002: Tenant not found` | Tenant ID is wrong | Re-copy the tenant GUID from Microsoft Entra ID → Overview (Step 1) |
| `Authorization_RequestDenied` on Graph calls | Admin consent not granted, or the requested permission is missing | Go to App registrations → API permissions → click **Grant admin consent for {tenant}** (Step 4) |
| `Insufficient privileges to complete the operation` | The application permission you have is not enough for that endpoint (e.g. `User.Read.All` is insufficient for `create_user` which needs `User.ReadWrite.All`) | Add the broader permission, then grant admin consent again |
| `429 Too Many Requests` during bulk operations | Microsoft Graph throttling | The connector retries automatically honoring `Retry-After`. If sustained, lower the request rate or stagger jobs |
| Connector shows **Missing Credentials** | Tenant ID, Client ID, or Client Secret is blank | Fill in all three required fields and click **Save** |
