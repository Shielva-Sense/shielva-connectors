# ActiveCampaign Connector — Setup Guide

## Overview

The ActiveCampaign connector syncs contacts, deals, campaigns, automations, lists, and tags from your ActiveCampaign account into the Shielva knowledge base. It uses the ActiveCampaign REST API v3 with API key authentication via the `Api-Token` header.

---

## Prerequisites

- An ActiveCampaign account (any plan with API access)
- Account owner or admin role to access Settings → Developer

---

## Step 1 — Find Your API Key and Account Name

1. Log into your ActiveCampaign account.
2. Click the **Settings** icon (gear) in the bottom-left sidebar.
3. Navigate to **Developer** in the left-hand menu.
4. You will see two values on the **API Access** page:
   - **API URL** — e.g. `https://mycompany.api-activecampaign.com`
   - **API Key** — a long alphanumeric string

Your **Account Name** is the subdomain of the API URL. For example, if your API URL is `https://mycompany.api-activecampaign.com`, your account name is `mycompany`.

---

## Step 2 — Configure the Connector in Shielva

In the Shielva connector install form, fill in:

| Field | Key | Type | Required | Description |
|---|---|---|---|---|
| API Key | `api_key` | password | Yes | Your ActiveCampaign API key from Settings → Developer |
| Account Name (subdomain) | `account_name` | string | Yes | The subdomain from your ActiveCampaign API URL (e.g. `mycompany`) |

Click **Connect** to validate the credentials. Shielva calls `GET /api/3/users/me` to confirm the API key is valid and the account is reachable.

---

## What the Connector Syncs

| Object | Endpoint | Properties |
|---|---|---|
| Contacts | `GET /api/3/contacts` | firstName, lastName, email, phone, orgname, cdate, udate |
| Deals | `GET /api/3/deals` | title, value, currency, status, stage, owner, cdate, mdate |
| Campaigns | `GET /api/3/campaigns` | name, type, status, subject, send_amt, opens, cdate |
| Automations | `GET /api/3/automations` | name, status, cdate, mdate |
| Lists | `GET /api/3/lists` | name, id |
| Tags | `GET /api/3/tags` | tag, tagType |

Pagination uses offset-based paging (`limit` + `offset`). The total record count is read from the `meta.total` field in each response. All records are normalized into `ConnectorDocument` objects with a stable 16-character `source_id` derived from `SHA-256("{type}:{id}")[:16]`.

---

## Health Check

The health check calls `GET /api/3/users/me` and returns:

- `HEALTHY` — API key is valid; response includes authenticated user name and email.
- `DEGRADED` — Transient network errors; circuit breaker has not yet opened.
- `OFFLINE` — Authentication failed (401/403) or circuit breaker is open (5 consecutive failures).

---

## Troubleshooting

### 401 Unauthorized — Invalid API Key

- The API key has been regenerated or deleted.
- Go to **Settings → Developer** in ActiveCampaign, copy the current API key, and update the connector.

### 403 Forbidden — Insufficient Permissions

- The account used to generate the API key does not have permission to access the requested resource.
- Ensure the account is an admin or has the required access level.

### Account Name Incorrect

- The account name is the **subdomain** of your API URL, not the full URL.
- Correct: `mycompany` (from `https://mycompany.api-activecampaign.com`)
- Incorrect: `https://mycompany.api-activecampaign.com` (do not enter the full URL)
- Incorrect: `mycompany.api-activecampaign.com` (no protocol, no domain suffix needed)
- Double-check the exact API URL shown in **Settings → Developer** and extract only the subdomain.

### 429 Too Many Requests — Rate Limited

- ActiveCampaign enforces rate limits on API requests.
- The connector retries automatically with exponential backoff (up to 3 attempts, honouring the `Retry-After` header).
- If your account has a very large number of records, consider scheduling syncs during off-peak hours.

### Connector Shows as Degraded

- The circuit breaker opens after 5 consecutive failures, marking the connector as DEGRADED or OFFLINE.
- Resolve the underlying error (auth, network, rate limit), then trigger a health check to reset the circuit breaker.

---

## API Reference

The connector uses the ActiveCampaign REST API v3:

- **Base URL**: `https://{account_name}.api-activecampaign.com/api/3`
- **Authentication**: `Api-Token: {api_key}` request header (not Bearer)
- **Contacts**: `GET /api/3/contacts?limit=100&offset=0`
- **Single contact**: `GET /api/3/contacts/{id}`
- **Lists**: `GET /api/3/lists?limit=100&offset=0`
- **Campaigns**: `GET /api/3/campaigns?limit=100&offset=0`
- **Automations**: `GET /api/3/automations?limit=100&offset=0`
- **Deals**: `GET /api/3/deals?limit=100&offset=0`
- **Single deal**: `GET /api/3/deals/{id}`
- **Tags**: `GET /api/3/tags?limit=100&offset=0`
- **Health / user info**: `GET /api/3/users/me`

---

## Support

For additional help, refer to the [ActiveCampaign API documentation](https://developers.activecampaign.com/reference) or contact Shielva support.
