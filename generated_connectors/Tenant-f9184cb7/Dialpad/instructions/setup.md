# Dialpad Connector — Setup Guide

## Overview

The Dialpad connector syncs call logs and contacts from your Dialpad account into the Shielva knowledge base using the Dialpad REST API v2 with OAuth 2.0 authentication.

---

## Prerequisites

You need a **Dialpad account** (Standard, Pro, or Enterprise). You also need access to the [Dialpad Developer Portal](https://developers.dialpad.com/) to create an OAuth App.

---

## Step 1 — Create a Dialpad OAuth App

1. Sign in to the [Dialpad Developer Portal](https://developers.dialpad.com/).
2. Navigate to **My Apps** and click **Create App**.
3. Choose **OAuth** as the app type.
4. Fill in:
   - **App Name**: e.g., "Shielva Connector"
   - **Description**: Optional description of the integration
5. Under **OAuth Credentials**, note your **Client ID** and **Client Secret**.

---

## Step 2 — Configure OAuth Scopes

In your Dialpad App settings, grant the following scopes:

| Scope | Purpose |
|---|---|
| `calls` | Read call logs |
| `contacts` | Read contact information |
| `users` | Read user profiles (required for health check) |

---

## Step 3 — Set the Redirect URI

Under **OAuth Settings**, add your Shielva OAuth callback URL:

```
https://your-shielva-domain.com/oauth/callback
```

This must match exactly what you provide in the `redirect_uri` install field.

---

## Step 4 — Configure the Connector in Shielva

In the Shielva connector install form, fill in:

| Field | Key | Required | Description |
|---|---|---|---|
| Client ID | `client_id` | Yes | From Dialpad App OAuth Credentials |
| Client Secret | `client_secret` | Yes | From Dialpad App OAuth Credentials |
| Redirect URI | `redirect_uri` | No | Your OAuth callback URL |

---

## Step 5 — Complete the OAuth Flow

After installing the connector:

1. Shielva will redirect you to the Dialpad consent screen.
2. Log in to Dialpad and authorize the requested scopes.
3. Dialpad redirects back to Shielva with an authorization code.
4. Shielva exchanges the code for an `access_token` and `refresh_token`.
5. The connector is now authorized and ready to sync.

Token storage fields: `access_token`, `refresh_token`, `token_expires_at`.

---

## What the Connector Syncs

| Resource | Endpoint | Properties Synced |
|---|---|---|
| Call Logs | `GET /api/v2/call` | id, direction, duration, started_at, ended_at, from_number, to_number, status, target |
| Contacts | `GET /api/v2/contacts` | id, display_name, email, phone, company, job_title |

Additional list methods available:

| Method | Endpoint |
|---|---|
| `list_users()` | `GET /api/v2/users` |
| `list_departments()` | `GET /api/v2/departments` |

---

## Pagination

The Dialpad API uses cursor-based pagination. The connector automatically follows all pages using the `cursor` field in each response until all records are retrieved.

---

## Token Refresh

The connector supports refreshing the OAuth access token using the stored `refresh_token`:

- **URL**: `https://dialpad.com/oauth2/token`
- **Grant type**: `refresh_token`
- **Parameters**: `client_id`, `client_secret`, `refresh_token`

---

## Stable Document IDs

Each document ingested into the knowledge base uses a stable ID computed as:

```
SHA-256("call:" + call_id)[:16]     # for call logs
SHA-256("contact:" + contact_id)[:16]  # for contacts
SHA-256("user:" + user_id)[:16]     # for users
```

This ensures idempotent syncs — the same record always produces the same document ID regardless of how many times it is synced.

---

## Troubleshooting

### 401 Unauthorized

- The `access_token` has expired or been revoked.
- Re-authorize via the OAuth flow or trigger a token refresh.

### 403 Forbidden — Missing Scope

- The Dialpad App is missing one or more required scopes (`calls`, `contacts`, `users`).
- Update your app in the Dialpad Developer Portal and re-authorize.

### 429 Too Many Requests

- Dialpad API rate limits have been reached.
- The connector retries automatically with exponential backoff (up to 3 attempts, honouring the `Retry-After` header).

### Connector Health is DEGRADED

- Transient network errors have tripped the circuit breaker (5 consecutive failures).
- Resolve the underlying network or auth issue, then trigger a health check to reset.

---

## API Reference

- **Base URL**: `https://dialpad.com`
- **Auth URL**: `https://dialpad.com/oauth2/authorize`
- **Token URL**: `https://dialpad.com/oauth2/token`
- Current user: `GET /api/v2/users/me`
- Users: `GET /api/v2/users`
- Call logs: `GET /api/v2/call`
- Contacts: `GET /api/v2/contacts`
- Departments: `GET /api/v2/departments`
- Phone numbers: `GET /api/v2/numbers`

---

## Support

For additional help, refer to the [Dialpad API documentation](https://developers.dialpad.com/reference) or contact Shielva support.
