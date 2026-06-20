# Magento 2 Connector — Setup Guide

## What you need

- A Magento 2 store (any edition: Open Source, Adobe Commerce, or Cloud)
- Admin access to the store
- Your store's base URL (e.g. `https://mystore.com`)

---

## Step 1 — Create an Integration in Magento Admin

1. Log in to your Magento Admin panel: `https://{your-store}/admin`
2. Go to **System** → **Extensions** → **Integrations**
3. Click **Add New Integration**
4. Give the integration a name, e.g. "Shielva Integration"
5. Set your **Current User Identity Verification** password (your admin password)

---

## Step 2 — Grant API Resource Access

1. On the integration form, click the **API** tab in the left sidebar
2. Under **Resource Access**, select **Custom**
3. Enable the following resources:
   - **Sales** → **Operations** → **Orders** (read)
   - **Catalog** → **Products** (read)
   - **Customers** → **Customer Groups** and **All Customers** (read)
   - **Stores** → **Settings** → **All Stores** (read)
4. Click **Save**

> **Tip:** You can also select **All** under Resource Access for simplicity, but the minimal set above is more secure.

---

## Step 3 — Activate the Integration

1. After saving, find your integration in the Integrations list
2. Click **Activate** in the Action column
3. In the confirmation dialog, click **Allow**
4. On the **Integration Tokens** screen, you will see:
   - **Access Token** — copy this value
   - (Also shown: Consumer Key, Consumer Secret, Access Token Secret — not needed)
5. Store the Access Token securely — it is shown only once

---

## Step 4 — Find Your Store Base URL

Your base URL is the root address of your Magento store — the same one in your browser's address bar when you visit the store front. Examples:
- `https://mystore.com`
- `https://store.example.com`
- `https://mystore.com/magento` (if installed in a subdirectory)

Do **not** include a trailing slash.

---

## Step 5 — Connect in Shielva

In the Shielva ACP → Integrations → Magento 2:

| Field | Value |
|-------|-------|
| **Store URL** | Your Magento 2 base URL (e.g. `https://mystore.com`) |
| **Integration Access Token** | The Access Token you copied in Step 3 |

Click **Install**. The connector verifies credentials by calling `GET /rest/V1/store/storeConfigs`.

---

## What gets synced

| Resource | Endpoint | Notes |
|----------|----------|-------|
| Orders | `GET /rest/V1/orders` | All orders, sorted by created_at DESC |
| Products | `GET /rest/V1/products` | All products in the catalog |
| Customers | `GET /rest/V1/customers/search` | All customer accounts |
| Categories | `GET /rest/V1/categories` | Full category tree (for reference) |

Pagination uses Magento's `searchCriteria` offset model. Each sync page is 100 records. Syncing stops when `page * page_size >= total_count`.

---

## Troubleshooting

### 401 Unauthorized
**Cause:** The access token is invalid or was not copied correctly.
**Fix:** Return to Magento Admin → System → Integrations → Deactivate and Reactivate your integration. Copy the new Access Token.

### 403 Forbidden
**Cause:** The integration lacks resource access for the requested endpoint.
**Fix:** Edit the integration in Magento Admin, go to the API tab, and ensure the required resources (Orders, Products, Customers, Stores) are enabled. Save and Activate again.

### Slow responses on large catalogs
**Cause:** Large catalogs (10,000+ products or orders) take time to page through.
**Fix:** This is expected. The connector uses page size 100 and stops when the total is exhausted. For very large stores, run the first full sync during off-peak hours. Subsequent incremental syncs (using `since`) will be much faster.

### Store URL format errors
**Fix:** Include the full `https://` prefix and no trailing slash: `https://mystore.com`

### "resource not found" on /rest/V1/...
**Cause:** Magento REST API may be disabled or your store uses a custom base URL for the API.
**Fix:** In Magento Admin → Stores → Configuration → Services → Magento Web API → Web API Security, ensure "Allow Anonymous Guest Access" or verify the API base URL matches your store URL.

### Test without a live store

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/magento_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
python -m pytest tests/ -v
# Expected: 97 passed
```

All HTTP calls are mocked — no live Magento store required for the test suite.
