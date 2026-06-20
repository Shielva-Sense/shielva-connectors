# Bugsnag Connector — Setup Guide

## Overview

This connector syncs your Bugsnag projects, errors (crash reports), and releases
into the Shielva knowledge base using the
[Bugsnag Data Access API v2](https://bugsnagapiv2.docs.apiary.io/).
It uses Personal Auth Token authentication.

---

## Prerequisites

- A Bugsnag account (any plan that includes API access).
- Access to at least one organization on Bugsnag.

---

## Step 1 — Create a Personal Auth Token

1. Log in to [Bugsnag](https://app.bugsnag.com).
2. Click your **avatar** (top-right) → **My account**.
3. Scroll to the **Personal auth token** section.
4. Click **Generate new token** and give it a descriptive name (e.g. `shielva-connector`).
5. **Copy the token immediately** — it is displayed only once.

---

## Step 2 — Find Your Organization Slug

Your organization slug appears in the Bugsnag URL after `/organizations/`:

```
https://app.bugsnag.com/organizations/<your-org-slug>/
```

You can also find it at **Settings → Organization → Slug**.

---

## Step 3 — Configure the Connector in Shielva

| Field | Value |
|---|---|
| **Personal Auth Token** | The token you created in Step 1 |
| **Organization Slug** | Your org slug from Step 2 |

---

## What Gets Synced

| Resource | Bugsnag API endpoint |
|---|---|
| **Projects** | `GET /organizations/{org_slug}/projects` |
| **Errors** | `GET /projects/{project_id}/errors` |
| **Releases** | `GET /projects/{project_id}/releases` |
| **Collaborators** | `GET /organizations/{org_slug}/collaborators` |

Pagination for projects uses per-page offset. Errors use the `X-Next-Page-Link`
response header for subsequent pages. All available pages are consumed exhaustively.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Authentication failed (401)` | Token is invalid or expired | Re-generate the Personal Auth Token |
| `Authentication failed (403)` | Token lacks organization access | Ensure the token belongs to a member of the org |
| `resource 'X' not found (404)` | Wrong organization slug | Double-check the slug in Bugsnag Settings |
| `Rate limited (429)` | Too many API requests | The connector retries automatically with exponential backoff |
| Connection refused / timeout | Network issue | Verify connectivity to `https://api.bugsnag.com` |
