# Brevo Connector — Setup Guide

## Overview

The Brevo connector syncs contacts and email campaigns from your Brevo account (formerly Sendinblue) into the Shielva knowledge base. It uses the Brevo REST API v3 with API key authentication via the `api-key` request header (Brevo-specific — not Bearer, not Authorization).

---

## Prerequisites

- A Brevo account (any plan with API access)
- Access to Brevo Settings → API Keys

---

## Step 1 — Find Your Brevo API Key

1. Log into your Brevo account at [app.brevo.com](https://app.brevo.com).
2. Click your account name (top-right corner) → **Profile & Settings**.
3. Navigate to **API Keys** in the left menu (or go to `Settings → API Keys`).
4. Click **Generate a new API key** or copy an existing one.

Keep this key secure — treat it like a password.

---

## Step 2 — Configure the Connector in Shielva

In the Shielva connector install form, fill in:

| Field | Key | Type | Required | Description |
|---|---|---|---|---|
| API Key | `api_key` | password | Yes | Your Brevo API key from Settings → API Keys |

Click **Connect** to validate the credentials. Shielva calls `GET /v3/account` to confirm the API key is valid and the account is reachable.

---

## What the Connector Syncs

| Object | Endpoint | Properties |
|---|---|---|
| Contacts | `GET /v3/contacts` | email, FIRSTNAME, LASTNAME, listIds, createdAt, modifiedAt |
| Email Campaigns | `GET /v3/emailCampaigns` | name, subject, status, sentDate, createdAt, statistics |

Pagination uses offset-based paging (`limit` + `offset`). The total record count is read from the `count` field in each response. All records are normalized into `ConnectorDocument` objects with a stable 16-character `source_id`:

- Contact: `SHA-256("contact:{id}:{email}")[:16]`
- Campaign: `SHA-256("campaign:{id}")[:16]`
- Template: `SHA-256("template:{id}")[:16]`

---

## Health Check

The health check calls `GET /v3/account` and returns:

- `HEALTHY` — API key is valid; response includes account email and plan type.
- `DEGRADED` — Transient network errors.
- `OFFLINE` — Authentication failed (401/403).

---

## Available Methods

| Method | Description |
|---|---|
| `install()` | Validate API key via GET /v3/account |
| `health_check()` | Return health status with account email and plan |
| `sync()` | Full paginated sync of contacts + email campaigns |
| `list_contacts(limit, offset)` | Paginated list of contacts |
| `get_contact(identifier)` | Single contact by email or id |
| `list_contact_lists(limit, offset)` | Paginated list of contact lists |
| `list_campaigns(status, limit, offset)` | Paginated email campaigns, optional status filter |
| `list_senders()` | All senders in the account |

---

## Troubleshooting

### 401 Unauthorized — Invalid API Key

- The API key has been regenerated, deleted, or is incorrect.
- Go to **Brevo Settings → API Keys**, copy the current key, and update the connector.

### 403 Forbidden — Insufficient Permissions

- The API key does not have permission to access the requested resource.
- Ensure the key has the required scopes (contacts, campaigns, senders).

### 429 Too Many Requests — Rate Limited

- Brevo enforces rate limits on API requests.
- The connector retries automatically with exponential backoff (up to 3 attempts, honouring the `Retry-After` header).

### Connector Shows as Degraded

- Transient network errors are causing repeated failures.
- Resolve the underlying network issue and trigger a health check.

---

## API Reference

The connector uses the Brevo REST API v3:

- **Base URL**: `https://api.brevo.com`
- **Authentication**: `api-key: {api_key}` request header
- **Account**: `GET /v3/account`
- **Contacts**: `GET /v3/contacts?limit=50&offset=0`
- **Single contact**: `GET /v3/contacts/{identifier}`
- **Contact lists**: `GET /v3/contacts/lists?limit=50&offset=0`
- **Email campaigns**: `GET /v3/emailCampaigns?limit=50&offset=0`
- **Campaign report**: `GET /v3/emailCampaigns/{id}`
- **Senders**: `GET /v3/senders`
- **SMTP templates**: `GET /v3/smtp/templates?limit=50&offset=0`

---

## Support

For additional help, refer to the [Brevo API documentation](https://developers.brevo.com/reference) or contact Shielva support.
