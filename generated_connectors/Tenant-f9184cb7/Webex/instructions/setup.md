# Webex Connector — Setup Guide

## Overview

The Webex connector syncs Cisco Webex rooms, meetings, messages, and people from your Webex organization into the Shielva knowledge base using the Webex REST API with OAuth 2.0 authentication.

---

## Prerequisites

You need a **Cisco Webex account** (free or paid) and access to the [Webex Developer Portal](https://developer.webex.com).

---

## Step 1 — Create a Webex Integration (OAuth App)

1. Go to [developer.webex.com](https://developer.webex.com) and sign in.
2. Click **Start Building Apps** → **Create a New App**.
3. Select **Integration** as the app type.
4. Fill in:
   - **Integration Name**: e.g., "Shielva Connector"
   - **Description**: Brief description of the integration
   - **Redirect URI(s)**: Your Shielva OAuth callback URL
5. Under **Scopes**, select the required permissions (see Step 2).
6. Click **Add Integration**.
7. Note your **Client ID** and **Client Secret** from the app credentials page.

---

## Step 2 — Configure OAuth Scopes

In your Webex Integration settings, ensure the following scopes are enabled:

| Scope | Purpose |
|---|---|
| `spark:all` | Full access to Webex APIs |
| `spark:messages_read` | Read messages in rooms |
| `spark:rooms_read` | Read room/space information |
| `spark:memberships_read` | Read room memberships |
| `meeting:schedules_read` | Read meeting schedules |

---

## Step 3 — Set the Redirect URI

Under **Redirect URI(s)**, add your Shielva OAuth callback URL:

```
https://your-shielva-domain.com/oauth/callback
```

This must match exactly what you provide in the `redirect_uri` install field.

---

## Step 4 — Configure the Connector in Shielva

In the Shielva connector install form, fill in:

| Field | Key | Required | Description |
|---|---|---|---|
| Client ID | `client_id` | Yes | From Webex Integration Credentials |
| Client Secret | `client_secret` | Yes | From Webex Integration Credentials |
| Redirect URI | `redirect_uri` | No | Your OAuth callback URL |

---

## Step 5 — Complete the OAuth Flow

After installing the connector:

1. Shielva will redirect you to the Webex consent screen.
2. Log in to Webex and authorize the requested scopes.
3. Webex redirects back to Shielva with an authorization code.
4. Shielva exchanges the code for an `access_token` and `refresh_token`.
5. Tokens are stored as `access_token`, `refresh_token`, and `token_expires_at` in your connector config.
6. The connector is now authorized and ready to sync.

---

## What the Connector Syncs

| Resource | Endpoint | Properties Synced |
|---|---|---|
| Rooms / Spaces | `GET /rooms` | id, title, type, created, lastActivity, isLocked, teamId |
| Meetings | `GET /meetings` | id, title, start, end, timezone, meetingType, status, hostEmail, webLink |
| Messages | `GET /messages` | id, roomId, text, personEmail, created, roomType (via list_messages) |
| People | `GET /people` | id, displayName, emails (via list_people) |

---

## Stable Document IDs

Each document ingested into the knowledge base uses a stable ID computed as:

```
SHA-256("room:" + room_id)[:16]    → for rooms
SHA-256("meeting:" + meeting_id)[:16]  → for meetings
SHA-256("message:" + message_id)[:16]  → for messages
```

This ensures idempotent syncs — the same resource always produces the same document ID regardless of how many times it is synced.

---

## Pagination

The connector uses Webex cursor-based pagination via the `cursor` / `nextCursor` field. All list endpoints are fully paginated — no records are missed even for large organizations.

---

## Troubleshooting

### 401 Unauthorized

- The `access_token` has expired or been revoked.
- Re-authorize via the OAuth flow to obtain a fresh token.

### 403 Forbidden — Missing Scope

- The Webex Integration is missing one or more required scopes.
- Edit your Integration in the Webex Developer Portal, add the missing scopes, and re-authorize.

### 429 Too Many Requests

- Webex API rate limits have been hit.
- The connector retries automatically with exponential backoff (up to 3 attempts) honouring the `Retry-After` header.

### Connector Health is DEGRADED

- Transient network errors have tripped the circuit breaker (5 failures threshold).
- Resolve the underlying issue and trigger a health check to reset the breaker.

---

## API Reference

- **Base URL**: `https://webexapis.com/v1`
- **Auth URL**: `https://webexapis.com/v1/authorize`
- **Token URL**: `https://webexapis.com/v1/access_token`
- Me (health probe): `GET /people/me`
- Rooms: `GET /rooms`, `GET /rooms/{room_id}`
- Messages: `GET /messages?roomId={room_id}`
- Meetings: `GET /meetings`
- People: `GET /people`
- Memberships: `GET /memberships`

---

## Support

For additional help, refer to the [Webex API documentation](https://developer.webex.com/docs/api/getting-started) or contact Shielva support.
