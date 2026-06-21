# JazzHR Connector — Setup Instructions

## 1. Get your JazzHR API key

1. Log in to JazzHR as an account owner or admin.
2. Navigate to **Settings → Integrations → API**.
3. Click **Generate API Key** (or copy your existing key).
4. The key is a long opaque string — store it in a secret manager; never commit it to source.

## 2. Install the connector in Shielva

1. Open **Shielva → Connectors → Add Connector → JazzHR**.
2. Paste the API key into the **JazzHR API Key** field.
3. Leave **Base URL** at the default (`https://api.resumatorapi.com/v1`) unless JazzHR has
   instructed you to use a regional endpoint.
4. Optional: lower **Rate Limit (requests/min)** below the default 60 if your JazzHR plan
   throttles harder than the standard tier.
5. Click **Install**. The connector calls `GET /jobs?page=1` to verify the key — a healthy
   install returns `HEALTHY` + `CONNECTED`.

## 3. API quirks you should know

- **Auth is a query parameter, not a header.** Every JazzHR request must include
  `?apikey={your_key}`. The connector injects this automatically; you never need to set it
  yourself.
- **POST bodies are form-encoded** (`application/x-www-form-urlencoded`), not JSON. This is
  unusual for a modern REST API and is the most common source of "400 Bad Request" issues.
- **Pagination is `?page=N`**, ~50 items per page. The connector's `sync()` iterates until
  it sees a short or empty page.
- **`/categories` = workflow buckets**, **`/workflows` = stage names within a workflow**.
  This is the opposite of what most ATSes call them — JazzHR's terminology is historical.
- **Notes require `user_id`.** JazzHR rejects notes without an authoring user. Set
  `default_user_id` in the connector config to skip passing it per call.

## 4. Verify

After install, run a health check from the Shielva UI or call:

```bash
curl "https://api.resumatorapi.com/v1/jobs?apikey=YOUR_KEY&page=1"
```

A `200` with a JSON array (possibly empty) means the key works.
