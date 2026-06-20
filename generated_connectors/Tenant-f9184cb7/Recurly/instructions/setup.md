# Recurly Connector — Setup Guide

## Overview

The Shielva Recurly connector syncs your subscription billing data — accounts, subscriptions, invoices, plans, and transactions — from Recurly into the Shielva knowledge base using Recurly's REST API v3.

Authentication uses **HTTP Basic Auth**: your Private API Key is the username, and the password is always an empty string. Recurly encodes this as `Authorization: Basic base64(api_key:)`.

---

## Step 1 — Locate your Private API Key

1. Log in to the [Recurly dashboard](https://app.recurly.com).
2. Click your site name in the top-left to open site settings.
3. Navigate to **Integrations** → **API Credentials**.
4. Under **Private API Keys**, you will see your existing key or a button to generate a new one.
   - Private keys start with `rk_live_` (production) or `rk_test_` (sandbox).
   - You can generate multiple keys and label them (e.g. "Shielva Connector").
5. Copy the full key — it is only shown once when generated. If you lost an existing key, revoke it and generate a new one.

> **Security note**: Treat your Private API Key like a password. It grants full read and write access to your Recurly account. The Shielva connector only uses it for read operations.

---

## Step 2 — Configure the connector

In the Shielva dashboard:

| Field | Value |
|---|---|
| **API Key** (required) | Your Recurly Private API Key (`rk_live_...` or `rk_test_...`) |
| **Subdomain** (optional) | Your Recurly subdomain — the prefix before `.recurly.com` (e.g. `mycompany`). Used for generating direct dashboard links in synced documents. |

---

## Step 3 — API version header

All requests from this connector include:

```
Accept: application/vnd.recurly.v2021-02-25
```

This pins the connector to a stable API version. If Recurly releases a newer version, the connector must be updated to adopt it.

---

## Step 4 — Pagination

Recurly v3 uses **cursor-based pagination** for all list endpoints. Each response includes:

```json
{
  "data": [...],
  "has_more": true,
  "next": "cursor_string_for_next_page"
}
```

The connector iterates pages automatically until `has_more` is `false`. Default page size is **200 records** per request (Recurly's maximum).

---

## Step 5 — Resources synced

| Resource | Recurly endpoint | Notes |
|---|---|---|
| Accounts | `GET /accounts` | Customer accounts with contact, billing, tax info |
| Subscriptions | `GET /subscriptions` | All subscription states (active, expired, cancelled, etc.) |
| Invoices | `GET /invoices` | Charge and credit invoices |
| Plans | `GET /plans` | Subscription plan definitions with pricing |
| Transactions | `GET /transactions` | Payment transactions including refunds and voids |

---

## Rate limits

Recurly enforces rate limits per API key. When a `429 Too Many Requests` response is received, the connector reads the `X-RateLimit-Reset` header and waits that duration before retrying. The connector makes up to **3 attempts** with exponential backoff for transient errors.

---

## Sandbox vs production

- **Sandbox keys** (`rk_test_...`) connect to `https://v3.recurly.com` but scope to your test data.
- **Production keys** (`rk_live_...`) connect to the same base URL but to your live data.

The API base URL is always `https://v3.recurly.com` — there is no separate sandbox host.

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Authentication failed: Unauthorized` | Invalid or revoked API key | Generate a new key in Recurly dashboard |
| `rate_limit` / `429` | Too many requests | Wait and retry; connector handles this automatically |
| `not_found` / `404` | Resource does not exist | Check that your Recurly account has data for the resource |
| `Connection error` | Network issue | Check network connectivity to `v3.recurly.com` |
| `server error 5xx` | Recurly API outage | Check [Recurly status page](https://status.recurly.com) |
