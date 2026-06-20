# Gusto Connector — Setup Guide

## Prerequisites

- A Gusto account with Payroll Admin access
- Access to the Gusto Developer Portal to create an OAuth application

---

## Step 1: Create a Gusto Developer Account

1. Go to [Gusto Developer Portal](https://dev.gusto.com)
2. Sign up or log in with your Gusto credentials
3. Navigate to **Applications** and click **Create Application**

---

## Step 2: Configure Your OAuth Application

In the Gusto Developer Portal:

1. Set the **Application Name** (e.g., "Shielva Integration")
2. Set the **Redirect URI** to the Shielva OAuth callback URL:
   - Example: `https://your-shielva-instance/oauth/callback/gusto`
3. Note the **Client ID** and **Client Secret** shown after creation
4. Ensure your application requests the following scopes:
   - `openid`
   - `employees:read`
   - `payrolls:read`
   - `companies:read`

---

## Step 3: Install in Shielva

1. Open **Shielva ACP → Integrations → Gusto**
2. Click **Connect**
3. Enter:
   - **OAuth Client ID**: the Client ID from Step 2
   - **OAuth Client Secret**: the Client Secret from Step 2
   - **Redirect URI** (optional): leave blank to use the default, or enter the exact URI registered in Gusto Developer Portal
4. Click **Install** — status shows **Pending** until the OAuth flow is completed
5. Click **Authorize with Gusto** to complete the OAuth flow
6. Sign in with the Gusto Payroll Admin account
7. Grant the requested permissions
8. Status changes to **Connected**

---

## Step 4: Verify

The health check calls `GET https://api.gusto.com/v1/me` and displays the email address of the connected account. A **Connected** status confirms the token is valid.

---

## Required OAuth Scopes

| Scope | Purpose |
|-------|---------|
| `openid` | OpenID Connect — identity verification |
| `employees:read` | Read employee records for all companies |
| `payrolls:read` | Read payroll run data |
| `companies:read` | List companies accessible to the authorized account |

---

## Sync Behavior

- The `sync()` operation lists all companies accessible to the authorized Payroll Admin account
- For each company, all employees are fetched (paginated at 100 per page)
- For each company, all processed payrolls are fetched
- Each employee and each payroll is normalized into a `ConnectorDocument` with a stable SHA-256-derived ID
- The stable ID for employees is `sha256("employee:" + employee_id)[:16]`
- The stable ID for payrolls is `sha256("payroll:" + payroll_id)[:16]`
- Re-syncing the same record produces the same document ID (safe for deduplication)

---

## Troubleshooting

### Status shows "Pending" after install

The OAuth flow has not been completed. Click **Authorize with Gusto** to initiate the redirect. Ensure the Redirect URI registered in the Gusto Developer Portal exactly matches the one Shielva uses.

### 401 Unauthorized

The access token has expired or been revoked. Re-authorize via **ACP → Integrations → Gusto → Re-authorize**. Gusto OAuth tokens have a limited lifetime — re-authorization is required periodically.

### 403 Forbidden

The authorized account does not have Payroll Admin access, or the application does not have the required scopes. Verify the account is a Payroll Admin on the company and the OAuth application requests `employees:read`, `payrolls:read`, and `companies:read`.

### No companies returned

The authorized Gusto account must have **Payroll Admin** role on at least one company. Basic employee accounts cannot access the payroll admin API.

### Rate Limiting (429)

Gusto enforces API rate limits. The connector retries automatically with exponential backoff (up to 3 attempts, honouring `Retry-After` headers). For high-volume syncs, consider increasing the interval between sync runs.

---

## Security Notes

- The connector requests **read-only** scopes — it cannot create, modify, or delete employees or payrolls
- OAuth tokens are stored encrypted in the Shielva vault and never logged
- `client_secret` is stored encrypted and never transmitted to the browser
- Payroll figures (gross pay, net pay) are stored in the knowledge base for HR intelligence queries only — they are governed by your Shielva data retention policy
