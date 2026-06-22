# WooCommerce Connector — Setup Guide

## Prerequisites

- A live WooCommerce store running WordPress 5.6+ and WooCommerce 3.5+
- Administrator access to the WordPress dashboard
- HTTPS enabled on your store (required for production; HTTP may work for local dev)

## Step 1 — Generate REST API Credentials

1. Log in to your WordPress admin panel (e.g. `https://mystore.com/wp-admin`)
2. Navigate to **WooCommerce → Settings → Advanced → REST API**
3. Click **Add key**
4. Fill in the form:
   - **Description**: `Shielva Connector` (or any label you prefer)
   - **User**: Select a user with at least *Shop Manager* or *Administrator* role
   - **Permissions**: Select **Read** (the connector only reads data)
5. Click **Generate API key**
6. You will see your credentials **only once**:
   - **Consumer Key** — starts with `ck_...`
   - **Consumer Secret** — starts with `cs_...`
7. Copy both values immediately and store them securely

> **Important:** Once you leave this page the Consumer Secret is no longer shown in full. If you lose it, delete the key and generate a new one.

## Step 2 — Find Your Store URL

Your store URL is the root domain of your WooCommerce site, visible in your browser address bar on the homepage. Examples:

- `https://mystore.com`
- `https://shop.mycompany.io`
- `https://mystore.com/shop` (if WooCommerce is installed in a subdirectory)

Do **not** include a trailing slash.

## Step 3 — Install in Shielva

In the Shielva ACP:

1. Navigate to **Integrations → WooCommerce**
2. Click **Connect**
3. Fill in the install fields:
   - **Store URL**: Your store root URL (Step 2)
   - **Consumer Key**: The `ck_...` value from Step 1
   - **Consumer Secret**: The `cs_...` value from Step 1
4. Click **Install**

The connector calls `GET /wp-json/wc/v3/system_status` to validate credentials and store info. On success the status changes to `ONLINE`.

## Step 4 — Run a Sync

Once installed, trigger a sync from the Integrations page. The connector will:

1. Fetch all **orders** (paginated, 100 per page)
2. Fetch all **products** (paginated, 100 per page)
3. Fetch all **customers** (paginated, 100 per page)

For incremental syncs (recommended), pass a `since` timestamp — only records modified after that time will be fetched.

## Troubleshooting

### `401 Unauthorized` — Authentication failed

**Cause:** The Consumer Key or Consumer Secret is wrong or has been revoked.

**Fix:**
- Double-check the key values (they are case-sensitive)
- Go to **WooCommerce → Settings → Advanced → REST API** and verify the key is still listed
- If revoked or lost, delete the old key and generate a new one

### `403 Forbidden` — Key has no read permission

**Cause:** The REST API key was created with *Write* or *None* permission instead of *Read*.

**Fix:** Delete the key and create a new one with **Read** permission.

### `SSL / Certificate errors`

**Cause:** Your store uses a self-signed certificate or HTTP only.

**Fix:**
- For production: install a valid TLS certificate (e.g. Let's Encrypt)
- For local development: the connector disables SSL verification by default (`ssl=False` in aiohttp) — no action needed

### `woocommerce_rest_cannot_view` in error message

**Cause:** The WordPress user attached to the API key does not have permission to view the resource.

**Fix:** Ensure the user has the *Shop Manager* or *Administrator* role in WordPress.

### Sync returns 0 records

**Cause:** The store is empty, or the `since` filter is too recent.

**Fix:**
- Check the store has at least one order, product, and customer
- Run with `full=True` to bypass the incremental filter

### `X-WP-TotalPages` pagination

The connector uses the `X-WP-TotalPages` response header to drive pagination. If your store or a caching plugin strips this header, the connector falls back to stopping when a page returns fewer items than the requested page size.
