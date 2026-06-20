# Apollo.io Connector — Setup Guide

## Overview

The Apollo.io connector syncs people, contacts, accounts, and sequences from Apollo.io into the Shielva knowledge base using the Apollo REST API v1. Authentication uses an API key passed via the `X-Api-Key` header.

---

## Prerequisites

- An active Apollo.io account (Basic plan or higher for API access)
- API access enabled on your Apollo.io plan

---

## Getting Your API Key

1. Log in to [Apollo.io](https://app.apollo.io).
2. Click your profile icon → **Settings**.
3. Navigate to **Integrations** → **API**.
4. Copy your **API Key** from the API Keys section.
   - If no key exists, click **Create API Key**, give it a name, and copy the generated key.

---

## Installing the Connector

1. In Shielva, open **Connectors** → **Add Connector** → **Apollo.io**.
2. Paste your API key into the **API Key** field.
3. Click **Install**.

The connector will call `POST /v1/auth/health` to verify the key. On success, the connector status changes to **Connected**.

---

## Syncing Data

After installation, trigger a sync from the connector settings page. The sync engine will:

1. Fetch all **contacts** via paginated `POST /v1/contacts/search`.
2. Fetch all **accounts** (companies) via paginated `POST /v1/accounts/search`.
3. Normalize each record into a `ConnectorDocument` and ingest it into the Shielva knowledge base.

Each page fetches 50 records. The engine follows `pagination.total_pages` to fetch all pages.

---

## Available Methods

| Method | Endpoint | Description |
|---|---|---|
| `install()` | POST /v1/auth/health | Validates the API key |
| `health_check()` | POST /v1/auth/health | Checks connectivity |
| `sync()` | contacts + accounts | Full paginated sync |
| `list_people(page)` | POST /v1/mixed_people/search | List people from Apollo DB |
| `list_contacts(page)` | POST /v1/contacts/search | List CRM contacts |
| `get_contact(id)` | GET /v1/contacts/{id} | Get a single contact |
| `list_accounts(page)` | POST /v1/accounts/search | List CRM accounts |
| `get_account(id)` | GET /v1/accounts/{id} | Get a single account |
| `list_sequences()` | GET /v1/emailer_campaigns | List email sequences |

---

## Error Handling

| Error | Cause | Solution |
|---|---|---|
| `ApolloAuthError` (401/403) | Invalid or missing API key | Regenerate your API key in Apollo.io Settings → Integrations → API |
| `ApolloRateLimitError` (429) | Rate limit exceeded | The connector retries automatically with exponential backoff |
| `ApolloNotFoundError` (404) | Resource not found | Verify the contact or account ID is correct |
| `ApolloNetworkError` | Connection timeout or network issue | Check connectivity; the connector will retry transient failures |

---

## Data Model

Every Apollo.io record is normalized into a `ConnectorDocument`:

| Field | Description |
|---|---|
| `source_id` | SHA-256 hash of `"{type}:{apollo_id}"` (first 16 hex chars) |
| `title` | Human-readable title (e.g., `"Contact: Jane Doe"`) |
| `content` | Newline-delimited key-value properties |
| `metadata` | Type-specific fields: name, email, title, company, location |
| `source_url` | LinkedIn URL or website URL when available |

---

## Troubleshooting

**Health check returns OFFLINE after installing:**
- Verify the API key is correct and not expired.
- Ensure your Apollo.io plan includes API access.

**Sync returns `documents_failed > 0`:**
- Some records may have unexpected null values. Check the connector logs for affected record IDs.

**Rate limit errors during sync:**
- Apollo.io enforces per-minute request limits. The connector's retry logic handles this automatically.
  For very large databases, schedule syncs during off-peak hours.
