# Airtable Connector — Setup Guide

## Overview

The Airtable connector syncs all bases, tables, and records from your Airtable workspace into Shielva. It uses the Airtable REST API v0 with Bearer token authentication via a Personal Access Token (PAT).

---

## Step 1 — Create an Airtable Account

If you don't have an Airtable account:

1. Go to [airtable.com](https://airtable.com) and click **Sign up**.
2. Complete registration and create your first workspace.

---

## Step 2 — Generate a Personal Access Token

Airtable uses **Personal Access Tokens (PATs)** — not API keys — for server-to-server integrations. PATs start with the prefix `pat`.

1. Log in to [airtable.com](https://airtable.com).
2. Click your account avatar (top-right) → **Account** or go directly to **Developer Hub**.
3. In the left sidebar under **Developer Hub**, click **Personal access tokens** — or navigate to [airtable.com/create/tokens](https://airtable.com/create/tokens).
4. Click **Create new token**.
5. Enter a descriptive name (e.g. "Shielva Connector").
6. Under **Scopes**, add at minimum:
   - `data.records:read` — read records from all tables
   - `schema.bases:read` — read base and table schemas
7. Under **Access**, choose **All current and future bases** (recommended) or select specific bases you want to sync.
8. Click **Create token**.
9. **Copy the token immediately** — Airtable shows it only once. It will look like `patABCDEFGHIJKLMNOP.xxxxxxxxxxxxxxxxxxxx`.

---

## Step 3 — Find Your Base ID (Optional)

If you want to restrict the connector to a single Airtable base, you can provide the Base ID.

1. Open Airtable and navigate to the base you want to sync.
2. Look at the browser URL. It will look like:
   ```
   https://airtable.com/appXXXXXXXXXXXXXX/tblYYYYYYYYYYYYYY/viwZZZZZZZZZZZZZZ
   ```
3. The Base ID is the part that starts with `app` — for example `appXXXXXXXXXXXXXX`.

Leave the Base ID blank to sync all bases accessible to your token.

---

## Step 4 — Install in Shielva ACP

### Install Fields

| Field | Key | Required | Description |
|-------|-----|----------|-------------|
| **Personal Access Token** | `api_key` | Yes | Airtable PAT (starts with `pat`) |
| **Default Base ID** | `base_id` | No | Restrict sync to a specific base (e.g. `appXXXXXXXXXXXXXX`) |

### Steps

1. In the Shielva ACP, navigate to **Integrations → Add Connector → Airtable**.
2. Paste your **Personal Access Token** in the `api_key` field.
3. Optionally enter a **Base ID** if you want to restrict the sync to one base.
4. Click **Install**. Shielva calls `GET /meta/whoami` to verify the token.
5. On success, the connector status shows **ONLINE**.

---

## Required Scopes

| Scope | Endpoint | Purpose |
|-------|----------|---------|
| `schema.bases:read` | `GET /v0/meta/bases` | List all accessible bases |
| `schema.bases:read` | `GET /v0/meta/bases/{id}/tables` | Read table schemas and views |
| `data.records:read` | `GET /v0/{base_id}/{table_name}` | Read records (paginated) |
| `data.records:read` | `GET /v0/{base_id}/{table_name}/{record_id}` | Read a single record |

---

## How Sync Works

1. Calls `GET /v0/meta/bases` (paginated via `offset`) to list all accessible bases.
2. For each base, calls `GET /v0/meta/bases/{id}/tables` to list tables and their schemas.
3. For each table, calls `GET /v0/{base_id}/{table_name}?pageSize=100` and paginates via the `offset` cursor until exhausted.
4. Each record is normalized into a `ConnectorDocument` with a stable 16-char SHA-256 source ID keyed on `"record:" + record_id`.

---

## Troubleshooting

### 401 Unauthorized

**Cause:** The Personal Access Token is invalid or has been revoked.

**Fix:** Regenerate the token at [airtable.com/create/tokens](https://airtable.com/create/tokens) and update it in the connector install fields.

---

### 403 Forbidden

**Cause:** The token lacks the required scopes or the base is not included in the token's Access list.

**Fix:**
1. Go to [airtable.com/create/tokens](https://airtable.com/create/tokens) and edit the token.
2. Add `data.records:read` and `schema.bases:read` scopes.
3. Under Access, ensure the relevant bases are included (or set to **All current and future bases**).

---

### 422 Invalid Request

**Cause:** A `filterByFormula` or other parameter passed to Airtable is malformed.

**Fix:** Check the formula syntax. Airtable formulas use the same syntax as the Airtable formula editor. Refer to [Airtable formula documentation](https://support.airtable.com/docs/formula-field-reference).

---

### 429 Too Many Requests

**Cause:** Airtable rate limit reached (5 requests per second per token on most plans).

**Fix:** The connector retries automatically with exponential backoff (2s base delay, respects `Retry-After` header). If the error persists during large syncs, contact Airtable about rate limit increases.

---

### Sync returns 0 records

**Cause 1:** The token has no bases in its Access list.

**Fix:** Edit the token at [airtable.com/create/tokens](https://airtable.com/create/tokens) and set Access to **All current and future bases**.

**Cause 2:** All tables are empty.

**Fix:** Add at least one record to a table and re-run the sync.

---

### `ModuleNotFoundError: No module named 'aiohttp'`

**Fix:** Install dependencies:
```bash
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
pip install aiohttp>=3.9.0
```

---

## Token Rotation

Personal Access Tokens do not expire automatically. To rotate:

1. Go to [airtable.com/create/tokens](https://airtable.com/create/tokens).
2. Delete the old token.
3. Create a new token with the same name, scopes, and access settings.
4. Update the `api_key` field in the Shielva ACP connector install fields.
5. Click **Save / Reinstall** to re-validate.
