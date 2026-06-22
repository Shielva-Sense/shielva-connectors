# Sentry Connector — Setup Guide

## Overview

This connector syncs your Sentry issues, projects, releases, and issue events
into the Shielva knowledge base. It uses the [Sentry REST API v0](https://docs.sentry.io/api/)
with Bearer token authentication.

---

## Prerequisites

- A Sentry account (Cloud at [sentry.io](https://sentry.io) or self-hosted).
- At least **Member** access on the organization you want to sync.

---

## Step 1 — Create a Sentry Auth Token

1. Log in to Sentry and click your **profile avatar** (top-right corner).
2. Go to **Settings → Account → API → Auth Tokens**.
3. Click **Create New Token**.
4. Under **Scopes**, enable the following (minimum required):
   - `project:read`
   - `org:read`
   - `event:read` (covers issues and events)
   - `releases` (or `release:read` depending on your Sentry version)
5. Give the token a descriptive name (e.g. `shielva-connector`).
6. Click **Create Token** and **copy the token immediately** — it is shown only once.

---

## Step 2 — Find Your Organization Slug

Your organization slug appears in the Sentry URL after `/organizations/`:

```
https://sentry.io/organizations/<your-org-slug>/
```

You can also find it at **Settings → General Settings → Organization Slug**.

---

## Step 3 — Configure the Connector in Shielva

| Field | Value |
|---|---|
| **Auth Token** | The token you created in Step 1 |
| **Organization Slug** | Your org slug from Step 2 |
| **Sentry URL** | Leave blank for Sentry Cloud (`https://sentry.io`). For self-hosted, enter your instance URL (e.g. `https://sentry.mycompany.com`) |

---

## Self-Hosted Sentry

If you run Sentry on your own infrastructure:

1. Set **Sentry URL** to your instance's base URL, e.g. `https://sentry.mycompany.com`.
2. Ensure the Shielva connector service can reach that URL (firewall / VPN).
3. All other steps are identical to Sentry Cloud.

---

## What Gets Synced

| Resource | Sentry API endpoint |
|---|---|
| **Projects** | `GET /api/0/organizations/{slug}/projects/` |
| **Issues** | `GET /api/0/organizations/{slug}/issues/` |
| **Releases** | `GET /api/0/organizations/{slug}/releases/` |
| **Issue Events** | `GET /api/0/issues/{id}/events/` |

Pagination uses Sentry's Link-header cursor pattern. All pages are consumed
exhaustively on each sync run.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Authentication failed (401)` | Token is invalid or expired | Re-create the auth token |
| `Authentication failed (403)` | Token lacks required scopes | Add `project:read`, `org:read`, `event:read` scopes |
| `resource 'X' not found (404)` | Wrong organization slug | Double-check the slug in Sentry Settings |
| `Rate limited (429)` | Too many API requests | The connector retries automatically with backoff |
| Connection refused | Self-hosted URL wrong or unreachable | Verify the URL and network access |
