# Pipedrive Connector — Setup Guide

## Overview

The Pipedrive connector syncs deals, persons, organizations, and activities from your Pipedrive CRM account into the Shielva knowledge base. It authenticates using a **Pipedrive API Token** passed as a query parameter (`?api_token=...`) on every request.

---

## Step 1 — Find Your Pipedrive API Token

1. Log into your Pipedrive account.
2. Click your **avatar / profile picture** in the top-right corner.
3. Select **Personal preferences**.
4. Open the **API** tab.
5. Copy the **Your personal API token** value shown at the top of the page.

> Your API token is specific to your Pipedrive user account. Every request made by the connector will be authorized as that user.

---

## Step 2 — Configure the Connector

In the Shielva connector install form, fill in:

| Field | Key | Required | Description |
|---|---|---|---|
| API Key | `api_key` | Yes | Your Pipedrive personal API token from Settings → API |
| Company Domain | `company_domain` | No | Your Pipedrive subdomain (e.g. `mycompany` for `mycompany.pipedrive.com`). Leave blank for standard cloud accounts. |

Paste your API token into the **API Key** field and click **Install**.

---

## Step 3 — Verify the Connection

After clicking Install, the connector calls `GET /users/me` with your API token. A successful response means:

- Health: **HEALTHY**
- Auth status: **CONNECTED**

If authentication fails, the connector returns **INVALID_CREDENTIALS** and installation is aborted. Double-check your API token and try again.

---

## What the Connector Syncs

| Object | Pipedrive Endpoint | Properties Synced |
|---|---|---|
| Deals | `GET /deals` | title, value, currency, status, stage, pipeline, owner, person, organization, expected_close_date |
| Persons | `GET /persons` | name, email, phone, organization, owner, add_time, update_time |
| Organizations | `GET /organizations` | name, address, owner, people_count, open_deals_count, add_time |
| Activities | `GET /activities` | subject, type, due_date, done *(list only — not synced to KB)* |

Pagination uses Pipedrive's **offset-based** pattern (`start` + `limit`), following `additional_data.pagination.more_items_in_collection` until all pages are consumed.

---

## Stable Document IDs

Each synced entity is assigned a **stable 16-character SHA-256 ID** derived from the entity type and Pipedrive ID:

```
source_id = SHA-256("deal:42")[:16]
```

This ensures re-syncs produce identical document IDs, enabling proper deduplication in the knowledge base.

---

## Troubleshooting

### 401 Unauthorized — Invalid API Token

- Your Pipedrive API token may have changed. Go to **Personal preferences → API**, regenerate the token, and update the connector's `api_key` field.
- Ensure you copied the complete token without leading or trailing whitespace.

### 403 Forbidden

- The user account associated with the API token lacks permission to access the requested resource.
- Ensure your Pipedrive user account has at least **read** access to deals, persons, and organizations.
- Contact your Pipedrive admin to review permissions.

### 429 Too Many Requests — Rate Limited

- Pipedrive enforces rate limits on the REST API (typically 10 requests/second or 80 requests/2-second window depending on your plan).
- The connector retries automatically with exponential backoff (up to 3 attempts). If your account has a very large number of records, schedule syncs during off-peak hours.

### Network Timeouts

- Default request timeout is 30 seconds. Large Pipedrive accounts with tens of thousands of records may trigger timeouts.
- The connector retries transient network errors up to 3 times with exponential backoff.

### Connector Shows as DEGRADED

- The circuit breaker opens after 5 consecutive failures, marking the connector as DEGRADED or OFFLINE.
- Resolve the underlying error (auth, network, rate limit), then trigger a health check to reset the circuit breaker.

---

## API Reference

The connector uses the Pipedrive REST API v1:

- Base URL: `https://api.pipedrive.com/v1`
- Authentication: `?api_token={your_token}` query parameter on every request
- Deals: `GET /deals?api_token=...&status=all&limit=100&start=0`
- Persons: `GET /persons?api_token=...&limit=100&start=0`
- Organizations: `GET /organizations?api_token=...&limit=100&start=0`
- Activities: `GET /activities?api_token=...&limit=100&start=0`
- Current User (health): `GET /users/me?api_token=...`

All responses follow the Pipedrive envelope format:

```json
{
  "success": true,
  "data": [...],
  "additional_data": {
    "pagination": {
      "start": 0,
      "limit": 100,
      "more_items_in_collection": true,
      "next_start": 100
    }
  }
}
```

---

## Support

For additional help, refer to the [Pipedrive API documentation](https://developers.pipedrive.com/docs/api/v1) or contact Shielva support.
