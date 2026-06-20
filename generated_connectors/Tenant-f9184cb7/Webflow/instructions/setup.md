# Webflow Connector — Setup Guide

## Overview

This connector integrates with the **Webflow REST API v2** using **OAuth 2.0 Authorization Code** flow. It syncs your Webflow sites, CMS collections and items, and pages into Shielva.

---

## Step 1 — Register a Webflow App

1. Log into your [Webflow Dashboard](https://webflow.com/dashboard).
2. Click your **workspace name** (top-left) → **Account Settings**.
3. In the left navigation select **Integrations** → **Apps** → **Register an App**.
4. Fill in:
   - **App name**: e.g. `Shielva Integration`
   - **Homepage URL**: your Shielva instance URL
   - **Redirect URI**: the callback URL Shielva provides (e.g. `https://your-shielva-instance.com/connectors/oauth/callback`). This value must match exactly what you enter in the connector `redirect_uri` field.

5. Click **Create App**. You will receive a **Client ID** and **Client Secret** — copy both immediately as the secret is only shown once.

---

## Step 2 — Configure OAuth Scopes

When configuring the app in the Webflow dashboard, select the following scopes:

| Scope | Purpose |
|---|---|
| `sites:read` | List and read site metadata |
| `cms:read` | Read CMS collections and items |
| `pages:read` | Read site pages |
| `forms:read` | Read site forms (optional) |

> **Note:** Webflow's OAuth scopes are workspace-level. The token will have access to all sites in the authorized workspace that the user can access.

---

## Step 3 — Webflow Designer API vs Data API

Webflow exposes two distinct APIs:

| API | Usage |
|---|---|
| **Designer Extensions API** | Runs inside the Webflow Designer for real-time canvas manipulation. Requires a Designer Extension app type, not an OAuth data app. |
| **Data API (REST v2)** | Server-side integration — used by this connector. Operates on site data, CMS, pages, and forms. Requires an OAuth App registration as described above. |

**This connector uses the Data API only.** Do not use a Designer Extension client ID here.

---

## Step 4 — Install the Connector

In the Shielva connector setup UI, enter:

| Field | Value |
|---|---|
| **Client ID** | The Client ID from Step 1 |
| **Client Secret** | The Client Secret from Step 1 |
| **Redirect URI** | The callback URL registered in Step 1 (optional — omit if the Webflow app has only one redirect URI) |

---

## Step 5 — Authorize

After installation, click **Authorize with Webflow**. You will be redirected to:

```
https://webflow.com/oauth/authorize?response_type=code&client_id=<YOUR_CLIENT_ID>&scope=sites:read+cms:read+pages:read+forms:read&redirect_uri=<YOUR_REDIRECT_URI>
```

Log into Webflow (if not already) and approve the requested permissions. Webflow will redirect back to Shielva with an authorization code, which is automatically exchanged for an access token.

---

## Step 6 — Verify & Sync

Use the **Health Check** button to verify the token is valid. The check calls `GET /token/introspect` and reports the authenticated user and the number of authorized sites.

Run **Sync** to pull all data:
- All accessible **sites** (metadata)
- All **CMS collections** per site (field schema)
- All **CMS items** per collection (offset-paginated, 100 per page)
- All **pages** per site

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` | Access token expired or revoked | Re-authorize via the connector UI |
| `403 Forbidden` | App missing required scope | Re-register the app with the correct scopes |
| `404 Not Found` on a specific site | Site was deleted or the token has no access | Check workspace membership in Webflow |
| Health check shows 0 sites | Token is valid but no sites in the authorized workspace | Add at least one site to the workspace |

---

## Security Notes

- The access token is stored encrypted at rest using the Shielva vault (`AES-256-GCM`, per-tenant key derivation).
- The client secret is stored as a `password` field type and is never returned in API responses after initial save.
- OAuth tokens should be rotated if workspace admin credentials change.
