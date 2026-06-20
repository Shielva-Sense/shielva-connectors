# Zoom Connector — Setup Guide

## Overview

The Zoom connector syncs users, meetings, cloud recordings, and webinars from your Zoom account into the Shielva knowledge base using the Zoom REST API v2.

Authentication uses **Server-to-Server OAuth** (account credentials grant). No user redirect or browser consent screen is needed — Shielva exchanges your Account ID and OAuth app credentials for a Bearer token automatically.

---

## Prerequisites

- A **Zoom account** (Pro, Business, or Enterprise) — free accounts have limited API access and no cloud recordings.
- Admin access to the [Zoom Marketplace](https://marketplace.zoom.us/) to create a Server-to-Server OAuth app.

---

## Step 1 — Create a Server-to-Server OAuth App

1. Go to [marketplace.zoom.us](https://marketplace.zoom.us/) and sign in with an admin account.
2. Click **Develop** → **Build App**.
3. Select **Server-to-Server OAuth** and click **Create**.
4. Give the app a name, for example: `Shielva Connector`.
5. Click **Continue**.

---

## Step 2 — Note Your Credentials

Under **App Credentials**, copy:

| Field | Where to find it |
|---|---|
| **Account ID** | App Credentials page → Account ID |
| **Client ID** | App Credentials page → Client ID |
| **Client Secret** | App Credentials page → Client Secret (click Show) |

These three values go into the Shielva connector install form.

---

## Step 3 — Add Required Scopes

In your Zoom App settings, go to **Scopes** and add the following:

| Scope | Purpose |
|---|---|
| `user:read:admin` | List and read users in the account |
| `meeting:read:admin` | Read meeting information |
| `recording:read:admin` | Read cloud recording information |

Click **Continue** and then **Activate** your app.

---

## Step 4 — Find Your Account ID

Your Account ID is shown on the App Credentials page. It can also be found at:

- **Zoom Web Portal** → Account Profile → Account ID (bottom of the page)

---

## Step 5 — Approve the App (Enterprise accounts)

For Enterprise accounts with an App Marketplace approval workflow:

1. Go to **Manage** → **Apps** in the Zoom Admin portal.
2. Find your Server-to-Server OAuth app and click **Approve**.

---

## Step 6 — Configure the Connector in Shielva

In the Shielva connector install form, fill in:

| Field | Key | Required | Description |
|---|---|---|---|
| Account ID | `account_id` | Yes | Your Zoom Account ID |
| Client ID | `client_id` | Yes | From Zoom App Credentials |
| Client Secret | `client_secret` | Yes | From Zoom App Credentials |

Shielva will immediately exchange these credentials for a Bearer token and probe `GET /accounts/me` to verify connectivity.

---

## How Authentication Works

Server-to-Server OAuth works as follows:

1. Shielva sends a `POST` to `https://zoom.us/oauth/token?grant_type=account_credentials&account_id={account_id}` with HTTP Basic auth (`client_id:client_secret`).
2. Zoom returns `{"access_token": "...", "expires_in": 3600}`.
3. Shielva uses the token as `Authorization: Bearer {access_token}` on all API calls.
4. Tokens expire after 1 hour. Shielva refreshes automatically, no action required.

---

## What the Connector Syncs

| Resource | Endpoint | Properties Synced |
|---|---|---|
| Users | `GET /users?status=active` | id, email, name, type, status, timezone, department |
| Meetings | `GET /users/{user_id}/meetings?type=scheduled` | id, topic, status, start_time, duration, timezone, host, join_url |
| Recordings | `GET /users/{user_id}/recordings` | id, uuid, topic, start_time, duration, host, share_url, recording_count, total_size |
| Webinars | `GET /users/{user_id}/webinars` | id, topic, start_time, duration, host |

---

## Pagination

The connector uses cursor-based pagination with `next_page_token`. Default page size is 300 records per page.

```
GET /users?page_size=300
→ {"users": [...], "next_page_token": "abc123"}
GET /users?page_size=300&next_page_token=abc123
→ {"users": [...], "next_page_token": ""}  ← no more pages
```

---

## Stable Document IDs

Each document ingested into the knowledge base uses a stable ID computed as:

```
SHA-256("meeting:" + meeting_id)[:16]
```

This ensures idempotent syncs — the same meeting always produces the same document ID regardless of how many times it is synced.

---

## Troubleshooting

### 401 Unauthorized on Token Exchange

- Verify the Client ID and Client Secret are correct and that the app has not been deactivated in Zoom Marketplace.
- Ensure the Account ID matches the account where the app is installed.

### 403 Forbidden — Missing Scope

- The Server-to-Server OAuth app is missing one or more required scopes.
- Edit the app in Zoom Marketplace → Scopes, add `user:read:admin`, `meeting:read:admin`, `recording:read:admin`, and reactivate the app.

### 429 Too Many Requests

- Zoom API rate limits have been hit (typically 10 requests/second per user).
- The connector retries automatically with exponential backoff (up to 3 attempts).
- For large accounts, schedule syncs during off-peak hours.

### Connector Health is DEGRADED

- Transient network errors have tripped the circuit breaker (5 consecutive failures).
- Resolve the underlying network or credential issue and trigger a health check to reset.

### Cloud Recordings Not Appearing

- Cloud recording requires a **Pro, Business, or Enterprise** Zoom plan.
- Ensure the `recording:read:admin` scope is granted.
- Free plan accounts do not support cloud recordings via API.

---

## API Reference

- **Token URL**: `https://zoom.us/oauth/token` (grant_type=account_credentials)
- **Base URL**: `https://api.zoom.us/v2`
- Users: `GET /users?status=active&page_size=300`
- Meetings: `GET /users/{user_id}/meetings?type=scheduled&page_size=300`
- Past meetings: `GET /users/{user_id}/meetings?type=previous_meetings&page_size=300`
- Recordings: `GET /users/{user_id}/recordings?from=2024-01-01&to=2024-12-31&page_size=300`
- Webinars: `GET /users/{user_id}/webinars?page_size=300`
- Account info: `GET /accounts/me` (used for health check)

---

## Support

For additional help, refer to the [Zoom API documentation](https://developers.zoom.us/docs/api/) or contact Shielva support.
