# Freshdesk Connector — Setup Guide

## Prerequisites

You need a Freshdesk account with Agent or Admin access.

---

## Step 1 — Find your Freshdesk domain

Your domain is the subdomain you use to log in to Freshdesk.

- If you access Freshdesk at `https://mycompany.freshdesk.com`, your domain is `mycompany.freshdesk.com`.
- Do not include `https://` — enter the hostname only.

---

## Step 2 — Get your API Key

1. Log in to your Freshdesk account.
2. Click your **avatar / profile picture** in the top-right corner.
3. Select **Profile Settings** from the dropdown menu.
4. Scroll down to the section labelled **Your API Key**.
5. Copy the key shown there.

The API key is a long alphanumeric string (e.g. `7zYBMqNXJkRGD1pE2owH`).

---

## Step 3 — Install the connector

In the Shielva integration builder:

1. Navigate to **Integrations → Freshdesk**.
2. Click **Connect** or **Install**.
3. Enter your **Domain** (e.g. `mycompany.freshdesk.com`).
4. Paste your **API Key**.
5. Click **Save / Install**.

The connector verifies your credentials by calling `GET /api/v2/agents/me`. On success, status is set to **Connected**.

---

## Required permissions

Your Freshdesk account must have at least **Agent** level access. Agent access allows:

- Reading all tickets in your account
- Listing contacts
- Listing agents

Admin access is not required for read-only sync.

---

## What gets synced

| Resource | API endpoint | Notes |
|----------|-------------|-------|
| Tickets | `GET /api/v2/tickets` | Includes conversations (replies) |
| Contacts | `GET /api/v2/contacts` | Name, email, phone, company |
| Agents | `GET /api/v2/agents` | Returned by list_agents() only |

Pagination stops automatically when a page returns an empty array.

---

## Incremental sync

Pass a `since` datetime to `sync()` to fetch only records updated after that timestamp. The connector maps this to the `updated_since` query parameter supported by the Freshdesk API.

---

## Troubleshooting

### 401 Unauthorized
- The API key is wrong or has been reset.
- Go to **Profile Settings → Your API Key** and copy the current key.
- Re-enter it in the connector settings.

### 403 Forbidden
- Your agent account does not have permission to read the resource.
- Confirm that your account is at least an Agent (not a restricted contact).
- Ask a Freshdesk Admin to check your role.

### 429 Too Many Requests — rate limit
- Freshdesk free-plan accounts are limited to approximately **1,000 API requests per hour**.
- The connector retries automatically with exponential backoff (up to 3 attempts).
- If the limit is consistently hit, consider upgrading your Freshdesk plan or reducing sync frequency.

### Domain not found / connection error
- Verify the domain is entered without `https://` (e.g. `mycompany.freshdesk.com`, not `https://mycompany.freshdesk.com`).
- Confirm your Freshdesk account is active and the subdomain exists.

### Tickets missing conversations
- Conversations are fetched per-ticket via a separate API call. If the conversations call fails, the ticket is still synced without conversation content. Check network connectivity and rate limits.
