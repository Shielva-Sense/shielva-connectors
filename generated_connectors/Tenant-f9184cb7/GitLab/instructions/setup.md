# GitLab Connector — Setup Guide

## Overview

The GitLab connector syncs your GitLab **projects, issues, merge requests, pipelines, groups, and members** into Shielva. It connects using a Personal Access Token (PAT) and communicates with the GitLab REST API v4.

Both **gitlab.com** and **self-hosted GitLab** instances are supported.

---

## Step 1 — Create a Personal Access Token

1. Sign in to your GitLab account.
2. In the top-right corner, click your **avatar → Edit profile**.
3. In the left sidebar, click **Access Tokens**.
4. Click **Add new token**.
5. Fill in the form:
   - **Token name**: e.g. `Shielva Integration`
   - **Expiration date**: choose a date (GitLab may enforce a maximum expiry)
   - **Select scopes**: check all of the following:
     - **`read_api`** — read-only access to the API (projects, issues, MRs, pipelines, groups, members)
     - **`read_user`** — read-only access to user profile info (used for health check)
     - **`read_repository`** — read-only access to repository files and commits
6. Click **Create personal access token**.
7. **Copy the token immediately** — it will not be shown again.

> For project-scoped access only, you can use a **Project Access Token** instead. Navigate to your project → **Settings → Access Tokens** and follow the same steps.

---

## Step 2 — Configure the connector in Shielva

| Field | Value |
|---|---|
| **Personal Access Token** | Paste the token you created above (field key: `api_key`) |
| **GitLab URL** | Leave blank for `https://gitlab.com`, or enter your self-hosted URL (e.g. `https://gitlab.mycompany.com`) |
| **Group ID or Path** | *(Optional)* Enter a group ID or path (e.g. `mygroup`) to scope project sync to one group |

---

## Self-Hosted GitLab

If you use a self-hosted GitLab instance:

1. Enter the **full URL** of your instance in the **GitLab URL** field, e.g. `https://gitlab.internal.corp`.
2. Ensure your Shielva deployment can reach the GitLab instance on port 443 (HTTPS).
3. The connector uses `{base_url}/api/v4/` as the API root.
4. Your PAT must be created on your self-hosted instance (not on gitlab.com).

---

## Required Token Scopes

| Scope | Purpose |
|---|---|
| `read_api` | Read-only access to all API resources (projects, issues, MRs, pipelines, groups, members) |
| `read_user` | Read-only access to user profile info (health check uses `GET /user`) |
| `read_repository` | Read-only access to repository contents and commits |

The minimum required scope is `read_api` + `read_user`. Add `read_repository` for full functionality.

---

## Group vs. Project Access

- **No `group_id` configured**: the connector syncs all projects the token owner is a member of (across all groups).
- **`group_id` configured**: the connector scopes project discovery to `GET /groups/{group_id}/projects`, reducing sync scope to one group and its subgroups.
- **Members**: `list_members` fetches `GET /groups/{group_id}/members` and requires `group_id` to be set.

---

## Rate Limits

GitLab enforces rate limits based on your plan:

| Plan | Authenticated limit |
|---|---|
| Free (gitlab.com) | 600 requests/minute |
| Premium | 1200 requests/minute |
| Ultimate | 2000 requests/minute |
| Self-hosted | Configurable by admin (default 300 req/min per user) |

The connector automatically retries on 429 responses with exponential backoff (up to 3 attempts).

---

## What Gets Synced

| Resource | GitLab API Endpoint |
|---|---|
| Projects | `GET /projects` or `GET /groups/{id}/projects` |
| Issues | `GET /projects/{id}/issues` (per project) |
| Merge Requests | `GET /projects/{id}/merge_requests` |
| Pipelines | `GET /projects/{id}/pipelines` |
| Groups | `GET /groups` |
| Members | `GET /groups/{id}/members` (requires `group_id`) |

---

## Troubleshooting

**401 Unauthorized** — The token is invalid, expired, or was not copied correctly. Regenerate and re-enter it.

**403 Forbidden** — The token lacks the required scope, or the token does not have access to the requested project/group.

**404 Not Found** — The project, group, or resource ID does not exist, or the token does not have visibility into it (e.g. private project with no membership).

**Connection refused / Network error** — Check that your Shielva deployment can reach the GitLab instance URL. For self-hosted instances verify firewall rules and TLS certificates.

**Empty sync** — If no projects appear, the PAT may belong to a user with no group memberships. Ensure the GitLab user linked to the PAT is a member of at least one group or project. Alternatively, configure `group_id` to scope to a specific group.
