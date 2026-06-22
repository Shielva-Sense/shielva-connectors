# FullStory Connector — Setup Guide

## Overview

The FullStory connector integrates with the FullStory REST API v2 to sync session recordings, users, and user segments into Shielva. It also provides direct access to custom events per user.

---

## Prerequisites

- An active FullStory account with at least **Admin** access
- API access enabled on your FullStory plan (available on Pro and Enterprise plans)

---

## Step 1: Obtain your API Key

1. Log in to your FullStory account at [https://app.fullstory.com](https://app.fullstory.com)
2. Click your organization name in the top-left corner → **Settings**
3. Navigate to **Integrations** → **API Keys**
4. Click **Create API Key**
5. Give it a name (e.g., "Shielva Connector") and copy the generated key

> The API key is shown only once. Store it securely before closing the dialog.

---

## Step 2: Install the Connector

In the Shielva connector setup form:

| Field | Value |
|-------|-------|
| **API Key** | Bearer token from Step 1 |

The connector will validate credentials by calling `GET /v2/org` and return your organization name on success.

---

## API Details

- **Base URL**: `https://api.fullstory.com`
- **API Version**: v2
- **Authentication**: `Authorization: Bearer {api_key}`
- **Content-Type**: `application/json`

### Endpoints used

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v2/org` | Organization info (health check) |
| GET | `/v2/sessions` | Session recordings list |
| GET | `/v2/sessions/{id}` | Single session recording |
| GET | `/v2/users` | Users list |
| GET | `/v2/users/{uid}` | Single user |
| GET | `/v2/segments` | User segments |
| GET | `/v2/events?uid={uid}` | Custom events for a user |

---

## Pagination

FullStory uses cursor-based pagination with a `nextPageToken` field in responses. Pass `pageToken=<token>` in query params to fetch subsequent pages. The connector handles this automatically via the `cursor` parameter.

---

## Rate Limits

FullStory enforces per-organization rate limits on REST API v2 calls:

- Requests returning `429 Too Many Requests` are automatically retried with exponential backoff
- Backoff starts at 1 second, doubles per attempt (max 30 seconds), with jitter
- Auth errors (`401/403`) are never retried — fix the API key instead

---

## Data Synced

| Resource | Description |
|----------|-------------|
| **Users** | Identified users and their properties |
| **Segments** | User segments defined in your FullStory project |
| **Sessions** | Session recordings (available via `list_sessions(uid=...)`) |
| **Events** | Custom events per user (available via `list_events(uid=...)`) |

> Sessions and events are not synced in bulk (too many per organization). Use the targeted methods `list_sessions(uid=...)` and `list_events(uid=...)` for per-user retrieval.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Authentication failed (401)` | Invalid or expired API key | Re-generate and paste the correct API key |
| `Authentication failed (403)` | API access not enabled on plan | Upgrade to a FullStory plan with API access |
| `Rate limited (429)` | Too many requests | The connector retries automatically; reduce sync frequency if persistent |
| `Connection error` | Network/firewall | Ensure egress to `api.fullstory.com:443` is allowed |
| `404 Not Found` | Resource does not exist | Verify the session ID or user UID is correct |

---

## Further Reading

- [FullStory REST API v2 Reference](https://developer.fullstory.com/)
- [FullStory API Authentication](https://developer.fullstory.com/#section/Authentication)
- [FullStory Sessions API](https://developer.fullstory.com/#tag/Sessions)
- [FullStory Users API](https://developer.fullstory.com/#tag/Users)
