# Freshservice Connector — Setup Guide

## Overview

Freshservice is an IT Service Management (ITSM) platform by Freshworks. This connector syncs the following resources into your Shielva knowledge base:

- **Tickets** — IT incidents, service requests, and problems
- **Assets** — CMDB configuration items (laptops, servers, network devices, etc.)
- **Agents** — IT support staff and their contact details
- **Changes** — Planned, emergency, and standard change requests
- **Groups** — Agent groups / support queues
- **Service Catalog Items** — Self-service request catalog entries

---

## Step 1 — Find your API Key

Freshservice supports API key authentication. There are two ways to locate your key:

### Option A: Profile Settings (any agent)

1. Log in to your Freshservice account.
2. Click your **avatar** in the top-right corner.
3. Select **Profile Settings**.
4. Scroll down to **Your API Key**.
5. Copy the key.

### Option B: Admin → Agents (admin only)

1. Go to **Admin** → **Agents**.
2. Click on any agent's name.
3. The **API Key** is shown at the bottom of the agent profile.

> **Note:** Each agent has their own API key. The connector will operate with the permissions of the agent whose key you use. For full sync coverage (tickets, assets, changes), use a key belonging to an Admin or IT agent with read access to all modules.

---

## Step 2 — Find your Subdomain

Your subdomain is the prefix in your Freshservice URL.

| Your Freshservice URL | Subdomain to enter |
|-----------------------|--------------------|
| `https://mycompany.freshservice.com` | `mycompany` |
| `https://acme-it.freshservice.com` | `acme-it` |
| `https://support.freshservice.com` | `support` |

Enter **only the subdomain** — not the full URL, no `https://`, no `.freshservice.com` suffix.

---

## Step 3 — How Authentication Works

The connector uses **HTTP Basic Auth** with:

- **Username:** your API key
- **Password:** the literal string `X` (Freshservice standard)

The `Authorization` header sent on every request:

```
Authorization: Basic base64(api_key:X)
```

This is handled automatically by the connector — you only provide the raw API key in the install form.

---

## Step 4 — Install in Shielva

1. In the Shielva ACP, navigate to **Integrations → Freshservice**.
2. Enter your **API Key** and **Subdomain**.
3. Click **Connect**.

The connector validates credentials by calling `GET /api/v2/agents?per_page=1`. A successful response confirms the connection.

---

## Step 5 — Sync Resources

After installation, trigger a sync from the integrations panel. The sync runs in order:

1. **Tickets** — paginated, with optional `updated_since` for incremental sync
2. **Assets** — CMDB items from the Configuration Management Database
3. **Agents** — all agents in the account
4. **Changes** — change requests with optional `updated_since` filter

Each resource is normalized to a `ConnectorDocument` and ingested into the specified knowledge base.

---

## API Reference

- **Base URL:** `https://{subdomain}.freshservice.com/api/v2/`
- **Auth:** HTTP Basic (`api_key` + `X`)
- **Endpoints used:**
  - `GET /api/v2/agents` — list agents
  - `GET /api/v2/tickets` — list tickets
  - `GET /api/v2/tickets/{id}` — get single ticket
  - `GET /api/v2/assets` — list CMDB assets
  - `GET /api/v2/changes` — list change requests
  - `GET /api/v2/groups` — list agent groups
  - `GET /api/v2/service_catalog/items` — list service catalog items

---

## Permissions Required

The API key's agent account needs read access to:

| Resource | Permission |
|----------|-----------|
| Tickets | View Tickets |
| Assets | View Assets |
| Changes | View Changes |
| Agents | View Agents |
| Service Catalog | View Catalog |

Admin-level agents have all of these by default.

---

## Troubleshooting

### `health=OFFLINE` after install

- Verify the subdomain is entered without `https://` and without `.freshservice.com`.
- Re-copy the API key from Profile Settings — make sure there are no trailing spaces.
- Ensure the Freshservice account is active and not on a trial that has expired.

### `401 Unauthorized`

- The API key is invalid or has been regenerated. Re-copy from Profile Settings and update the connector.

### `403 Forbidden`

- The agent account does not have permission to read the resource. Ask a Freshservice Admin to verify role permissions.

### `429 Too Many Requests`

- Freshservice rate limits vary by plan. The connector retries automatically with exponential backoff. If this persists, reduce sync frequency or upgrade your Freshservice plan.

### Sync returns 0 documents

- The account may be new or empty. Create a test ticket in Freshservice and re-run sync.
- If using incremental sync (`since=<date>`), try a full sync to rule out the date filter excluding all records.
