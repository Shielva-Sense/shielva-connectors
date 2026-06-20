# Basecamp Connector — Setup Guide

Connect Shielva to your Basecamp account to sync projects, to-do lists, to-dos, messages, and documents into your knowledge base.

---

## Prerequisites

- A Basecamp account (Basecamp 4 / bc3)
- Admin access to your Basecamp organization
- A Shielva workspace with the Integrations module enabled

---

## Step 1 — Register a Basecamp OAuth App

1. Go to **https://launchpad.37signals.com/integrations** and sign in with your 37signals / Basecamp credentials.
2. Click **Register one now** (or **New application** if you already have apps).
3. Fill in the form:
   - **Name**: `Shielva Integration` (or any descriptive name)
   - **Company name**: your company
   - **Website URL**: `https://shielva.ai` (or your own domain)
   - **Redirect URI**: the callback URL from your Shielva workspace. Example:
     ```
     https://app.shielva.ai/oauth/basecamp/callback
     ```
4. Click **Register this app**.
5. Note your **Client ID** and **Client Secret** — you will need these in Shielva.

> **User-Agent note**: 37signals requires all API clients to send a `User-Agent` header that identifies the application and provides a contact address. The Shielva Basecamp connector always sends:
> ```
> User-Agent: Shielva (contact@shielva.ai)
> ```
> This is required and cannot be changed.

---

## Step 2 — Discover your Account ID

After the OAuth flow completes, the connector calls:

```
GET https://launchpad.37signals.com/authorization.json
```

This returns a list of all Basecamp accounts accessible with your token. The connector automatically selects the first `bc3` (Basecamp 4) account and stores its `account_id`. All subsequent API calls use:

```
https://3.basecampapi.com/{account_id}/
```

If you have multiple Basecamp accounts and want to sync a specific one, update the `account_id` field in the connector config after installation.

---

## Step 3 — Install in Shielva

1. In Shielva, navigate to **Integrations → Basecamp**.
2. Enter your:
   - **Client ID** — from Step 1
   - **Client Secret** — from Step 1
   - **Redirect URI** — the same URL you registered in Step 1
3. Click **Connect to Basecamp**.
4. You will be redirected to Basecamp to authorize the connection.
5. After authorization, you are redirected back to Shielva. The connector calls `/authorization.json` to validate the token and records your account ID.

---

## Step 4 — Run a Sync

Once installed, trigger a sync from the Shielva integration panel or via the API:

```python
async with BasecampConnector(config={
    "access_token": "<oauth_access_token>",
    "account_id": "<your_account_id>",
}) as conn:
    result = await conn.sync(full=True, kb_id="kb_basecamp_001")
    print(f"Synced {result.documents_synced} documents")
```

### What gets synced

| Resource | Basecamp endpoint |
|----------|------------------|
| Projects | `GET /projects.json` |
| To-do lists | `GET /buckets/{project_id}/todolists.json` |
| To-dos | `GET /buckets/{project_id}/todolists/{list_id}/todos.json` |
| Messages | `GET /buckets/{project_id}/messages.json` |
| Documents | `GET /buckets/{project_id}/vaults/{vault_id}/documents.json` |

---

## Pagination

Basecamp uses RFC 5988 `Link` headers for pagination:

```
Link: <https://3.basecampapi.com/1234567/projects.json?page=2>; rel="next"
```

The connector follows `rel="next"` links automatically until all pages are consumed.

---

## Scopes

Basecamp OAuth 2.0 does not use granular scopes — access is granted to all resources the authenticated user can see. The connector reads data only and never writes to Basecamp.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `auth_error` / 401 | Token expired or revoked | Re-authorize via the Shielva integration panel |
| `auth_error` / 403 | Insufficient permissions | Ensure your Basecamp user has access to the projects you want to sync |
| `rate_limit` / 429 | Too many API requests | The connector retries automatically with exponential backoff; if persistent, reduce sync frequency |
| `resource_missing` / 404 | Project or resource deleted | Re-run full sync to reconcile |
| `Network error` | Transient connectivity issue | The connector retries up to 3 times with backoff |

---

## Security

- Access tokens are stored encrypted in the Shielva vault (AES-256-GCM under the tenant DEK).
- Tokens are never logged or exposed in API responses.
- The connector reads data only — it never creates, updates, or deletes Basecamp resources.
- To revoke access, go to **Basecamp → My profile → Integrations** and remove the Shielva app.
