# ConvertKit Connector — Setup Guide

This guide walks you through obtaining your ConvertKit API credentials and connecting them to Shielva.

---

## 1. Obtain Your ConvertKit API Key and API Secret

1. Log in to your ConvertKit account at [app.convertkit.com](https://app.convertkit.com).
2. Click your account name in the top-right corner, then select **Settings**.
3. Go to **Advanced** in the left-hand sidebar.
4. Scroll down to the **API** section.
5. Copy your **API Key** — this is used for all read operations.
6. Copy your **API Secret** — this is required to list subscribers.

> **Security note:** Treat your API Key and API Secret like passwords. Do not share them or commit them to version control. Shielva stores them AES-256-GCM encrypted at rest.

---

## 2. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value | Required |
|-------|-------|----------|
| **API Key** | `your-api-key` | Yes |
| **API Secret** | `your-api-secret` | No (required for subscriber sync) |

The connector validates the API Key by calling `GET /v3/account` before completing installation.

---

## 3. What Gets Synced

The connector syncs three resource types on each run:

| Resource | ConvertKit API | Description |
|----------|----------------|-------------|
| Subscribers | `GET /v3/subscribers` | All subscribers with email, name, state, custom fields |
| Sequences | `GET /v3/sequences` | Email automation sequences with name, hold/repeat settings |
| Forms | `GET /v3/forms` | Opt-in forms with name, type, and embed URL |

Subscriber sync uses page-based pagination (`page` param) with `total_subscribers` as the termination check — there is no record limit.

---

## 4. API Details

- **Base URL:** `https://api.convertkit.com`
- **API Version:** v3 (path prefix `/v3/`)
- **Read Auth:** `?api_key={api_key}` query parameter on all GET requests
- **Subscriber Auth:** `?api_secret={api_secret}` query parameter on `GET /v3/subscribers`
- **Response Format:** JSON — resources are returned in named arrays (`subscribers`, `forms`, `courses`)
- **Pagination:** Page-based via `?page=` parameter; `total_subscribers` field indicates total count

---

## 5. Verify the Connection

After installation, click **Test Connection**. The connector calls `GET /v3/account` and returns the account name if the key is valid.

A healthy response looks like:
```
Connected to ConvertKit — Acme Creators
```

---

## 6. Additional Methods

Beyond sync, the connector exposes these direct API methods:

| Method | Description |
|--------|-------------|
| `list_subscribers(page, per_page)` | Paginated subscriber list |
| `get_subscriber(id)` | Single subscriber by ID |
| `list_tags(page)` | All tags |
| `list_sequences(page)` | All email sequences |
| `list_forms(page)` | All forms |
| `list_broadcasts(page)` | All broadcast (one-off) emails sent |

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `api_key is required` | No key provided | Enter the API Key in the install form |
| `401 Unauthorized` | Invalid or revoked API key | Re-copy the key from ConvertKit → Settings → Advanced |
| `403 Forbidden` | Wrong credential type | Ensure you are using the API Key (not a subscriber token) |
| `429 Too Many Requests` | Rate limit exceeded | The connector retries automatically with exponential back-off |
| Subscribers not syncing | Missing API Secret | Add the API Secret in the connector settings — it is required for subscriber listing |

---

## Rate Limits

ConvertKit enforces rate limits per API key. The connector includes automatic retry with exponential back-off for 429 responses (up to 3 attempts). The `Retry-After` header is honoured when present.

---

## Pagination

Subscriber sync uses page-based pagination:

```json
{
  "total_subscribers": 5000,
  "page": 1,
  "subscribers": [...]
}
```

The connector increments `page` until `total_subscribers` is reached or the returned page is smaller than `per_page` (1000). All records are fetched with no record limit.

---

## Document ID Stability

Each synced document receives a stable ID computed as `SHA-256("subscriber:" + id)[:16]` (or `"sequence:"` / `"form:"` prefix for those types). The same ConvertKit resource always maps to the same Shielva document ID across syncs, enabling upsert deduplication without storing seen-ID lists.
