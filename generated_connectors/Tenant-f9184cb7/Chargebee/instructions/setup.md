# Chargebee Connector — Setup Guide

## Prerequisites

You need a Chargebee account with API access enabled. Any role (Admin or restricted API-only keys) that has read access to subscriptions, customers, and invoices is sufficient.

---

## Step 1 — Find your Chargebee site name

Your site name is the subdomain you use to access your Chargebee dashboard.

- If your Chargebee URL is `https://acme.chargebee.com`, your site name is `acme`.
- Enter the site name only — do **not** include `.chargebee.com` or `https://`.

---

## Step 2 — Generate an API Key

1. Log in to your Chargebee account.
2. Go to **Settings** (gear icon, top-right) → **Configure Chargebee**.
3. Under the **API Keys** section, click **API Keys & Webhooks**.
4. Click **+ Add API Key**.
5. Give the key a descriptive name (e.g. `Shielva Connector`).
6. Set the key type to **Read-Only** (recommended) — the connector only reads data.
7. Click **Create** and copy the generated key immediately (it is only shown once for full keys; you can regenerate if lost).

---

## Step 3 — Install the connector

In the Shielva integration builder:

1. Navigate to **Integrations → Chargebee**.
2. Click **Connect** or **Install**.
3. Enter your **Site** name (e.g. `acme`).
4. Paste your **API Key**.
5. Click **Save / Install**.

The connector validates your credentials by calling `GET /api/v2/subscriptions?limit=1`. On success, status is set to **Connected**.

---

## How authentication works

Chargebee uses HTTP Basic Auth where the API key is the username and the password is an **empty string**:

```
Authorization: Basic base64(api_key:)
```

This is handled automatically by `aiohttp.BasicAuth(api_key, "")`.

---

## Required permissions

Your API key must have **read** access to:

| Resource | Chargebee permission |
|----------|----------------------|
| Subscriptions | Read Subscriptions |
| Customers | Read Customers |
| Invoices | Read Invoices |

A full-access API key grants all of these automatically. For security, create a dedicated read-only key.

---

## What gets synced

| Resource | API endpoint | Notes |
|----------|-------------|-------|
| Subscriptions | `GET /api/v2/subscriptions` | Plan, status, MRR, billing period |
| Customers | `GET /api/v2/customers` | Name, email, company, phone |
| Invoices | `GET /api/v2/invoices` | Status, total, amount due/paid |

Chargebee uses **offset-based pagination** — the connector follows `next_offset` until it is absent or empty.

---

## Incremental sync

The `sync()` method syncs all three resource types in one pass. Pass `kb_id` to ingest documents into a Shielva knowledge base.

```python
async with ChargebeeConnector(config={"site": "acme", "api_key": "YOUR_KEY"}) as conn:
    result = await conn.sync(full=True, kb_id="kb_billing_001")
    print(f"Synced {result.documents_synced} documents")
```

---

## Troubleshooting

### 401 Unauthorized
- The API key is wrong or has been deactivated.
- Go to Chargebee → Settings → Configure Chargebee → API Keys and verify the key is active.
- Re-enter the key in the connector settings.

### 403 Forbidden
- The API key does not have the required permissions (e.g. read-only key missing a resource scope).
- Create a new key with read access to Subscriptions, Customers, and Invoices.

### 404 Not Found — site name incorrect
- The site name does not match any active Chargebee account.
- Double-check: if your Chargebee URL is `https://mycompany.chargebee.com`, the site name is `mycompany` (no `.chargebee.com`).

### 429 Too Many Requests — rate limit
- Chargebee enforces API rate limits per plan.
- The connector retries automatically with exponential backoff (up to 3 attempts).
- If consistently rate-limited, reduce sync frequency or contact Chargebee to increase your limit.

### Sync returns 0 documents
- Verify the Chargebee site has subscriptions, customers, or invoices.
- A new or test site may be empty — create a test subscription and re-run sync.
