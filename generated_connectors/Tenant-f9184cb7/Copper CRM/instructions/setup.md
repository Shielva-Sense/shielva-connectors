# Copper CRM Connector — Setup Guide

## Overview

Copper (formerly ProsperWorks) is the CRM built for Google Workspace. This connector syncs **people** (contacts), **companies**, **opportunities**, and **tasks** into Shielva using Copper's Developer API v1.

Authentication uses two credentials — an **API Key** and the **user email** associated with that key — sent as custom request headers on every API call.

---

## Step 1 — Generate a Copper API Key

1. Log in to [app.copper.com](https://app.copper.com).
2. Click your avatar (bottom-left corner) → **Settings**.
3. In the left sidebar select **Integrations** → **API Keys**.
4. Click **Generate API Key**.
5. Copy the key immediately — it is shown only once.

> **Tip:** If you already have an existing key, you can use it here. Each key is tied to the user who generated it.

---

## Step 2 — Find your Copper user email

The API key is always scoped to a specific Copper user. The **user email** must match the email address of the account that owns the API key.

To confirm your email: click your avatar → **Profile** → the email shown is your `user_email` value.

---

## Step 3 — Enter credentials in Shielva

When installing the Copper CRM connector in Shielva, fill in:

| Field | Description |
|---|---|
| **API Key** | The key you generated in Step 1 |
| **User Email** | The Copper account email from Step 2 |

---

## Authentication — how it works

Every request to the Copper API uses three custom headers:

```
X-PW-AccessToken: <api_key>
X-PW-Application: developer_api
X-PW-UserEmail:   <user_email>
Content-Type:     application/json
```

> Note: Copper requires `Content-Type: application/json` on **all** requests, including `GET` requests that carry no body. The connector handles this automatically.

---

## Resources synced

| Resource | Copper Endpoint | Method |
|---|---|---|
| People (contacts) | `/people/search` | POST |
| Companies | `/companies/search` | POST |
| Opportunities | `/opportunities/search` | POST |
| Tasks | `/tasks/search` | POST |
| Activity Types | `/activity_types` | GET |

All list operations use `POST` with a JSON body (`page_number`, `page_size`) because Copper's search endpoints require a request body for pagination — even when listing all records.

---

## Pagination

The connector pages through all records using `page_size: 200` (Copper's maximum) until a page returns fewer than 200 records, signalling the last page.

---

## Permissions required

The Copper user whose API key you provide must have at least **read access** to the resources you want to sync. Admin-level access is recommended to ensure all records are visible.

---

## Troubleshooting

| Error | Likely cause | Resolution |
|---|---|---|
| 401 Unauthorized | Wrong API key or mismatched user email | Regenerate the key in Copper Settings → API Keys; confirm the user email matches exactly |
| 403 Forbidden | The user lacks permission to a resource | Grant the user read access in Copper Settings → Teams & Permissions |
| 429 Too Many Requests | Rate limit hit | The connector automatically retries with exponential back-off; reduce sync frequency if this persists |
| 404 Not Found | Resource ID no longer exists | Stale reference; safe to ignore |

---

## Support

For Copper API documentation: [developer.copper.com](https://developer.copper.com)
For Shielva connector support: support@shielva.com
