# Gong Connector — Setup Guide

## Overview

The Gong connector syncs calls, transcripts, and users from your Gong revenue intelligence platform into the Shielva knowledge base using the Gong REST API v2 with HTTP Basic Auth (Access Key + Access Key Secret).

---

## Prerequisites

You need a **Gong account** with API access enabled. API access is available on Gong Business and Enterprise plans. Contact your Gong account manager if API access is not enabled.

---

## Step 1 — Generate a Gong API Access Key

1. Log in to Gong at [https://app.gong.io](https://app.gong.io).
2. Go to **Settings** → **Company Settings** → **Ecosystem** → **API**.
3. Click **Create** to generate a new API key pair.
4. Copy the **Access Key** and **Access Key Secret** — the secret is shown only once.

> Store the Access Key Secret securely. You cannot retrieve it after leaving the page.

---

## Step 2 — Configure Required Scopes / Permissions

Gong API access inherits the permissions of the user or service account that generated the key. Ensure the account has at minimum:

| Permission | Purpose |
|---|---|
| Access calls | Read call metadata and recordings |
| Access users | Read user directory |
| Access transcripts | Read call transcripts |
| Access scorecards | Read coaching scorecards (optional) |

---

## Step 3 — Configure the Connector in Shielva

In the Shielva connector install form, fill in:

| Field | Key | Required | Description |
|---|---|---|---|
| Access Key | `access_key` | Yes | The Access Key from Gong API settings |
| Access Key Secret | `access_key_secret` | Yes | The Access Key Secret from Gong API settings |

---

## What the Connector Syncs

| Resource | Endpoint | Properties Synced |
|---|---|---|
| Calls | `POST /v2/calls` | id, title, started, duration, url, parties |
| Call Transcripts | `POST /v2/calls/transcript` | speaker segments, sentences, timestamps |
| Users | `GET /v2/users` | id, name, email, title, managerId |
| Scorecards | `GET /v2/settings/scorecards` | id, name |

---

## Stable Document IDs

Each document ingested uses a stable ID computed as:

```
SHA-256("call:"       + call_id)[:16]  → call documents
SHA-256("user:"       + user_id)[:16]  → user documents
SHA-256("transcript:" + call_id)[:16]  → transcript documents
```

This ensures idempotent syncs — the same call always produces the same document ID.

---

## Pagination

The connector handles Gong's cursor-based pagination automatically. Gong returns a `records.cursor` field in each response; the connector iterates until `cursor` is null or absent.

Note: Gong uses `POST /v2/calls` (not GET) for listing calls with filters. The cursor and date range are passed in the JSON request body under the `filter` key.

---

## Troubleshooting

### 401 Unauthorized

- The Access Key or Access Key Secret is incorrect or has been revoked.
- Regenerate a new key pair from Gong Settings → API.

### 403 Forbidden

- The Gong account generating the API key does not have permission to access the requested resource.
- Ensure the account has the necessary roles/permissions in Gong.

### 429 Too Many Requests

- Gong API rate limits have been hit.
- The connector retries automatically with exponential backoff (up to 3 attempts).
- For large accounts, schedule syncs during off-peak hours.

### Connector Health is DEGRADED

- Transient network errors are occurring.
- Check network connectivity and retry the health check.

### Scorecards / CRM Deals Not Appearing

- These features require Gong CRM integration or specific Gong plan tiers.
- Verify that your Gong plan includes CRM integration and that deals sync is configured.

---

## API Reference

- **Base URL**: `https://api.gong.io`
- **Authentication**: HTTP Basic Auth (Access Key as username, Access Key Secret as password)
- Calls (list): `POST /v2/calls`
- Call (detail): `GET /v2/calls/{call_id}`
- Call Transcripts: `POST /v2/calls/transcript`
- Users: `GET /v2/users`
- CRM Deals: `GET /v2/crm/deals`
- Scorecards: `GET /v2/settings/scorecards`
- Stats: `GET /v2/stats/activity/account`

---

## Support

For additional help, refer to the [Gong API documentation](https://us-14321.app.gong.io/settings/api) or contact Shielva support.
