# Jira Connector — Setup Guide

## Overview

The Jira connector syncs issues, projects, and boards from your Atlassian Jira account into the Shielva knowledge base. Authentication uses HTTP Basic Auth with your Atlassian account email and an API token (not your Atlassian password).

---

## Step 1 — Generate an Atlassian API Token

1. Go to [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens).
2. Click **Create API token**.
3. Give it a label (e.g., "Shielva Jira Connector").
4. Click **Create**.
5. Copy the token immediately — Atlassian only shows it once.

> **Note:** This is different from your Atlassian password. The API token is used as the HTTP Basic Auth password when calling the Jira REST API.

---

## Step 2 — Find Your Atlassian Domain

Your Jira site URL follows the pattern: `https://<domain>.atlassian.net`

For example, if your Jira URL is `https://acme.atlassian.net`, your domain is `acme`.

Enter only the subdomain (e.g., `acme`) — not the full URL.

---

## Step 3 — Configure the Connector

In the Shielva connector install form, provide:

| Field | Value |
|---|---|
| **Atlassian Domain** (`domain`) | Your subdomain, e.g. `acme` (from `acme.atlassian.net`) |
| **Atlassian Account Email** (`email`) | The email of the Atlassian account that generated the API token |
| **Atlassian API Token** (`api_token`) | The token copied in Step 1 |

All three fields are required.

---

## Required Permissions

The Atlassian account used must have:

- **Browse Projects** permission on all projects you want to sync
- **View Issues** (read access) on the issue types you want indexed

For most Jira Cloud accounts, a standard team member role is sufficient. If certain projects do not appear in sync results, ask your Jira administrator to grant Browse Projects permission for those projects.

---

## What the Connector Syncs

| Object | Fields Synced |
|---|---|
| Issues | key, summary, description (ADF extracted), status, priority, assignee, reporter, project, issuetype, labels, created, updated |
| Projects | key, name, projectTypeKey (via `list_projects`) |
| Boards | id, name, type (via `list_boards`) |

The sync uses JQL `ORDER BY updated DESC` to fetch all issues across all projects. Pagination is handled automatically (100 issues per page).

Each synced issue gets a stable `id` derived from `SHA-256(issue_key)[:16]`.

---

## Troubleshooting

### 401 Unauthorized

- The API token has been revoked. Generate a new one at [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) and update the connector.
- Verify the email field matches the Atlassian account that owns the API token.

### 403 Forbidden

- The account lacks Browse Projects permission for that project. Ask your Jira admin to grant access.
- Check that the domain is correct (subdomain only, no `.atlassian.net` suffix).

### 404 Not Found

- The domain value may be incorrect. Verify your Jira site URL: `https://<domain>.atlassian.net`.
- Some Jira Agile features (boards) require the Jira Software plan — a 404 from `/rest/agile/1.0/board` indicates the plan does not include Agile features.

### 429 Too Many Requests

- Atlassian rate-limits the REST API per-user. The connector handles this automatically with exponential backoff (up to 3 retries).
- If syncs fail consistently on large Jira instances, consider scheduling syncs during off-peak hours.

### Network Timeouts

- Default request timeout is 30 seconds. Very large Jira instances may trigger timeouts on individual page fetches.
- The connector retries transient network errors up to 3 times with exponential backoff.

---

## API Reference

The connector uses two Atlassian REST APIs:

**Jira REST API v3** — base URL: `https://<domain>.atlassian.net/rest/api/3`

| Endpoint | Purpose |
|---|---|
| `GET /myself` | Health check / install validation |
| `GET /project/search` | List all projects |
| `GET /search?jql=...` | Paginated issue search |
| `GET /issue/{issue_key}` | Fetch a single issue |

**Jira Agile REST API v1** — base URL: `https://<domain>.atlassian.net/rest/agile/1.0`

| Endpoint | Purpose |
|---|---|
| `GET /board` | List all Agile boards |

---

## Support

For additional help, refer to the [Atlassian Jira REST API documentation](https://developer.atlassian.com/cloud/jira/platform/rest/v3/) or contact Shielva support.
