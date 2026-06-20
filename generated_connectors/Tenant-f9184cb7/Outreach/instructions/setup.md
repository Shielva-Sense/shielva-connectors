# Outreach Connector — Setup Guide

## Overview

The Outreach connector integrates Shielva with the [Outreach](https://www.outreach.io/) sales engagement platform via OAuth2 Authorization Code flow. It syncs prospects, sequences, accounts, calls, and mailings into the Shielva knowledge base.

---

## Prerequisites

- An active Outreach account with admin or API access
- An Outreach OAuth application (see below)

---

## Step 1 — Create an Outreach OAuth Application

1. Log in to Outreach as an admin.
2. Go to **Settings → API → Apps**.
3. Click **Create new app**.
4. Fill in:
   - **Application name**: `Shielva`
   - **Redirect URI**: your Shielva callback URL (e.g. `https://app.shielva.com/oauth/outreach/callback`)
5. Under **Scopes**, enable:
   - `prospects.read`
   - `sequences.read`
   - `mailings.read`
   - `calls.read`
   - `accounts.read`
6. Save. Copy the **Client ID** and **Client Secret**.

---

## Step 2 — Install the Connector in Shielva

Navigate to **Shielva → Connectors → Outreach** and enter:

| Field | Value | Required |
|---|---|---|
| Client ID | From your Outreach OAuth app | Yes |
| Client Secret | From your Outreach OAuth app | Yes |
| Redirect URI | Your registered callback URL | No (uses default) |

Click **Connect**. You will be redirected to the Outreach consent screen.

---

## Step 3 — Authorize Access

1. Shielva generates the authorization URL:
   ```
   https://api.outreach.io/oauth/authorize?response_type=code&client_id=<CLIENT_ID>&scope=prospects.read+sequences.read+mailings.read+calls.read+accounts.read&redirect_uri=<REDIRECT_URI>
   ```
2. Log in to Outreach when prompted and click **Allow**.
3. Outreach redirects back to Shielva with an authorization code.
4. Shielva exchanges the code for `access_token`, `refresh_token`, and `token_expires_at` at:
   ```
   POST https://api.outreach.io/oauth/token
   ```

---

## Step 4 — Verify the Connection

After OAuth completes, Shielva calls `GET /api/v2/users/current` to confirm the token is valid. You should see a **Connected** health status and the authenticated user's email.

---

## Step 5 — Run a Sync

Trigger a sync from **Connectors → Outreach → Sync Now**. The connector will:

1. Paginate all prospects via `GET /api/v2/prospects?page[size]=100` using `links.next` cursor pagination.
2. Paginate all sequences via `GET /api/v2/sequences`.
3. Paginate all accounts via `GET /api/v2/accounts`.
4. Normalize each record into a `ConnectorDocument` with a stable 16-char SHA-256 source ID.
5. Ingest documents into the configured knowledge base.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `get_current_user()` | `GET /api/v2/users/current` | Health check — returns authed user |
| `get_prospects(cursor, count)` | `GET /api/v2/prospects` | Paginated prospect list |
| `get_prospect(id)` | `GET /api/v2/prospects/{id}` | Single prospect by ID |
| `get_sequences(cursor)` | `GET /api/v2/sequences` | Paginated sequence list |
| `get_accounts(cursor)` | `GET /api/v2/accounts` | Paginated account list |
| `get_calls(cursor)` | `GET /api/v2/calls` | Paginated call list |
| `get_mailings(cursor)` | `GET /api/v2/mailings` | Paginated mailing list |
| `refresh_token()` | `POST /api/v2/oauth/token` | Exchange refresh token |

---

## Pagination

Outreach uses **JSON:API cursor pagination**. Each list response includes a `links.next` URL for the next page. The connector follows `links.next` until it is `null`.

```json
{
  "data": [...],
  "links": {
    "next": "https://api.outreach.io/api/v2/prospects?page[after]=cursor_value"
  }
}
```

---

## Token Refresh

Access tokens expire. The connector stores `refresh_token` and calls `POST /api/v2/oauth/token` with `grant_type=refresh_token` to obtain a new `access_token`. Token rotation is handled automatically by the Shielva runtime.

---

## Scopes Reference

| Scope | Access Granted |
|---|---|
| `prospects.read` | Read prospect records |
| `sequences.read` | Read sequence definitions and steps |
| `mailings.read` | Read mailing activity |
| `calls.read` | Read call records |
| `accounts.read` | Read account records |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 Authentication failed` | Token expired or revoked | Re-authorize via OAuth flow |
| `403 Forbidden` | Missing scope | Add the required scope in your Outreach OAuth app |
| `429 Rate limited` | Too many requests | Connector retries automatically with backoff |
| Health check shows OFFLINE | Token not yet obtained | Complete the OAuth authorization flow |
| Empty sync results | No data in Outreach | Verify your account has prospects/sequences/accounts |

---

## Security Notes

- `client_secret` is stored encrypted at rest via the Shielva credential store (AES-256-GCM).
- `access_token` and `refresh_token` are stored in the connector config, encrypted at rest.
- Never commit credentials to source control.
- The connector uses `credentials: "include"` for all cross-origin requests.
