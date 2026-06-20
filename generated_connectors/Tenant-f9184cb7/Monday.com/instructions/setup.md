# Monday.com Connector — Setup Guide

## Overview

The Monday.com connector syncs boards and work items from your Monday.com account into Shielva using the Monday.com GraphQL API v2. It authenticates with a personal API v2 token.

---

## Step 1 — Generate a Monday.com API Token

1. Log in to your Monday.com account at [https://monday.com](https://monday.com).
2. Click your **avatar** in the top-right corner → **Developers**.
3. In the Developer Centre, navigate to **My Access Tokens** (left sidebar).
4. Click **Show** next to your personal API v2 token.
5. Copy the token — it is a long alphanumeric string (no "Bearer" prefix needed).

> **Tip:** If you do not see Developer options, ask your Monday.com account Admin to enable the **Developer** feature for your account in **Admin → Features**.

---

## Step 2 — Required Scopes

The following Monday.com API permissions are required for this connector:

| Scope | Purpose |
|-------|---------|
| `boards:read` | List and read boards, groups, and columns |
| `workspaces:read` | Access workspace metadata |
| `users:read` | List users in the account |
| `teams:read` | List teams in the account |

Personal API tokens inherit the permissions of the user who generated them. If the token owner has Admin access, all scopes above are covered automatically.

---

## Step 3 — Install the Connector in Shielva

1. In the Shielva Admin Console, go to **Connectors → Add Connector**.
2. Search for **Monday.com** and click **Connect**.
3. Paste your API v2 token into the **API Token** field.
4. Click **Save & Test** — Shielva calls `{ me { id name email } }` to verify the token.

---

## Step 4 — Find Your Workspace and Board IDs (optional)

If you need Board IDs for direct API testing:

**Via the Monday.com UI:**
- Open a board → the URL path contains the board ID: `https://your-account.monday.com/boards/<BOARD_ID>`

**Via the API Explorer:**
- Go to **Developer Centre → API Explorer**.
- Run: `{ boards(limit: 10) { id name } }` to list your boards.

---

## Step 5 — Verify the Connection

Once installed, run a health check from the Shielva connector page. A successful check returns:

```
Connected as: Your Name (your@email.com)
```

If the health check fails:
- Verify the token was not truncated on copy.
- Check that the token owner's account has at least `boards:read` and `users:read`.
- If the account uses SSO, confirm API tokens are allowed by your IT/SSO policy.

---

## Data Synced

| Resource | Type | Notes |
|----------|------|-------|
| Boards | `board` | All boards visible to the token owner |
| Work Items | `work_item` | All items within each board (cursor-paginated) |

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `HTTP 401 — invalid API key` | Token is wrong or expired | Regenerate a new API v2 token |
| `HTTP 429 — rate limited` | Too many API calls | Reduce sync frequency; the connector uses exponential-backoff retry |
| `HTTP 500 — server error` | Monday.com service outage | Check [status.monday.com](https://status.monday.com) |
| `GraphQL error: Not authenticated` | Token is invalid | Re-enter a valid token |
| `Board not found` | Board was deleted or token lacks access | Check board visibility settings |

---

## Security Notes

- API tokens are stored encrypted at rest in the Shielva credential store.
- Tokens are never logged or transmitted in plaintext beyond the initial install.
- The connector uses `Authorization: <token>` (no "Bearer" prefix) as required by the Monday.com API v2 specification.
