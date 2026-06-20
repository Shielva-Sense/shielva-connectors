# Front Connector — Setup Guide

## Overview

The Front connector syncs your team's shared inbox data — conversations, contacts,
teammates, inboxes, and tags — into Shielva using the Front Core API v1 and a
Bearer API token.

---

## Step 1 — Generate a Front API Token

1. Sign in to your Front account at [app.frontapp.com](https://app.frontapp.com).
2. Click your **avatar** (bottom-left) → **Settings**.
3. Under **Developers**, click **API Tokens**.
4. Click **Generate new token**.
5. Give the token a descriptive name (e.g. `Shielva Integration`).
6. Select the required **scopes** (see below).
7. Click **Create token**.
8. **Copy the token immediately** — it is only shown once.

---

## Required Scopes

| Scope | Purpose |
|-------|---------|
| `conversations:read` | Read conversation threads |
| `contacts:read` | Read contact records |
| `teammates:read` | Read teammate profiles |
| `inboxes:read` | Read inbox configurations |
| `tags:read` | Read tag definitions |
| `messages:read` | Read message bodies within conversations |

> **Minimum scopes:** `conversations:read` + `contacts:read` are required for
> sync. All other scopes are needed for the full feature set.

---

## Step 2 — Install the Connector in Shielva

1. In the Shielva Admin Console, go to **Connectors → Add Connector**.
2. Search for **Front** and click **Install**.
3. Paste the API token from Step 1 into the **API Token** field.
4. Click **Connect**. Shielva calls `GET /me` to verify the token.
5. If the health check passes, the connector status shows **Connected**.

---

## Token Format

Front API tokens are long opaque strings — typically 64–128 hexadecimal
characters. They are sent in every request as:

```
Authorization: Bearer <your_api_token>
```

Shielva stores the token encrypted (AES-256-GCM) and never logs its value.

---

## Rate Limits

The Front Core API v1 enforces a global rate limit of **50 requests per
second** per API token. The connector handles `429 Too Many Requests`
responses automatically with exponential backoff (up to 3 retries, max 30 s
delay). For large workspaces with high conversation volume, syncs may take
several minutes to complete.

---

## Pagination

Front uses cursor-based pagination via `_pagination.next` in every list
response. The connector follows all pages automatically — you do not need to
configure page size.

---

## Data Synced

| Resource | Front endpoint | Notes |
|----------|----------------|-------|
| Conversations | `GET /conversations` | Subject, status, assignee, tags, last message |
| Contacts | `GET /contacts` | Name, email, phone, groups |
| Teammates | `GET /teammates` | Name, email, admin status |
| Inboxes | `GET /inboxes` | Name, address |
| Tags | `GET /tags` | Name, highlight color |
| Messages | `GET /conversations/{id}/messages` | Synced on-demand per conversation |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Authentication failed (401)` | Token is invalid or expired | Regenerate the token in Front and re-install |
| `Authentication failed (403)` | Token lacks required scopes | Regenerate with all required scopes |
| `resource 'X' not found (404)` | Conversation/contact was deleted | Safe to ignore — sync continues |
| Sync is very slow | Large workspace + rate limits | Expect multi-minute syncs; do not re-trigger |
| Network timeout | Front API outage | Check [status.frontapp.com](https://status.frontapp.com); retry will occur automatically |

---

## Security Notes

- Never commit your API token to source control.
- Rotate the token in Front Settings if you suspect it has been exposed.
- The Shielva connector stores the token encrypted at rest.
- Set `token_expiry` in Front if you want automatic token rotation.
