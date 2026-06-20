# Productboard Connector — Setup Guide

## Overview

The Productboard connector syncs features, components, products, notes, and users from your Productboard workspace into Shielva using the [Productboard Public API](https://developer.productboard.com/).

## Prerequisites

- A Productboard account with **Maker** or **Admin** permissions.
- Access to **Account settings** (not workspace settings).

---

## Step 1 — Generate an API Token

1. Log in to [Productboard](https://app.productboard.com/).
2. Click your avatar in the top-right corner and select **Account settings**.
3. In the left sidebar, navigate to **Integrations → API Access** (or search for "API" in the settings search bar).
4. Click **+ Generate token**.
5. Give the token a descriptive name (e.g., `shielva-connector`).
6. Copy the token immediately — Productboard only shows it once.

> If you lose the token, you must revoke it and generate a new one.

---

## Step 2 — Paste the token into Shielva

In the Shielva connector install form, paste the copied token into the **API Token** field and click **Connect**.

Shielva validates the token by calling `GET /me` against the Productboard API. A successful response confirms the token is valid and the connection is live.

---

## API details

| Property | Value |
|---|---|
| Base URL | `https://api.productboard.com/` |
| Required header | `X-Version: 1` |
| Auth header | `Authorization: Bearer <api_token>` |
| Rate limit | ~200 requests per minute (soft cap; 429 is retried automatically) |
| Pagination | Cursor-based via `links.next` URL in list responses |

---

## Resources synced

| Resource | Endpoint | Pagination |
|---|---|---|
| Features | `GET /features` | Cursor (`links.next`) |
| Components | `GET /components` | Cursor (`links.next`) |
| Products | `GET /products` | Single page |
| Notes | `GET /notes` | Cursor (`links.next`) |
| Users | `GET /users` | Single page |

---

## Rate limits & retries

Productboard enforces a soft limit of approximately **200 requests per minute**. The connector automatically retries `429 Too Many Requests` responses with exponential backoff (up to 3 attempts). If your workspace has a very large number of features or notes, the initial sync may take several minutes.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Authentication failed` (401/403) | Token invalid or revoked | Re-generate the token in Productboard |
| `Rate limited` (429) | Too many requests in a short window | The connector retries automatically; wait and re-run if persistent |
| `resource not found` (404) | Feature/component ID no longer exists | Skip expected — stale references are ignored |
| Sync completes with partial status | Some records failed to normalize | Check Shielva logs for the specific item IDs |

---

## Security notes

- The API token grants read access to your entire Productboard workspace.
- Shielva stores the token encrypted at rest using AES-256-GCM.
- Rotate the token regularly via **Account settings → API Access → Revoke**.
