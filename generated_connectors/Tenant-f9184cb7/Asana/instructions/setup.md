# Asana Connector — Setup Guide

## Prerequisites

You need an Asana account. The connector uses a Personal Access Token (PAT) — no OAuth required.

---

## Step 1 — Generate a Personal Access Token

1. Log in to [https://app.asana.com](https://app.asana.com).
2. Click your **profile avatar** (top-right corner).
3. Select **My Profile Settings**.
4. Go to the **Apps** tab.
5. Click **Manage Developer Apps**.
6. Click **New Access Token**.
7. Name the token (e.g. `Shielva Integration`) and click **Create**.
8. Copy the token immediately — it is shown only once.

The token is a long alphanumeric string starting with `1/`.

---

## Step 2 — Find your Workspace GID (optional)

The Workspace GID scopes the connector to a single workspace/organization. If left blank, all workspaces the token owner belongs to are synced.

**From the Asana URL:**
- Navigate to any project in the target workspace.
- The URL is: `https://app.asana.com/0/{workspace_gid}/{project_gid}/...`
- The first large number after `/0/` is your workspace GID.

**From the API:**
```bash
curl -H "Authorization: Bearer YOUR_PAT" \
     "https://app.asana.com/api/1.0/workspaces"
```
Each workspace object has a `gid` field.

---

## Step 3 — Find a Project GID (reference)

Project GIDs appear in Asana URLs:
- `https://app.asana.com/0/{workspace_gid}/{project_gid}/list`
- The second large number is the project GID.

Or query the API:
```bash
curl -H "Authorization: Bearer YOUR_PAT" \
     "https://app.asana.com/api/1.0/projects?workspace=WORKSPACE_GID"
```

---

## Step 4 — Install the connector

In the Shielva integration builder:

1. Navigate to **Integrations → Asana**.
2. Click **Connect** / **Install**.
3. Paste your **Personal Access Token** into the `api_key` field.
4. Optionally paste your **Workspace GID** into the `workspace_gid` field.
5. Click **Save / Install**.

The connector calls `GET /users/me` to validate credentials. On success, status changes to **Connected** and shows your Asana user name.

---

## Install fields

| Field | Key | Required | Description |
|-------|-----|----------|-------------|
| Personal Access Token | `api_key` | Yes | From My Profile Settings → Apps → Manage Developer Apps → New Access Token |
| Workspace GID | `workspace_gid` | No | Limits sync to one workspace/organization |

---

## What gets synced

| Resource | Endpoint | Notes |
|----------|----------|-------|
| Workspaces | `GET /workspaces` | All workspaces the token owner belongs to |
| Projects | `GET /projects?workspace={gid}` | All non-archived projects per workspace |
| Tasks | `GET /tasks?project={gid}` | All tasks per project |
| Sections | `GET /projects/{gid}/sections` | Via `list_sections()` |
| Users | `GET /users?workspace={gid}` | Via `list_users()` |

Pagination uses Asana's cursor-based `next_page.offset` — all pages are fetched automatically.

---

## Rate limits

Asana enforces rate limits per access token:

- **Default:** approximately 1500 requests per minute per token.
- The connector honours the `Retry-After` response header and retries automatically with exponential backoff (up to 3 attempts).
- On persistent 429 errors, reduce sync frequency or create a dedicated service-account token.

---

## Troubleshooting

### 401 Unauthorized
The PAT is invalid or revoked. Go to **My Profile Settings → Apps → Manage Developer Apps**, revoke the old token, and create a new one. Update in **Shielva ACP → Integrations → Asana → Edit credentials**.

### 403 Forbidden
Your account lacks access to a workspace or project. Verify the token owner is a member of the target workspace.

### 429 Too Many Requests
Rate limit hit. The connector retries automatically. If persistent, lower sync frequency.

### Sync returns 0 documents
- Confirm the workspace has at least one project with tasks.
- Confirm the token owner is a member of those projects.
- Run a health check to confirm connectivity.

### Token not visible after creation
Asana shows the token only once. Revoke the old one and create a new token.
