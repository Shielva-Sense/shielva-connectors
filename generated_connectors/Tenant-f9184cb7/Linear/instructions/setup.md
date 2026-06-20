# Linear Connector — Setup Guide

## Overview

The Linear connector integrates Shielva with Linear's GraphQL API to sync issues from all teams into Shielva's knowledge base. It uses a Personal API key as a Bearer token for authentication — no OAuth flow required.

---

## Step 1 — Create a Personal API Key in Linear

1. Log in to your Linear workspace.
2. Click your avatar or workspace name in the bottom-left corner.
3. Navigate to **Settings → Account → API → Personal API keys**.
4. Click **Create key**.
5. Enter a label (e.g. "Shielva Connector") and click **Create key**.
6. Copy the key — it is shown only once.

> The key grants access to all data your account can see. Use a dedicated service account with read-only access if your Linear plan supports it.

---

## Step 2 — Gather your install fields

| Field | Where to find it | Example |
|-------|-----------------|---------|
| **API Key** | Copied in Step 1 from Settings → API → Personal API keys | `lin_api_abc123...` |

---

## Step 3 — Install in Shielva ACP

1. In the Shielva ACP, navigate to **Integrations → Add Connector → Linear**.
2. Paste your **API Key** into the install field.
3. Click **Install**. Shielva calls the Linear GraphQL `viewer` query to verify your credentials.
4. On success, the connector status shows **ONLINE** and displays your Linear username.

---

## Step 4 — Run Your First Sync

Once installed, trigger a full sync from the connector detail page or via the API:

```python
async with LinearConnector(api_key="lin_api_...") as conn:
    result = await conn.sync(full=True, kb_id="kb_engineering")
    print(f"Synced {result.documents_synced} issues from {result.documents_found} found")
```

Shielva will paginate through every team and all their issues automatically.

---

## Required Permissions

| Resource | Permission needed |
|----------|-----------------|
| Issues | Read |
| Teams | Read |
| Projects | Read |
| Users (assignee names) | Read |

A standard Linear member account has read access to all of the above within their workspace. Admin access is not required.

---

## Authentication Details

Linear uses Bearer token authentication for its GraphQL API. The connector sends:

```
POST https://api.linear.app/graphql
Authorization: <your-api-key>
Content-Type: application/json
```

The API key is stored encrypted in the Shielva vault and never logged.

---

## Data Synced

Each Linear issue is normalized to a `ConnectorDocument`:

| Field | Value |
|-------|-------|
| `title` | `[TEAM_KEY] Issue title` |
| `content` | Description + Status + Priority + Assignee + Team |
| `source_url` | `https://linear.app/issue/{id}` |
| `source_id` | `sha256(issue_id)[:16]` — stable 16-char hex |
| `metadata.state` | Issue state name (e.g. "In Progress") |
| `metadata.priority` | Priority integer (0=None, 1=Urgent, 2=High, 3=Medium, 4=Low) |
| `metadata.priority_label` | Human-readable priority string |
| `metadata.assignee` | Assignee name or "Unassigned" |
| `metadata.team_name` | Team name |
| `metadata.team_key` | Team key abbreviation (e.g. "ENG") |
| `metadata.created_at` | ISO 8601 creation timestamp |
| `metadata.updated_at` | ISO 8601 last-updated timestamp |

---

## Pagination

Linear uses cursor-based pagination. The connector handles this automatically:

- Issues are fetched 50 at a time using `pageInfo { hasNextPage endCursor }`.
- Pass `after=endCursor` to `list_issues()` for manual pagination.
- The `sync()` method automatically follows all cursors for every team until exhausted.

---

## Troubleshooting

### Authentication failed (401/UNAUTHORIZED)

**Cause:** Invalid or expired API key.

**Fix:** Generate a new Personal API key in Linear Settings → API → Personal API keys and reinstall the connector.

---

### 429 Rate Limited

**Cause:** Too many requests to the Linear API.

**Fix:** The connector retries automatically with exponential backoff, respecting the `Retry-After` header. Reduce sync frequency if this persists.

---

### Sync returns 0 issues

**Cause:** No teams or issues in the workspace visible to the API key owner.

**Fix:** Verify the Linear account has at least one team with issues. Run `list_teams()` to confirm teams are visible.

---

### `ModuleNotFoundError: No module named 'aiohttp'`

**Fix:**
```bash
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
pip install aiohttp>=3.9.0
```

---

## Running Tests

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/linear_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
python -m pytest tests/ -v
# Expected: 80 passed
```
