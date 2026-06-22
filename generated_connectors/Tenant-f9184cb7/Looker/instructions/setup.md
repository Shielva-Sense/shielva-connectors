# Looker Connector Setup Guide

## Overview

The Looker connector syncs **Looks** and **Dashboards** from your Google Looker instance into the Shielva knowledge base using the [Looker REST API 4.0](https://developers.looker.com/api/explorer/4.0/). Authentication uses OAuth2 client credentials (API3 Client ID + Secret) to obtain a Bearer access token.

---

## Prerequisites

- A Looker instance accessible over HTTPS (typically on port `19999`).
- An API3 key pair (Client ID + Client Secret) with at least `see_looks` and `see_dashboards` permissions.

---

## Step 1 — Generate API3 Credentials in Looker

1. Log in to your Looker instance as an **Admin** or as the user who will own the API key.
2. Navigate to **Admin → Users** (or **Account Settings → API3 Keys** for self-service).
3. Click **Edit** next to the target user, then scroll to **API3 Keys**.
4. Click **New API3 Key**.
5. Copy the **Client ID** and **Client Secret** — the secret is only shown once.

---

## Step 2 — Install Fields

| Field | Description | Required |
|-------|-------------|----------|
| `base_url` | Full Looker instance URL including port, e.g. `https://mycompany.looker.com:19999` | Yes |
| `client_id` | API3 Client ID from Looker → Users → API3 Keys | Yes |
| `client_secret` | API3 Client Secret (shown once at creation) | Yes |

---

## Step 3 — Authentication Flow

The connector uses **OAuth2 Client Credentials**:

1. `POST {base_url}/api/4.0/login` with form body `client_id=...&client_secret=...`
2. Looker returns `{"access_token": "...", "token_type": "Bearer", "expires_in": 3600}`
3. All subsequent requests carry `Authorization: Bearer {access_token}`

Tokens expire after ~1 hour. The connector re-authenticates automatically at the start of each `sync()` or `health_check()` call.

---

## Step 4 — Sync Behaviour

`sync()` performs:
1. `login()` — exchange credentials for access token
2. `GET /api/4.0/looks` — fetch all saved Looks (up to 500)
3. `GET /api/4.0/dashboards` — fetch all dashboards
4. Each item is normalized to a `ConnectorDocument` with a stable SHA-256-based `source_id` and pushed to the Shielva knowledge base.

**SyncResult statuses:**
- `completed` — all items fetched and normalized without errors
- `partial` — some items failed; `documents_failed > 0`
- `failed` — login itself failed (bad credentials, network down)

---

## Step 5 — Error Handling

| Exception | Cause | Retried? |
|-----------|-------|----------|
| `LookerAuthError` | 401 / 403 — bad Client ID or Secret | No |
| `LookerNotFoundError` | 404 — resource not found | No |
| `LookerRateLimitError` | 429 — too many requests | Yes (respects `Retry-After`) |
| `LookerNetworkError` | Timeout / connection refused | Yes (3× exponential backoff) |
| `LookerError` | 5xx server errors | Yes (3× exponential backoff) |

---

## Step 6 — Required Looker Permissions

The API3 key user must have at minimum:

- `see_looks` — list and read Looks
- `see_dashboards` — list and read Dashboards
- `see_lookml_models` — list LookML models (for `list_models()`)
- `access_data` — required to run Looks via `run_look()`

---

## Troubleshooting

**401 Authentication failed**
- Verify `client_id` and `client_secret` match the Looker API3 key.
- Confirm the key is enabled (not revoked).

**Connection error / timeout**
- Ensure `base_url` includes port `19999` and the instance is accessible from the Shielva runtime network.
- Check that HTTPS is reachable (valid TLS certificate or custom CA if self-hosted).

**404 on a Look or Dashboard**
- The item may have been deleted. Re-run `list_looks()` / `list_dashboards()` to verify current state.

**Empty results on sync**
- The API3 user may lack `see_looks` / `see_dashboards` permissions.
- Shared content must be in a **Shared** folder visible to the API3 user.
