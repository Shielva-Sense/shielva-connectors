# Klaviyo Connector — Setup Guide

This guide walks you through obtaining a Klaviyo Private API key and connecting it to Shielva.

---

## 1. Obtain Your Klaviyo Private API Key

1. Log in to your Klaviyo account at [klaviyo.com](https://www.klaviyo.com).
2. Click your account name in the bottom-left corner, then select **Account**.
3. Go to **Settings** → **API Keys**.
4. Under **Private API Keys**, click **Create Private API Key**.
5. Give the key a name (e.g. "Shielva Connector") and choose a scope:
   - **Full access** — grants read/write to all resources. Recommended for full sync.
   - **Custom key** — select at minimum: Profiles (Read), Campaigns (Read), Lists (Read), Segments (Read).
6. Click **Create** and immediately copy the key — it starts with `pk_` and is shown only once.

> **Security note:** Treat your Private API key like a password. Do not share it or commit it to version control. Shielva stores it AES-256-GCM encrypted at rest.

---

## 2. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **Private API Key** | `pk_your_key_here` |

The connector validates the key by calling `GET /accounts` before completing installation.

---

## 3. What Gets Synced

The connector syncs three resource types on each run:

| Resource | Klaviyo API | Description |
|----------|-------------|-------------|
| Profiles | `GET /profiles` | All subscriber/contact profiles with email, name, phone, location |
| Campaigns | `GET /campaigns` | Email campaigns with name, status, scheduled time |
| Lists | `GET /lists` | Subscriber lists with name and timestamps |

Profile sync uses cursor-based pagination (`page[cursor]`) to handle large audiences — there is no record limit.

---

## 4. API Details

- **Base URL:** `https://a.klaviyo.com/api`
- **API Version:** `2024-02-15` (sent as `revision` header on every request)
- **Auth Header:** `Authorization: Klaviyo-API-Key {api_key}`
- **Response Format:** JSON:API — all resources are wrapped in `{ "data": { ... } }` or `{ "data": [...] }`
- **Pagination:** Cursor-based via `links.next` URL — the connector follows all pages automatically

---

## 5. Verify the Connection

After installation, click **Test Connection**. The connector calls `GET /accounts` and returns the organization name if the key is valid.

A healthy response looks like:
```
Connected to Klaviyo — Acme Corp
```

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `api_key is required` | No key provided | Enter the Private API key in the install form |
| `must start with 'pk_'` | Wrong key type (public key or invalid) | Use a **Private** API key, not a public key |
| `401 Unauthorized` | Invalid or revoked key | Re-generate the key in Klaviyo → Settings → API Keys |
| `403 Forbidden` | Insufficient key scope | Recreate the key with Profiles, Campaigns, Lists, Segments (Read) permissions |
| `429 Too Many Requests` | Rate limit exceeded | The connector retries automatically with exponential back-off |
| No profiles returned | Empty account or wrong key | Verify the account has profiles and the key belongs to the correct account |

---

## Rate Limits

Klaviyo enforces rate limits per API key. The connector includes automatic retry with exponential back-off for 429 responses (up to 3 attempts). The `Retry-After` header is honoured when present.

---

## Pagination

All list endpoints use Klaviyo's cursor-based pagination:

```json
{
  "data": [...],
  "links": {
    "self": "https://a.klaviyo.com/api/profiles?page[size]=100",
    "next": "https://a.klaviyo.com/api/profiles?page[size]=100&page[cursor]=WzE2MjQ4..."
  }
}
```

The connector follows `links.next` until it is `null`, fetching all records with no page limit.

---

## Document ID Stability

Each synced document receives a stable ID computed as `SHA-256(resource_id)[:16]`. The same Klaviyo profile, campaign, or list always maps to the same Shielva document ID across syncs, enabling upsert deduplication without storing seen-ID lists.
