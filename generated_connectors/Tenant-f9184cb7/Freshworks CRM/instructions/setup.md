# Freshworks CRM Connector — Setup Guide

## Prerequisites

You need a Freshworks CRM (Freshsales) account with Agent or Admin access.

---

## Step 1 — Find your Freshworks CRM domain

Your domain is the subdomain of your Freshworks CRM account URL.

- If you access Freshworks CRM at `https://acme.myfreshworks.com`, your domain is **acme**.
- Enter only the subdomain — not the full URL. The connector automatically constructs `https://{domain}.myfreshworks.com/crm/sales/api/v2`.

---

## Step 2 — Get your API Key

1. Log in to your Freshworks CRM account.
2. Click your **avatar / profile icon** in the top-right corner.
3. Select **Profile Settings**.
4. Navigate to the **API Settings** tab.
5. Copy the **API Key** shown there.

The API key is a long alphanumeric string. Keep it secure — it grants full API access to your CRM account.

---

## Step 3 — Install the connector

In the Shielva integration builder:

1. Navigate to **Integrations → Freshworks CRM**.
2. Click **Connect** or **Install**.
3. Enter your **Domain** (e.g. `acme` — without `.myfreshworks.com`).
4. Paste your **API Key**.
5. Click **Save / Install**.

The connector validates credentials by calling `GET /crm/sales/api/v2/selector/owners`. On success, status is set to **Connected** and the number of CRM owners is reported.

---

## Authentication method

Freshworks CRM uses Token-based authentication via the `Authorization` header:

```
Authorization: Token token={api_key}
```

This is different from HTTP Basic Auth — do **not** use `Bearer` or `Basic` prefixes.

---

## Required permissions

Your Freshworks CRM account must have at least **Agent** level access. Agent access allows:

- Reading all contacts in your account
- Reading all deals
- Reading all sales accounts (companies)
- Listing CRM owners (used for auth validation)

Admin access is not required for read-only sync.

---

## What gets synced

| Resource | API endpoint | Key fields |
|----------|-------------|------------|
| Contacts | `POST /contacts/filters` | Name, email, phone, job title, company, LinkedIn |
| Deals | `POST /deals/filters` | Name, amount, stage, probability, expected close, account |
| Accounts | `POST /sales_accounts/filters` | Name, website, phone, industry, employees, revenue |

Freshworks CRM uses a `POST /resource/filters` pattern for listing records. The connector handles this transparently and exposes standard `list_*()` / `get_*()` methods.

Pagination is controlled by `meta.total_pages` in the API response. The connector stops fetching when all pages are consumed or the returned list is empty.

---

## Pagination response format

Freshworks CRM wraps all list responses:

```json
{
  "contacts": [...],
  "meta": {
    "total_pages": 5,
    "current_page": 1,
    "total_count": 487
  }
}
```

The connector reads `meta.total_pages` to stop pagination correctly.

---

## Troubleshooting

### 401 Unauthorized
- The API key is wrong or has been regenerated.
- Go to **Profile Settings → API Settings** and copy the current key.
- Re-enter it in the connector settings.

### 403 Forbidden
- Your account does not have permission to read the requested resource.
- Confirm your role in Freshworks CRM is at least **Agent**.
- Ask a Freshworks CRM Admin to check your permissions.

### 429 Too Many Requests — rate limit
- Freshworks CRM enforces API rate limits depending on your plan.
- The connector retries automatically with exponential backoff (up to 3 attempts).
- If the limit is consistently hit, consider reducing sync frequency.

### Domain not found / connection error
- Verify the domain is entered as just the subdomain (e.g. `acme`, not `acme.myfreshworks.com`).
- Confirm your Freshworks CRM account is active.
- Check that your network allows outbound HTTPS to `*.myfreshworks.com`.

### Sync returns 0 documents
- Your CRM account may be empty or newly created.
- Run with `full=True` to bypass any incremental filters.
- Check that your account contains contacts, deals, or accounts.

### Unit tests fail with `ModuleNotFoundError: aiohttp`
```bash
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
pip install aiohttp pytest pytest-asyncio
cd /Users/vivekvarshavaishvik/Documents/client_dir/freshworks_crm_connector
python -m pytest tests/ -v
```
