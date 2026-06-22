# ClickUp Connector — Setup Guide

## Overview

The ClickUp connector syncs tasks and lists from your ClickUp workspaces into the Shielva knowledge base using the ClickUp API v2. Authentication uses a **Personal API Token** — no OAuth app required.

---

## Prerequisites

- A ClickUp account (Free or paid — any plan)
- Access to ClickUp **Settings** (to generate a Personal API Token)

---

## Step 1 — Generate a Personal API Token

1. Log in to [app.clickup.com](https://app.clickup.com).
2. Click your **avatar** in the lower-left corner.
3. Select **Settings**.
4. In the left sidebar, click **Apps**.
5. Under **API Token**, click **Generate** (or **Regenerate** if you have generated one before).
6. Copy the token — it starts with `pk_`.

> **Important:** treat this token like a password. It grants full access to your ClickUp account. Store it securely and do not commit it to source control.

---

## Step 2 — Install the Connector

When prompted by the Shielva connector install wizard, paste the token into the **Personal API Token** field.

| Field | Value |
|-------|-------|
| Personal API Token | `pk_...` (your ClickUp token) |

The connector validates the token immediately by calling `GET /user`. If the token is invalid or expired, the install will fail with an `invalid_credentials` error.

---

## Step 3 — Understand the ClickUp Hierarchy

ClickUp organizes project management resources in a strict hierarchy:

```
Team (Workspace)
└── Space
    ├── Folder
    │   └── List
    │       └── Task
    └── List (folderless)
        └── Task
```

| Level | Description |
|-------|-------------|
| **Team** | Your ClickUp workspace. One account may belong to multiple teams. |
| **Space** | A high-level project area within a team (e.g. Engineering, Marketing). |
| **Folder** | Optional grouping within a space (e.g. Sprint 1, Q3 Planning). |
| **List** | A collection of tasks. Lists can be inside folders or directly in a space. |
| **Task** | An individual work item with status, priority, assignees, and tags. |

---

## Step 4 — What Gets Synced

The connector walks the full hierarchy and produces two document types:

| Document type | ClickUp resource | ID prefix |
|---------------|-----------------|-----------|
| `task` | Task | `sha256("task:" + task_id)[:16]` |
| `task_list` | List | `sha256("list:" + list_id)[:16]` |

For each task, the following fields are indexed:
- Name, description, status, priority
- Assignee usernames
- Tags
- List name, folder name
- Creation and update timestamps
- Direct URL (`https://app.clickup.com/t/{task_id}`)

---

## Step 5 — Pagination

ClickUp tasks use **page-based pagination** (`?page=0`, `?page=1`, …). The connector automatically fetches all pages until:
- The page returns an empty `tasks` array, or
- The response contains `"last_page": true`

There is no need to configure page sizes — the connector handles this automatically.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `install` fails with `invalid_credentials` | Token is wrong or expired | Regenerate the token in ClickUp Settings → Apps |
| `health_check` returns DEGRADED | Network error or ClickUp API downtime | Check [status.clickup.com](https://status.clickup.com) |
| Tasks missing from sync | Tasks are in a private space | Ensure the token belongs to a user with access to that space |
| Rate limit errors (429) | Too many API calls | The connector retries automatically; reduce sync frequency if persistent |
| `sync` shows 0 documents | No tasks in any list | Verify your ClickUp workspace has at least one list with tasks |

---

## API Reference

Base URL: `https://api.clickup.com/api/v2/`

| Endpoint | Description |
|----------|-------------|
| `GET /user` | Verify credentials, retrieve authenticated user |
| `GET /team` | List all workspaces |
| `GET /team/{team_id}/space` | List spaces in a workspace |
| `GET /space/{space_id}/folder` | List folders in a space |
| `GET /folder/{folder_id}/list` | List task lists in a folder |
| `GET /space/{space_id}/list` | List folderless task lists in a space |
| `GET /list/{list_id}/task?page=N` | List tasks (paginated) |
| `GET /task/{task_id}` | Retrieve a single task |
| `GET /list/{list_id}/member` | List members of a list |

---

## Security Notes

- The Personal API Token is stored encrypted at rest in the Shielva secrets vault.
- The token is transmitted only over HTTPS.
- The connector never stores or logs raw task content beyond what is required for indexing.
- Revoke the token from ClickUp Settings → Apps if you suspect it has been compromised.
