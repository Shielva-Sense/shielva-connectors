# PandaDoc Connector — Setup Guide

## Overview

The PandaDoc connector integrates Shielva with the [PandaDoc API v1](https://developers.pandadoc.com/reference/about). It uses API Key authentication — you generate the key directly in PandaDoc and provide it to Shielva. No OAuth browser flow is required.

The connector syncs: **Documents**, **Templates**, **Contacts**, **Forms**, and **Members** from your PandaDoc workspace.

---

## Prerequisites

- A PandaDoc account (Essentials plan or higher for API access)
- Access to PandaDoc Settings with permissions to manage API integrations

---

## Step 1 — Generate a PandaDoc API Key

1. Log in to your PandaDoc account at [app.pandadoc.com](https://app.pandadoc.com).
2. Click your profile avatar (bottom-left) → **Settings**.
3. In the left sidebar, navigate to **Integrations** → **API**.
4. Under the **API Keys** section, click **Generate API Key**.
5. Give the key a name (e.g., `Shielva Connector`).
6. Click **Generate** and **copy the key immediately** — it will not be shown again.

> **Important:** PandaDoc API keys grant full access to your account data. Store the key securely and do not share it.

---

## Step 2 — Identify the header format

The PandaDoc API uses a non-standard API key header format:

```
Authorization: API-Key YOUR_API_KEY
```

Note the `API-Key` prefix (not `Bearer`). The Shielva connector handles this automatically.

---

## Step 3 — Configure the connector in Shielva

1. In the Shielva ARC dashboard, go to **Connectors** → **Add Connector**.
2. Select **PandaDoc** from the catalog.
3. In the **API Key** field, paste the key you copied in Step 1.
4. Click **Install** — Shielva will verify the key by calling `GET /workspaces/` and confirm the workspace name.

---

## Step 4 — Verify the connection

After installation, the connector status should show **Healthy** with your PandaDoc workspace name. If it shows **Invalid Credentials**, double-check that:
- The key was copied without extra spaces
- The PandaDoc user account is active and not suspended
- The account plan includes API access (Essentials tier or above)

---

## Step 5 — Run a sync

Click **Sync Now** in the connector settings to pull all documents, templates, contacts, and forms into your Shielva knowledge base. Future syncs can be scheduled or triggered on-demand.

---

## API Base URL

```
https://api.pandadoc.com/public/v1/
```

## Resources synced

| Resource | PandaDoc Endpoint | Notes |
|----------|-------------------|-------|
| Documents | `GET /documents` | Paginated, count+page |
| Document Details | `GET /documents/{id}/details` | Fields + tokens |
| Templates | `GET /templates` | Paginated |
| Contacts | `GET /contacts` | Paginated |
| Forms | `GET /forms` | Paginated |
| Members | `GET /members` | Workspace members |

---

## Sandbox / Testing

PandaDoc does not provide a separate sandbox environment. For testing:
- Create a free Developer account at [developers.pandadoc.com](https://developers.pandadoc.com)
- Use that account's API key when validating the connector
- All operations are read-only (list + get), so sandbox testing is safe

---

## Troubleshooting

| Error | Likely Cause | Fix |
|-------|-------------|-----|
| 401 Unauthorized | Wrong API key | Regenerate and re-enter the key |
| 403 Forbidden | Insufficient plan | Upgrade to Essentials or higher |
| 429 Too Many Requests | Rate limit hit | The connector retries automatically (max 3 attempts) |
| Network timeout | Transient PandaDoc outage | Connector retries with exponential backoff |

---

## Security notes

- The API key is stored encrypted in Shielva's credential store (AES-256-GCM).
- The key is transmitted only over TLS to `api.pandadoc.com`.
- Revoke the key in PandaDoc Settings if the connector is removed or compromised.
