# Intercom Connector — Setup Guide

## Overview

The Intercom connector syncs contacts (leads and users), conversations, and companies from your Intercom account into Shielva. It uses the Intercom REST API v2.10 with Bearer token authentication.

---

## Step 1 — Create an Intercom Access Token

1. Log in to your Intercom account at [app.intercom.com](https://app.intercom.com).
2. Click the **Settings** gear icon (bottom-left).
3. Go to **Settings → Integrations → Developer Hub** (your Developer Hub URL is `app.intercom.com/a/apps/<workspace_id>/developer-hub`).
4. Click **New App** to create a workspace app (recommended), or select an existing one.
   - **Workspace app vs. public app**: Use a workspace app for internal integrations — it gives you a permanent access token tied to your workspace with no OAuth redirect needed. Public apps use OAuth and are intended for distributing your integration to other workspaces.
5. Under the app, go to the **Authentication** tab.
6. Copy the **Access Token** shown. This token grants API access on behalf of your workspace admin.

> If you are on a strict plan, you may need to create a Developer App first under **Settings → Integrations → Developer Hub → New App**.

---

## Step 2 — Gather your install fields

| Field | Where to find it | Example |
|-------|-----------------|---------|
| **Access Token** | Copied in Step 1 from Developer Hub | `dG9rOmZhNm...` |

That is the only field required. The token carries your workspace identity and permissions.

---

## Step 3 — Install in Shielva ACP

1. In the Shielva ACP, navigate to **Integrations → Add Connector → Intercom**.
2. Paste the **Access Token**.
3. Click **Install**. Shielva calls `GET /me` to verify credentials.
4. On success, the connector status shows **ONLINE** with your admin name.

---

## Required Permissions

The Intercom access token must have access to the following scopes (all are read-only):

| Permission scope | Used for |
|-----------------|----------|
| `contacts:read` | List and retrieve contacts (leads and users) |
| `conversations:read` | List and retrieve conversation threads |
| `companies:read` | List companies |
| `admins:read` | List workspace admins |

Standard workspace access tokens granted from the Developer Hub include all of these by default. If you created a public app, verify these scopes are checked in the **Permissions** tab.

**Rate limits**: Intercom enforces 167 requests per 10 seconds per workspace. The connector retries automatically with exponential backoff (up to 3 retries, max 30 s delay) and respects the `X-RateLimit-Reset` header.

---

## What Gets Synced

| Resource | Intercom Endpoint | Notes |
|----------|-------------------|-------|
| Contacts (leads + users) | `GET /contacts` | Cursor-paginated; 150 per page |
| Single contact | `GET /contacts/{id}` | Full contact detail |
| Contact search | `POST /contacts/search` | Field/operator/value query |
| Conversations | `GET /conversations` | Cursor-paginated; 20 per page |
| Single conversation | `GET /conversations/{id}` | With all reply parts |
| Companies | `GET /companies` | Full company list; 150 per page |
| Admins | `GET /admins` | All workspace admins |
| Tags | `GET /tags` | All workspace tags |
| Segments | `GET /segments` | All workspace segments |

The sync engine iterates all contacts and conversations, normalizing each into a `ConnectorDocument` with a stable 16-char source ID computed as `sha256("contact:" + id)[:16]` for contacts and `sha256("conversation:" + id)[:16]` for conversations. Cursor-based pagination (`pages.next.starting_after`) is followed automatically until all pages are exhausted.

---

## Rotating Your Token

If you need to rotate the access token:

1. Go to **Settings → Integrations → Developer Hub → Your App → Authentication**.
2. Regenerate or copy a new token.
3. In Shielva ACP, open the Intercom connector settings and update the **Access Token** field.
4. Click **Save**. The next health check will confirm the new token works.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Status OFFLINE after install | Token invalid or expired | Regenerate token in Developer Hub |
| 403 Forbidden | Token lacks workspace access | Use a full workspace token, not a restricted one |
| 429 Rate Limited | Too many requests | The connector retries automatically with backoff |
| Contacts missing | Filter or pagination issue | Run a full sync from the ACP |

---

## API Rate Limits

Intercom enforces **167 requests per 10 seconds** per workspace (as of API v2.10). The connector handles 429 responses automatically using exponential backoff (up to 3 retries, max 30 s delay). The `X-RateLimit-Reset` header is respected when present to wait the correct duration before retrying.

---

## Security Notes

- The access token is stored encrypted in the Shielva vault and never logged.
- All requests are made over HTTPS to `https://api.intercom.io`.
- The token is never written to disk or included in any log output.
- Rotate your token immediately if you suspect it has been compromised.
