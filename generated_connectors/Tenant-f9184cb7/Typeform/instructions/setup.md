# Typeform Connector — Setup Guide

## Overview

The Typeform connector syncs form definitions and responses from your Typeform account into Shielva. It uses the Typeform API v1 with OAuth 2.0 authentication. You can also use a Personal Access Token (PAT) as a simpler alternative for testing.

---

## Authentication options

### Option A — OAuth 2.0 (recommended for production)

#### Step 1 — Create a Typeform OAuth Application

1. Log in to your Typeform account at [https://admin.typeform.com](https://admin.typeform.com).
2. Click your profile icon (top-right) → **Settings** → **Developer apps**.
3. Click **Register a new application**.
4. Fill in:
   - **Application name:** e.g. "Shielva Integration"
   - **Application website:** your company URL
   - **Redirect URI:** the callback URL for your Shielva instance (e.g. `https://app.shielva.ai/connectors/typeform/callback`)
5. Click **Register application**.
6. Copy the **Client ID** and **Client Secret** — you will need these for the install step.

#### Step 2 — Configure OAuth Scopes

Ensure your application requests these scopes when authorizing:

| Scope | Purpose |
|-------|---------|
| `forms:read` | Read form definitions |
| `responses:read` | Read form responses |
| `workspaces:read` | Read workspace metadata |

#### Step 3 — Install in Shielva ACP

1. In the Shielva ACP, navigate to **Integrations → Add Connector → Typeform**.
2. Enter your **Client ID** and **Client Secret**.
3. Optionally enter a **Redirect URI** (if different from your registered default).
4. Click **Install**. Shielva validates that credentials are present.
5. Click **Authorize** to begin the OAuth flow — you will be redirected to Typeform to grant access.
6. After authorization, Typeform redirects back to Shielva with an authorization code, which is exchanged for an `access_token`.
7. Once the access token is stored, the connector status shows **ONLINE**.

---

### Option B — Personal Access Token (for testing/development)

1. Log in to your Typeform account.
2. Go to **My Account → Personal tokens** (URL: `https://admin.typeform.com/account#/section/tokens`).
3. Click **Generate a new token**, give it a name, click **Generate token**.
4. Copy the token — it is shown only once.
5. Store it as `access_token` in the connector config.

---

## Install fields

| Field | Key | Required | Description |
|-------|-----|----------|-------------|
| **Client ID** | `client_id` | Yes | Typeform OAuth App Client ID |
| **Client Secret** | `client_secret` | Yes (password) | Typeform OAuth App Client Secret |
| **Redirect URI** | `redirect_uri` | No | OAuth callback URI registered in Typeform |

Config also stores `access_token` after the OAuth flow completes.

---

## API endpoints used

| Resource | Endpoint | Description |
|----------|----------|-------------|
| Account info | `GET /me` | Verify token + get alias/email |
| Forms list | `GET /forms` | Page-based list of all forms |
| Form detail | `GET /forms/{id}` | Single form definition |
| Responses | `GET /forms/{id}/responses` | Cursor-paginated responses |
| Workspaces list | `GET /workspaces` | Page-based list of workspaces |
| Workspace detail | `GET /workspaces/{id}` | Single workspace |
| Insights | `GET /insights/{form_id}/summary` | Form insights summary |

---

## Pagination behaviour

- **Forms and workspaces** use page-based pagination (`?page=N&page_size=M`). The connector reads `page_count` from the response to determine whether more pages exist.
- **Responses** use cursor-based pagination via the `before` parameter. The connector passes the `token` of the last response on each page as `before` on the next request.

---

## Data synchronized

Each sync run produces two categories of `ConnectorDocument`:

| Document type | `metadata.type` | `source_id` derivation |
|---------------|-----------------|------------------------|
| Form definition | `form` | `SHA-256("form:" + form_id)[:16]` |
| Form response | `form_response` | `SHA-256("response:" + response_token)[:16]` |

---

## Troubleshooting

### 401 Unauthorized

**Cause:** The access token is invalid or has been revoked.

**Fix:**
- Re-run the OAuth flow to obtain a fresh access token.
- If using a PAT, generate a new one at `https://admin.typeform.com/account#/section/tokens`.

---

### 403 Forbidden

**Cause:** The token does not have the required scopes or lacks access to the resource.

**Fix:**
- Ensure `forms:read`, `responses:read`, and `workspaces:read` scopes were granted during OAuth.
- Collaborator access may not expose all form data depending on Typeform plan.

---

### 404 Not Found

**Cause:** The form, workspace, or insights resource does not exist or has been deleted.

**Fix:**
- Verify the resource exists in your Typeform account.
- Deleted forms cannot be recovered.

---

### 429 Too Many Requests

**Cause:** Typeform enforces rate limits (varies by plan).

**Fix:**
- The connector automatically retries with exponential backoff (up to 3 attempts, starting at 1 s).
- If 429s persist during large syncs, reduce sync frequency or upgrade your Typeform plan.

---

### Sync returns 0 documents

**Cause:** No forms found, or forms have no responses yet.

**Fix:**
- Confirm forms exist at `https://admin.typeform.com/forms`.
- Ensure at least one response has been submitted.
- Verify the token/OAuth grant covers the correct Typeform workspace.

---

### `ModuleNotFoundError: No module named 'aiohttp'`

**Fix:** Install dependencies:
```bash
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
pip install aiohttp>=3.9.0
```
