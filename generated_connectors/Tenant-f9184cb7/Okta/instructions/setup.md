# Okta Connector — Setup Guide

## Overview

The Okta connector syncs your Okta identity data — users, groups, applications, and system log events — into the Shielva knowledge base using the Okta REST API v1 with SSWS token authentication.

---

## Step 1: Create an Okta API Token

1. Sign in to your **Okta Admin Console** (e.g. `https://dev-123456-admin.okta.com`).
2. In the top navigation, go to **Security → API**.
3. Select the **Tokens** tab.
4. Click **Create Token**.
5. Give the token a descriptive name (e.g. `Shielva Connector`).
6. Click **Create Token**.
7. **Copy the token value immediately** — it is only shown once.

> The token value begins with `00` and has the format `00XXXXXXXX_XXXXXXXXXXXXXXXX`.

---

## Step 2: Find Your Okta Domain

Your Okta domain is the hostname of your Okta organization, without the `https://` prefix and without `/admin`.

**Examples:**
- Admin console URL: `https://dev-123456-admin.okta.com` → domain is `dev-123456.okta.com`
- Admin console URL: `https://acme.okta.com/admin` → domain is `acme.okta.com`
- Custom domain: `sso.acme.com` → domain is `sso.acme.com`

You can also find it under **Settings → Account → Okta domain** in the Admin Console.

---

## Step 3: Required Permissions

The API token inherits the permissions of the admin user who created it. For full connector functionality, the creating user must have at minimum one of:

### Option A — Read-Only Administrator (recommended for minimal privilege)
Grants read access to users, groups, applications, and system logs.

1. In Admin Console: **Security → Administrators → Add Administrator**.
2. Assign the **Read-Only Administrator** role to the user whose token you will use.

### Option B — Fine-grained scopes (Okta API Scopes)
If your Okta org supports OAuth 2.0 for Okta APIs, the following scopes cover all connector operations:

| Scope | Purpose |
|---|---|
| `okta.users.read` | List and get users |
| `okta.groups.read` | List groups |
| `okta.apps.read` | List applications |
| `okta.logs.read` | Read system log events |

> Note: SSWS tokens do not use OAuth scopes — they use admin role permissions. Scopes apply only when using OAuth 2.0 service apps.

---

## Step 4: Configure the Connector in Shielva

In the Shielva ACP connector setup screen, enter:

| Field | Value |
|---|---|
| **API Token** | The SSWS token you created in Step 1 |
| **Okta Domain** | Your domain from Step 2 (e.g. `dev-123456.okta.com`) |

Click **Connect** to validate credentials and complete setup.

---

## Data Synced

| Resource | Okta Endpoint | Description |
|---|---|---|
| Users | `GET /api/v1/users` | All user profiles, status, login, department |
| Groups | `GET /api/v1/groups` | All groups with name and description |
| Applications | `GET /api/v1/apps` | All app integrations with SSO mode and status |
| System Logs | `GET /api/v1/logs` | Authentication events, admin actions, policy changes |

All resources use cursor-based pagination (`after=` query param + `Link: rel="next"` header) to handle large organizations without hitting API limits.

---

## Rate Limits

Okta enforces per-org rate limits (typically 600–1000 requests/min for read endpoints on developer orgs, higher on production). The connector automatically retries on 429 responses with exponential backoff.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `401 Unauthorized` | Invalid or expired API token | Regenerate the token in Okta Admin Console → Security → API → Tokens |
| `403 Forbidden` | Token user lacks required admin role | Assign Read-Only Administrator role to the token owner |
| `Connection timed out` | Incorrect domain | Verify domain matches your Okta org URL exactly |
| Empty user list | Org has no active users in the default filter | Check Okta Admin Console → Directory → People |

---

## Security Notes

- The API token is stored encrypted (AES-256-GCM) in the Shielva credential store.
- The token is sent over HTTPS only, in the `Authorization: SSWS {token}` header.
- Rotate the token periodically (Okta recommends 90-day rotation). Update it in the connector settings after rotation.
- Revoking the token in Okta immediately invalidates the connector — recreate and update if you need to revoke.
