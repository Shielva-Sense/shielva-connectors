# Calendly Connector — Setup Guide

## Overview

The Shielva Calendly connector syncs your Calendly event types, scheduled events, and
invitees into Shielva using the Calendly REST API v2. It supports OAuth 2.0 for user
authorization and Personal Access Tokens (PAT) for direct API access.

---

## Step 1 — Create a Calendly OAuth App

1. Log in to [Calendly](https://calendly.com) and go to **Integrations → API & Webhooks**.
2. Click **Developer Portal** (or go to <https://developer.calendly.com>).
3. In the Developer Portal, select **Create New OAuth App**.
4. Fill in the app details:
   - **App Name**: Shielva Integration (or your preferred name)
   - **App Description**: Shielva connector for syncing scheduling data
   - **Redirect URI**: `https://your-shielva-instance/connectors/calendly/callback`
     (use the exact URI your Shielva instance is configured to receive OAuth callbacks on)
5. Note your **Client ID** and **Client Secret** — you will need these when configuring
   the connector in Shielva.

---

## Step 2 — Required OAuth Scopes

When authorizing via OAuth 2.0, the connector requests the following scopes:

| Scope | Purpose |
|---|---|
| `event_type:read` | List event type definitions |
| `scheduled_event:read` | List scheduled meetings |
| `organization:read` | Read organization and membership data |
| `user:read` | Read authenticated user profile |

These scopes are read-only. The connector never creates, modifies, or cancels events.

---

## Step 3 — Configure the Connector in Shielva

In the Shielva ARC connector settings, fill in:

| Field | Description | Required |
|---|---|---|
| **Client ID** | OAuth App Client ID from the Calendly Developer Portal | Yes |
| **Client Secret** | OAuth App Client Secret from the Calendly Developer Portal | Yes |
| **Redirect URI** | OAuth redirect URI — must match the URI registered in step 1 | No |

After saving, click **Authorize** to open the Calendly OAuth consent screen.
Grant access, and Shielva will exchange the authorization code for an access token
automatically.

---

## Alternative — Personal Access Token (PAT)

If you prefer not to use OAuth, you can authenticate with a Personal Access Token:

1. Go to **Integrations → API & Webhooks** in Calendly.
2. Under **Personal Access Tokens**, click **Generate New Token**.
3. Copy the token.
4. In the Shielva connector config, set the `access_token` field to the PAT value.

PATs do not expire unless revoked manually. They carry the same permissions as the
user who created them.

---

## Step 4 — Run a Sync

After authorization:

1. Click **Sync Now** in the Shielva connector panel.
2. The connector will:
   - Fetch your event type catalog
   - Fetch all active scheduled events (paginated)
   - Normalize each record into Shielva's knowledge base format
3. Check the sync report for `documents_found`, `documents_synced`, and
   `documents_failed` counts.

---

## Calendly API Reference

- API v2 base URL: `https://api.calendly.com/`
- Authentication: `Authorization: Bearer <access_token>`
- Rate limits: 100 requests/minute per access token (as of 2024)
- Pagination: responses include a `pagination.next_page_token`; pass it as `page_token`

Key endpoints used:

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/users/me` | Authenticated user profile |
| GET | `/event_types` | List event type definitions |
| GET | `/scheduled_events` | List scheduled meetings |
| GET | `/scheduled_events/{uuid}` | Single scheduled event |
| GET | `/scheduled_events/{uuid}/invitees` | Invitees for an event |
| GET | `/organization_memberships` | Organization member list |

---

## Troubleshooting

### 401 Unauthorized
- The access token has expired or been revoked.
- For OAuth: re-authorize the connector to get a new token.
- For PAT: generate a new Personal Access Token in Calendly.

### 403 Forbidden
- The token lacks the required scopes.
- Re-authorize with the scopes listed in Step 2.

### 429 Too Many Requests
- Calendly's rate limit has been hit.
- The connector retries automatically with exponential backoff (up to 3 attempts),
  honouring the `Retry-After` header.
- If this persists, reduce sync frequency in Shielva settings.

### No events returned
- Your Calendly account may not have any scheduled events in the active status.
- Try syncing with `status=all` or checking your Calendly calendar directly.
- Ensure the authenticated user or organization has events in the requested time range.

### Sync shows 0 documents found
- Confirm the connector is authorized (health check should return `healthy`).
- Confirm the Calendly account has event types or upcoming scheduled events.
