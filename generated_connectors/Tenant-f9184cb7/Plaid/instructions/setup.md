# Plaid Connector — Setup Guide

## Overview

The Plaid connector syncs bank transactions, account balances, and institution data into Shielva using the [Plaid API](https://plaid.com/docs/). It authenticates via three credentials: a `client_id`, a `secret`, and an `access_token` tied to a specific Plaid Item (a bank connection).

---

## Step 1: Create a Plaid Account and App

1. Sign up or log in at [dashboard.plaid.com](https://dashboard.plaid.com).
2. Go to **Team Settings → Keys**.
3. Copy your **Client ID** — this is shared across environments.
4. Copy your **Secret** for the target environment:
   - **Sandbox** — use the Sandbox secret for testing with synthetic data
   - **Development** — use the Development secret for testing with real banks (limited to 100 Items)
   - **Production** — use the Production secret for live deployments

---

## Step 2: Obtain an Access Token via Plaid Link

The `access_token` is issued per *Item* (a user's bank connection). You must complete the Plaid Link flow to get one.

**Fastest path — Plaid Quickstart (sandbox):**

```bash
git clone https://github.com/plaid/quickstart.git
cd quickstart
# Follow README to start the server, then open http://localhost:3000
# Complete Link flow → "Exchange token" → copy access_token from the response
```

**Your own implementation:**
1. Call `POST /link/token/create` from your server to get a `link_token`.
2. Initialize Plaid Link in your frontend using the `link_token`.
3. When the user completes Link, you receive a `public_token` in your callback.
4. Call `POST /item/public_token/exchange` with the `public_token` to receive the permanent `access_token`.

Access tokens look like:
- Sandbox: `access-sandbox-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`
- Production: `access-production-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`

---

## Step 3: Install the Connector in Shielva

In the Shielva ACP:

1. Navigate to **Integrations → Plaid**.
2. Click **Connect**.
3. Fill in the install fields:

| Field | Value |
|-------|-------|
| **Client ID** | From Plaid Dashboard → Team Settings → Keys |
| **Secret** | Environment-specific secret (sandbox/development/production) |
| **Access Token** | Obtained from Plaid Link flow |
| **Environment** | `sandbox`, `development`, or `production` (default: `production`) |

4. Click **Install**. The connector verifies credentials via `POST /item/get`.

---

## Environments

| Environment | Base URL | Use |
|-------------|----------|-----|
| `sandbox` | `https://sandbox.plaid.com` | Synthetic test data — no real banks |
| `development` | `https://development.plaid.com` | Real banks, up to 100 Items |
| `production` | `https://production.plaid.com` | Live deployments, unlimited Items |

The **sandbox environment** has pre-configured test credentials and simulates bank accounts with realistic transaction data. Use it for all development and testing.

---

## Troubleshooting

### `INVALID_ACCESS_TOKEN`

The access token is malformed, expired, or belongs to a different environment than the secret being used.

**Fix:** Verify the token environment matches your secret (sandbox secret + sandbox token). Re-run the Link flow to get a fresh token if needed.

---

### `INVALID_API_KEYS`

The `client_id` or `secret` is incorrect.

**Fix:** Go to Plaid Dashboard → Team Settings → Keys and copy the credentials for the correct environment. Ensure there are no leading/trailing spaces.

---

### `ITEM_LOGIN_REQUIRED`

The user's bank credentials have changed or the bank requires re-authentication.

**Fix:** Prompt the user to complete Plaid Link's update mode to re-authenticate. This generates a new `access_token` (or updates the existing Item). Update the connector with the new token.

---

### `RATE_LIMIT_EXCEEDED`

You have exceeded Plaid's API rate limits.

**Fix:** The connector retries automatically with exponential backoff. If persistent, reduce sync frequency or contact Plaid to review your rate limits.

---

### Health check returns `DEGRADED`

A transient network issue prevented reaching the Plaid API.

**Fix:** Check your network connectivity. If the Plaid API itself is down, see [Plaid Status](https://status.plaid.com).

---

## Data Synced

| Resource | Plaid Endpoint | Document Type |
|----------|---------------|---------------|
| Bank accounts | `POST /accounts/get` | `plaid_account` |
| Transactions (last 90 days) | `POST /transactions/get` | `plaid_transaction` |
| Real-time balances | `POST /accounts/balance/get` | (via `get_balance()`) |
| Institution details | `POST /institutions/get_by_id` | (via `get_institution()`) |

---

## Security

- All credentials are stored encrypted in the Shielva vault and never logged.
- The `access_token` grants access to one specific user's bank accounts. Treat it with the same care as a password.
- Rotate credentials in the Plaid Dashboard if you suspect exposure.
- Use environment-specific secrets — never use a production secret in development.
