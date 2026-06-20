# Domo Connector — Setup Guide

## Overview

The Domo connector uses **OAuth2 client credentials** to access your Domo instance. You will need to create a Domo API client at [developer.domo.com](https://developer.domo.com) to obtain a Client ID and Client Secret.

---

## Step 1 — Create a Domo API Client

1. Log in at [developer.domo.com](https://developer.domo.com).
2. Click **My Account** (top-right) → **New Client**.
3. Fill in:
   - **Name** — e.g. `Shielva Integration`
   - **Description** — e.g. `Shielva ACP connector`
   - **Application Scopes** — select at minimum: `Data`, `User`, `Dashboard`
4. Click **Create**.
5. Copy the **Client ID** and **Client Secret** shown — these are only displayed once.

---

## Step 2 — Configure the connector in Shielva

In the Shielva ACP connector install form, enter:

| Field | Value |
|-------|-------|
| **Client ID** | Paste the Client ID from your Domo client |
| **Client Secret** | Paste the Client Secret |

Click **Save & Connect**.

---

## How authentication works

The connector exchanges your Client ID and Client Secret for a short-lived Bearer token using:

```
GET https://api.domo.com/oauth/token
    ?grant_type=client_credentials
    &scope=data%20user%20dashboard
Authorization: Basic base64(client_id:client_secret)
```

All subsequent API calls use `Authorization: Bearer <access_token>`. Tokens expire after 1 hour; the connector automatically re-acquires a token on each sync and health check.

---

## What the connector syncs

| Resource | API Endpoint |
|----------|-------------|
| Datasets | `GET /v1/datasets?limit=50&offset=N` |
| Dashboard Pages | `GET /v1/pages?limit=50&offset=N` |
| Users | `GET /v1/users?limit=500&offset=N` |
| Groups | `GET /v1/groups?limit=500&offset=N` |

Pagination uses `limit` + `offset`. All responses are plain JSON arrays. Resources are normalized to `ConnectorDocument` with stable IDs derived from `sha256("<type>:<id>")[:16]` for deduplication across syncs.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `401 Auth error` | Invalid Client ID or Secret | Regenerate your Domo client credentials |
| `403 Forbidden` | Insufficient scope | Re-create your Domo client with Data + User + Dashboard scopes |
| `429 Rate limited` | Too many requests | The connector retries automatically (up to 3 attempts with exponential backoff) |
| `Connector status: OFFLINE / MISSING_CREDENTIALS` | client_id or client_secret not entered | Enter both fields in the connector install form |

---

## Revoking access

To remove Shielva's access to your Domo instance:

1. Go to [developer.domo.com](https://developer.domo.com) → **My Account**.
2. Find the client named `Shielva Integration` and delete it.

This immediately invalidates all tokens issued under that client.
