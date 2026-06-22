# Customer.io Connector — Setup Guide

This guide walks you through obtaining a Customer.io App API key and connecting it to Shielva.

---

## 1. Obtain Your Customer.io App API Key

1. Log in to your Customer.io account at [customer.io](https://customer.io).
2. Click your workspace name in the top navigation, then go to **Settings**.
3. Navigate to **Account Settings** → **API Credentials**.
4. Under **App API Keys**, click **Create App API Key**.
5. Give the key a name (e.g. "Shielva Connector") and click **Create**.
6. Copy the key immediately — it is shown only once.

> **Important:** Use the **App API key**, not the **Track API key**. The Track API key is for sending events from your product. The App API key is for reading data (customers, campaigns, newsletters).

> **Security note:** Treat your App API key like a password. Do not share it or commit it to version control. Shielva stores it AES-256-GCM encrypted at rest.

---

## 2. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **App API Key** | Your Customer.io App API key |

The connector validates the key by calling `GET /accounts` before completing installation.

---

## 3. What Gets Synced

The connector syncs three resource types on each run:

| Resource | Customer.io API | Description |
|----------|-----------------|-------------|
| Customers | `POST /customers` | All customer profiles with email, name, and attributes |
| Campaigns | `GET /campaigns` | Automated campaigns with name, status, and message type |
| Newsletters | `GET /newsletters` | One-off newsletter sends with name and tags |

Customer sync uses cursor-based pagination (the `next` field in the response) to handle large audiences — there is no record limit.

---

## 4. API Details

- **Base URL:** `https://api.customer.io/v1`
- **Auth Header:** `Authorization: Bearer {app_api_key}`
- **Content-Type:** `application/json`
- **Customer pagination:** Cursor-based via the `next` field in `POST /customers` responses
- **Campaign/Newsletter pagination:** Page-based via `page` and `limit` query parameters

---

## 5. Verify the Connection

After installation, click **Test Connection**. The connector calls `GET /accounts` and returns the workspace name if the key is valid.

A healthy response looks like:
```
Connected to Customer.io — Acme Corp
```

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `app_api_key is required` | No key provided | Enter the App API key in the install form |
| `401 Unauthorized` | Invalid or revoked key | Re-generate the key in Customer.io → Account Settings → API Credentials |
| `403 Forbidden` | Insufficient key permissions | Ensure you are using an App API key (not a Track API key) |
| `429 Too Many Requests` | Rate limit exceeded | The connector retries automatically with exponential back-off |
| No customers returned | Empty workspace or wrong key | Verify the workspace has customers and the key belongs to the correct workspace |

---

## Rate Limits

Customer.io enforces rate limits per API key. The connector includes automatic retry with exponential back-off for 429 responses (up to 3 attempts). The `Retry-After` header is honoured when present.

---

## Pagination

Customer list sync uses cursor-based pagination:

```json
{
  "customers": [...],
  "next": "cursor_opaque_value"
}
```

The connector follows the `next` cursor until it is `null` or absent, fetching all customer records with no limit.

Campaign and newsletter sync use page-based pagination (`?page=1&limit=50`).

---

## Document ID Stability

Each synced document receives a stable ID:
- **Customers:** `SHA-256("customer:" + customer_id)[:16]`
- **Campaigns/Newsletters:** `SHA-256(resource_id)[:16]`

The same Customer.io customer, campaign, or newsletter always maps to the same Shielva document ID across syncs, enabling upsert deduplication without storing seen-ID lists.
