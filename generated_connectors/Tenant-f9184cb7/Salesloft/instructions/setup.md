# Salesloft Connector — Setup Guide

## Overview

The Salesloft connector syncs people, cadences, calls, emails, and accounts from your Salesloft sales engagement platform into the Shielva knowledge base using the Salesloft REST API v2. Authentication uses OAuth2 Authorization Code flow.

---

## Prerequisites

- A Salesloft account (any plan that provides API access)
- Admin access to create an OAuth application in the Salesloft developer portal

---

## Step 1: Create a Salesloft OAuth Application

1. Log into your Salesloft account.
2. Navigate to **Settings → Integrations → API** or visit [https://developers.salesloft.com](https://developers.salesloft.com).
3. Click **Create Application**.
4. Fill in:
   - **Application Name**: Shielva Connector (or any descriptive name)
   - **Redirect URI**: Your Shielva callback URL (e.g., `https://your-app.shielva.com/oauth/callback/salesloft`)
5. Copy the **Client ID** and **Client Secret** that are generated.

---

## Step 2: Configure the Connector in Shielva

Open the Shielva ACP and navigate to **Connectors → Add Connector → Salesloft**.

Fill in the following fields:

| Field | Required | Description |
|---|---|---|
| **Client ID** | Yes | The OAuth Client ID from the Salesloft developer portal |
| **Client Secret** | Yes | The OAuth Client Secret from the Salesloft developer portal |
| **Redirect URI** | No | The redirect URI registered with your OAuth application |

Click **Install** to validate your credentials.

---

## Step 3: Authorize via OAuth2

After installing, click **Authorize** to be redirected to Salesloft's OAuth2 consent screen at:

```
https://accounts.salesloft.com/oauth/authorize
```

Log in with your Salesloft credentials and grant the requested scopes (`read`).

You will be redirected back to Shielva with an authorization code. Shielva will automatically exchange this code for an `access_token` and `refresh_token`.

---

## Step 4: Verify Connection

After authorization, use **Health Check** to verify the connection:

- Returns **HEALTHY** if `GET /v2/me.json` responds with your authenticated user info
- Returns **DEGRADED** on transient network errors (circuit breaker engaged)
- Returns **OFFLINE** on auth failures or missing credentials

---

## Step 5: Sync Data

Use the **Sync** action to pull all Salesloft data into Shielva:

- **People** — contacts/leads from `/v2/people.json`
- **Cadences** — sequences/workflows from `/v2/cadences.json`
- **Calls** — call activities from `/v2/activities/calls.json`
- **Emails** — email activities from `/v2/activities/emails.json`
- **Accounts** — companies from `/v2/accounts.json`

All records are normalized into `ConnectorDocument` objects with stable SHA-256 source IDs. Pagination is handled automatically (page + per_page, following `metadata.paging.next_page`).

---

## Authentication Details

| Parameter | Value |
|---|---|
| **Auth Type** | OAuth2 Authorization Code |
| **Authorization URL** | `https://accounts.salesloft.com/oauth/authorize` |
| **Token URL** | `https://accounts.salesloft.com/oauth/token` |
| **Scopes** | `read` |
| **Token Storage** | `access_token`, `refresh_token`, `token_expires_at` |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Health check returns OFFLINE with 401 | Access token expired or revoked | Re-authorize via OAuth2 |
| Health check returns DEGRADED | Transient network error | Retry — circuit breaker will reset after 60 s |
| `install()` returns MISSING_CREDENTIALS | `client_id` or `client_secret` not provided | Fill in both fields in the connector config |
| `authorize()` raises SalesloftAuthError | `client_id` is empty | Provide a valid Client ID from the developer portal |
| Rate limit errors (429) | Too many API requests | The connector automatically retries with exponential backoff and honours the `Retry-After` header |

---

## Data Normalizer Details

| Object | Source ID Formula | `object_type` |
|---|---|---|
| Person | `sha256("person:" + str(id))[:16]` | `person` |
| Cadence | `sha256("cadence:" + str(id))[:16]` | `cadence` |
| Call | `sha256("call:" + str(id))[:16]` | `call` |

Source IDs are stable and idempotent — the same Salesloft record always maps to the same Shielva document ID.
