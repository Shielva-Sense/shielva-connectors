# Gorgias Connector — Setup Guide

Gorgias is a helpdesk platform purpose-built for eCommerce brands. It centralises support tickets from email, live chat, social media, SMS, and voice into a single workspace, and integrates natively with Shopify, BigCommerce, and Magento to show order data inline inside every ticket.

---

## Prerequisites

- An active Gorgias account with **Admin** or **Agent** access.
- Your Gorgias **subdomain** — the part before `.gorgias.com` in your workspace URL (e.g. if your URL is `https://mystore.gorgias.com`, your subdomain is `mystore`).

---

## Step 1 — Generate a Gorgias API Key

1. Log in to your Gorgias workspace.
2. Navigate to **Settings → REST API** (left sidebar, under Integrations).
3. Click **Generate new API key**.
4. Give the key a name (e.g. `Shielva Sync`).
5. Copy the generated API key — it is shown **once** and cannot be retrieved later.
6. Note the **email address** on your Gorgias account (Settings → Profile).

---

## Step 2 — Enter Credentials in Shielva

| Field | Value | Example |
|-------|-------|---------|
| **Gorgias Account (subdomain)** | Your workspace subdomain | `mystore` |
| **Email** | The email on your Gorgias account | `support@mystore.com` |
| **API Key** | The key generated in Step 1 | `tok_...` |

---

## Authentication Details

Gorgias uses **HTTP Basic Auth**. The connector automatically encodes your credentials:

```
Authorization: Basic base64(email:api_key)
```

No OAuth flow is required. Credentials are stored encrypted in Shielva's secrets vault and are never logged.

---

## What Gets Synced

| Resource | Gorgias Endpoint | Notes |
|----------|-----------------|-------|
| **Tickets** | `GET /api/tickets` | All support conversations; cursor paginated |
| **Customers** | `GET /api/customers` | Customer profiles with channel data |
| **Tags** | `GET /api/tags` | All workspace tags |
| **Macros** | `GET /api/macros` | Saved response templates |
| **Satisfaction Surveys** | `GET /api/satisfaction-surveys` | CSAT responses (when enabled) |

Pagination uses Gorgias **cursor-based pagination** via `meta.next_cursor`. All pages are exhausted per sync run.

---

## eCommerce Context

The Gorgias connector is especially valuable for eCommerce businesses because:

- Tickets often contain order numbers, shipping statuses, and return requests.
- Customer profiles link to Shopify/BigCommerce order history.
- Macros encode your team's standard operating procedures for common issues (refunds, tracking, sizing questions).

Syncing this data into Shielva's knowledge base allows your AI to answer customer support questions with full awareness of your support history and playbooks.

---

## Troubleshooting

| Error | Likely Cause | Resolution |
|-------|-------------|------------|
| `Authentication failed (401)` | Wrong email or API key | Re-generate the API key in Gorgias Settings |
| `Authentication failed (403)` | Key lacks required permissions | Ensure the key owner has Agent or Admin role |
| `Rate limited (429)` | Too many API calls | The connector respects `Retry-After`; reduce sync frequency |
| `resource 'X' not found (404)` | Resource deleted between pages | Safe to ignore — the connector continues |

---

## Security Notes

- API keys grant access to **all tickets and customer data** in your Gorgias workspace. Treat them as sensitive credentials.
- Rotate your Gorgias API key immediately if you suspect it has been compromised (Settings → REST API → Revoke).
- The Shielva connector stores the key in an AES-256-GCM encrypted vault; it is never transmitted in plaintext.
