# Workday Connector — Setup Guide

This guide walks you through configuring the Shielva Workday connector to sync workers, organizations, job profiles, and locations from Workday HCM.

---

## Prerequisites

- Workday tenant with admin access (Security Administrator or Integration System User role)
- Ability to create an API Client in Workday (Integration System configuration)
- Your Workday tenant name (the subdomain in `https://<tenant>.workday.com`)

---

## Step 1 — Find Your Workday Tenant Name

Your tenant name is the subdomain portion of your Workday URL:

```
https://mycompany.workday.com/
         ─────────── (tenant = "mycompany")
```

You can also find it at: **Workday → Menu → About Workday**. The tenant name appears in the URL shown there.

---

## Step 2 — Create an Integration System User (ISU)

The Workday OAuth2 Client Credentials flow requires a non-interactive user account.

1. In Workday, search for **Create Integration System User**
2. Set a **User Name** (e.g. `shielva_integration`)
3. Set a strong **Password** — note: this is for the ISU account, not the OAuth secret
4. Check **Do Not Allow UI Sessions** — this locks the account to API-only access
5. Uncheck **Session Timeout Minutes Override** (leave at default)
6. Click **OK**

---

## Step 3 — Create a Security Group for the ISU

1. Search for **Create Security Group**
2. Select **Type: Integration System Security Group (Unconstrained)**
3. Name it `Shielva_HCM_Read` (or similar)
4. Click **OK**

Add the ISU you created:
1. Search for **Edit Integration System Security Group**
2. Find your group, click **Edit**
3. In **Integration System Users**, add the ISU you created in Step 2
4. Click **OK**

---

## Step 4 — Grant Security Group Domain Permissions

1. Search for **Maintain Permissions for Security Group**
2. Select your `Shielva_HCM_Read` group
3. Add the following **Domain Security Policies** with **View** / **Get** access:

   | Domain | Purpose |
   |--------|---------|
   | Worker Data: All Positions | Workers and job assignments |
   | Organization Information | Supervisory and company orgs |
   | Job Profile Data | Job profiles and families |
   | Location Data | Office and work locations |
   | Worker Data: Current Staffing Information | Active/inactive status |
   | Workers: Public Information | Basic worker directory |

4. Click **OK**

5. Search for **Activate Pending Security Policy Changes** and confirm with a comment (e.g. "Add Shielva read permissions").

---

## Step 5 — Create an OAuth 2.0 API Client

1. Search for **Register API Client**
2. Fill in:
   - **Client Name**: `Shielva HCM Connector`
   - **Non-Expiring Refresh Tokens**: leave unchecked (Client Credentials does not use refresh tokens)
   - **Allowed Scopes**: add **System** (required for Client Credentials grant)
   - **Redirection URI**: can be left blank for Client Credentials
3. Under **Grant Types**, check **Client Credentials**
4. Click **OK**

Workday will display:
- **Client ID** — copy this (you will need it in the connector config)
- **Client Secret** — copy this immediately; it is shown only once

---

## Step 6 — Assign the ISU to the API Client

1. Search for **View API Client**
2. Open your `Shielva HCM Connector` client
3. Go to **API Client Authorizations**
4. Click **Authorize New** (or **Edit Authorizations**)
5. Set:
   - **Integration System User**: select the ISU you created in Step 2
   - **Allowed Scope**: System
6. Click **OK**

---

## Step 7 — Find Your Workday Base URL

Your base URL is the root of your Workday tenant. It follows the pattern:

```
https://<tenant>.workday.com
```

Example: `https://mycompany.workday.com`

Do **not** include a trailing slash or any path segment.

---

## Step 8 — Configure the Connector in Shielva

In the Shielva ACP, navigate to **Integrations → Add Connector → Workday** and enter:

| Field | Value |
|-------|-------|
| **Client ID** | The Client ID from Step 5 |
| **Client Secret** | The Client Secret from Step 5 |
| **Workday Tenant Name** | Your tenant subdomain (e.g. `mycompany`) |
| **Workday Base URL** | `https://mycompany.workday.com` |

Click **Install**. The connector validates credentials by obtaining an OAuth2 token and listing workers.

---

## Step 9 — Run Your First Sync

Once installed, trigger a sync from **Integrations → Workday → Sync Now**.

The connector will sync:
- **Workers** — all active and inactive employee and contingent worker records
- **Organizations** — supervisory orgs, company hierarchies
- **Job Profiles** — job families, levels, and pay rate types
- **Locations** — office locations and remote work sites

---

## API Endpoints Used

| Resource | Endpoint |
|----------|----------|
| Token | `POST https://{tenant}.workday.com/ccx/oauth2/{tenant}/token` |
| Workers | `GET {base_url}/ccx/api/v1/{tenant}/workers` |
| Organizations | `GET {base_url}/ccx/api/v1/{tenant}/organizations` |
| Job Profiles | `GET {base_url}/ccx/api/v1/{tenant}/jobProfiles` |
| Locations | `GET {base_url}/ccx/api/v1/{tenant}/locations` |

All endpoints use offset/limit pagination (`?limit=100&offset=N`).

---

## Troubleshooting

### 401 Unauthorized on token request

- Verify the Client ID and Client Secret are copied correctly
- Confirm the API Client has **Client Credentials** grant type enabled
- Confirm the ISU is added under **API Client Authorizations**

### 403 Forbidden on resource endpoints

- The ISU security group does not have the required domain permissions
- Re-run Steps 3–5 and activate the pending security policy changes

### No workers returned

- The ISU may not have access to **Worker Data: Current Staffing Information**
- Check the ISU's security group domain assignments

### Rate Limiting (429)

The connector automatically retries with exponential backoff (up to 3 attempts, 1s → 2s → 4s delays). If rate limiting persists in production, contact Workday support to increase API quotas for the ISU.

---

## Security Notes

- Store Client Secret in Shielva's encrypted vault — never commit to source control
- The ISU account is API-only (`Do Not Allow UI Sessions` must remain checked)
- Rotate the Client Secret periodically: **Register API Client → Client Secret → Rotate**
- Grant only the minimum required domain scopes (read-only via GET/View permissions)
