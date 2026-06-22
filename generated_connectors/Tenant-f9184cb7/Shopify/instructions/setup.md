# Shopify Connector — Setup Guide

## What you need

- A Shopify store (any plan)
- Admin access to the store to create a custom app

---

## Step 1 — Create a Custom App in Shopify Admin

1. Log in to your Shopify Admin: `https://{your-store}.myshopify.com/admin`
2. Go to **Settings** (bottom-left gear icon) → **Apps and sales channels**
3. Click **Develop apps** (top-right)
4. If prompted, click **Allow custom app development**
5. Click **Create an app**
6. Give the app a name (e.g. "Shielva Integration") and click **Create app**

---

## Step 2 — Configure Admin API Scopes

1. On your new app's page, click **Configure Admin API scopes**
2. Enable the following read scopes:
   - `read_orders` — required to sync orders
   - `read_products` — required to sync products
   - `read_customers` — required to sync customers
3. Click **Save**

---

## Step 3 — Install the App and Copy the Token

1. Click **Install app** (top-right of the app page)
2. Confirm the installation
3. Go to the **API credentials** tab
4. Under **Admin API access token**, click **Reveal token once**
5. Copy the token — it starts with `shpat_`

> **Important:** Shopify shows this token only once. If you lose it, you must uninstall and reinstall the app to generate a new one.

---

## Step 4 — Connect in Shielva

In the Shielva ACP → Integrations → Shopify:

| Field | Value |
|-------|-------|
| **Shop URL** | `mystore.myshopify.com` (no `https://`, no trailing slash) |
| **Admin API Access Token** | The `shpat_...` token you copied above |

Click **Install**. The connector verifies credentials by calling `GET /admin/api/2024-01/shop.json`.

---

## What gets synced

| Resource | Endpoint | Notes |
|----------|----------|-------|
| Orders | `GET /orders.json?status=any` | All order statuses including archived |
| Products | `GET /products.json` | Active and draft products |
| Customers | `GET /customers.json` | All customer records |

Pagination uses Shopify's cursor-based system via the `Link` header. Each sync page is 100 records.

---

## Troubleshooting

### 401 Unauthorized
**Cause:** Wrong or expired access token.
**Fix:** Reinstall the app in Shopify Admin to generate a new token. Tokens do not expire unless the app is uninstalled.

### 403 Forbidden
**Cause:** The access token exists but lacks the required scope.
**Fix:** In Shopify Admin → Apps → your app → Configure Admin API scopes, add the missing scope (`read_orders`, `read_products`, or `read_customers`), save, and reinstall.

### 429 Too Many Requests
**Cause:** Shopify REST API limit is roughly 2 requests/second per store (leaky-bucket).
**Fix:** The connector retries automatically with a back-off. Large initial syncs may take a few minutes. For very large stores (100K+ orders), consider running sync during off-peak hours.

### Shop URL format errors
**Fix:** Enter the URL without `https://` and without a trailing slash: `mystore.myshopify.com`

### Token starts with `shpua_` instead of `shpat_`
**Cause:** You copied the wrong credential. `shpua_` is a user-access token from OAuth, not a custom-app token.
**Fix:** Use a Custom App (as described above) and copy the Admin API access token (`shpat_...`).
