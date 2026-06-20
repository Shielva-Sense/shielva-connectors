# Square Connector — Setup Guide

## Overview

The Square connector syncs payments, orders, customers, and catalog items from your Square account into the Shielva knowledge base. It uses **OAuth 2.0** for authentication and the Square REST API v2 (`https://connect.squareup.com/v2/`).

---

## Step 1 — Create a Square Application

1. Log in to the [Square Developer Dashboard](https://developer.squareup.com/apps).
2. Click **Create your first application** (or **New Application** if you already have apps).
3. Enter an application name (e.g., "Shielva Connector") and click **Save**.
4. Open the application and go to the **Credentials** tab.
5. Note your **Application ID** (starts with `sq0idp-`) and **Application Secret** (starts with `sq0csp-`). Keep the secret secure — treat it like a password.

---

## Step 2 — Configure OAuth Redirect URI (optional)

If you are running the full OAuth2 flow (recommended for production):

1. In the Square Developer Dashboard, go to **OAuth** tab of your application.
2. Under **Redirect URL**, add the Shielva OAuth callback URL (provided by your Shielva deployment).
3. Click **Save**.

---

## Step 3 — Configure the Connector in Shielva

In the Shielva connector install form, provide:

| Field | Key | Type | Required | Description |
|---|---|---|---|---|
| Square Application ID | `application_id` | text | Yes | From Credentials tab (sq0idp-...) |
| Square Application Secret | `application_secret` | secret | Yes | From Credentials tab (sq0csp-...) |
| OAuth Redirect URI | `redirect_uri` | text | No | Must match URI registered in your Square app |

After saving, call **Authorize** to generate the Square OAuth2 authorization URL. Complete the OAuth flow in your browser — the resulting `access_token` is stored securely by Shielva.

---

## Step 4 — OAuth2 Scopes

The connector requests the following permissions:

| Scope | Purpose |
|---|---|
| `MERCHANT_PROFILE_READ` | Read merchant business information |
| `PAYMENTS_READ` | Read payment records |
| `ORDERS_READ` | Read order records |
| `CUSTOMERS_READ` | Read customer profiles |
| `ITEMS_READ` | Read catalog items |

---

## What the Connector Syncs

| Resource | Endpoint | Notes |
|---|---|---|
| Payments | `GET /v2/payments` | Cursor-paginated; amounts normalized from cents to float |
| Orders | `POST /v2/orders/search` | Requires a `location_id`; used via `list_orders()` |
| Customers | `GET /v2/customers` | Cursor-paginated |
| Catalog items | `GET /v2/catalog/list` | Cursor-paginated; type filter via `types` param |

Sync runs payments then customers. Orders require a `location_id` and are available via the `list_orders()` method directly.

---

## Data Model

Each Square resource is normalized into a `ConnectorDocument`:

- **Payments**: stable ID is `SHA-256("payment:" + payment_id)[:16]`; amount converted from cents (integer) to float (e.g., `2500` → `25.0 USD`).
- **Customers**: source ID is the Square customer ID; full name, email, and phone included.

---

## Troubleshooting

### 401 Unauthorized

- The access token has expired or been revoked. Re-run the OAuth flow to obtain a new token.

### 403 Forbidden — Missing Scope

- Your Square application is missing one or more required OAuth scopes.
- In the Square Developer Dashboard → your app → **OAuth**, verify `PAYMENTS_READ`, `ORDERS_READ`, `CUSTOMERS_READ`, `ITEMS_READ`, and `MERCHANT_PROFILE_READ` are requested.

### 429 Too Many Requests

- Square imposes rate limits per application. The connector retries automatically with exponential backoff (up to 3 attempts, honours `Retry-After` header).

### Connector Shows as DEGRADED

- The circuit breaker opens after 5 consecutive failures.
- Resolve the underlying auth or network issue, then trigger a **Health Check** to reset the circuit breaker.

### `documents_failed > 0` in Sync Result

- One or more records returned unexpected data causing normalization to fail.
- Check connector logs for which record IDs failed; inspect those records in the Square Dashboard.

---

## API Reference

- Base URL: `https://connect.squareup.com/v2`
- API version header: `Square-Version: 2024-01-17`
- Authorization header: `Authorization: Bearer {access_token}`
- Payments: `GET /v2/payments`
- Orders: `POST /v2/orders/search`
- Customers: `GET /v2/customers`
- Catalog: `GET /v2/catalog/list`
- Merchant: `GET /v2/merchants/me`

---

## Support

For additional help, refer to the [Square API documentation](https://developer.squareup.com/docs/payments-api/overview) or contact Shielva support.
