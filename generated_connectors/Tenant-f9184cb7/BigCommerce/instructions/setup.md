# BigCommerce Connector — Setup Guide

## What you need

- A BigCommerce store (any plan)
- Store Owner or Admin access to create an API account

---

## Step 1 — Find your Store Hash

Your store hash identifies your store in the BigCommerce API. It appears in two places:

- **Store URL:** `https://store-{store_hash}.mybigcommerce.com` → the `store_hash` is the segment after `store-`
- **Control Panel URL:** When logged in to the Control Panel, the URL contains `/stores/{store_hash}/`

Example: If your store URL is `https://store-abc123def.mybigcommerce.com`, your store hash is `abc123def`.

---

## Step 2 — Create an API Account

1. Log in to your BigCommerce Control Panel
2. Go to **Advanced Settings** → **API Accounts**
3. Click **Create API Account** → choose **Create V2/V3 API Token**
4. Give the account a name (e.g. "Shielva Integration")
5. Set the following permissions:
   - **Products:** Read-only
   - **Orders:** Read-only
   - **Customers:** Read-only
6. Click **Save**
7. A dialog will display your credentials — copy the **Access Token** immediately

> **Important:** BigCommerce shows the access token only once at creation time. Store it securely — you cannot retrieve it later. If lost, you must delete and recreate the API account.

---

## Step 3 — Connect in Shielva

In the Shielva ACP → Integrations → BigCommerce:

| Field | Value | Example |
|-------|-------|---------|
| **Store Hash** | The hash portion of your store URL | `abc123def` |
| **API Access Token** | The token generated in Step 2 | `abc123xyz...` |

Click **Install**. The connector verifies credentials by calling `GET /v2/store`.

---

## What gets synced

| Resource | API Version | Endpoint | Notes |
|----------|-------------|----------|-------|
| Products | v3 | `GET /v3/catalog/products` | Active and inactive products; paginated via `meta.pagination` |
| Orders | v2 | `GET /v2/orders` | All order statuses; paginated by page length |
| Customers | v3 | `GET /v3/customers` | All customer records; paginated via `meta.pagination` |

Sync page size is 250 records per request (BigCommerce maximum).

---

## API version notes

BigCommerce uses two API versions simultaneously:

- **v3** (used for products and customers): Returns `{ "data": [...], "meta": { "pagination": { "total_pages": N } } }`
- **v2** (used for orders): Returns a JSON array directly; pagination is detected when the returned array has fewer records than the requested limit

The connector handles both patterns transparently.

---

## Troubleshooting

### 401 Unauthorized
**Cause:** Invalid or deleted access token.
**Fix:** In BigCommerce Control Panel → Advanced Settings → API Accounts, delete the account and create a new one. Copy the new token immediately.

### 403 Forbidden
**Cause:** The API account was created with insufficient permissions.
**Fix:** In Advanced Settings → API Accounts, edit the account and ensure Products, Orders, and Customers are all set to "Read-only" or higher.

### Store hash not found
**Cause:** Wrong store hash — check by logging in to the Control Panel and reading the hash from the URL bar (`/stores/{store_hash}/`).

### 429 Too Many Requests
**Cause:** BigCommerce rate limit exceeded. The API allows approximately 150 requests per 30-second window (varies by plan).
**Fix:** The connector retries automatically using the `X-Rate-Limit-Time-Reset-Ms` response header. Large syncs may take several minutes on stores with many records.

### `documents_failed > 0` after sync
**Cause:** Individual records failed normalization (e.g. unexpected null values).
**Fix:** Check the connector logs. Failed records increment `documents_failed` but do not stop the sync — the sync completes as `partial` status.
