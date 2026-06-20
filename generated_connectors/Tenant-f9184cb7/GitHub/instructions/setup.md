# GitHub Connector — Setup Guide

## Overview

The GitHub connector syncs repositories, issues, and pull requests from your GitHub account into the Shielva knowledge base. It uses the **GitHub REST API v3** with **Bearer token authentication** (Personal Access Token or GitHub App token).

---

## Prerequisites

- A GitHub account (personal or organization)
- A Personal Access Token (classic) or a fine-grained PAT with the appropriate scopes

---

## Step 1 — Create a Personal Access Token

### Classic PAT (recommended for most users)

1. Go to **GitHub** → click your avatar → **Settings**.
2. In the left sidebar, go to **Developer settings** → **Personal access tokens** → **Tokens (classic)**.
3. Click **Generate new token (classic)**.
4. Give it a descriptive name (e.g., "Shielva Connector").
5. Set an expiration (90 days recommended; rotate regularly).
6. Select the required scopes:

| Scope | Purpose |
|---|---|
| `repo` | Full access to private/public repositories, issues, and PRs |
| `read:org` | Read organization membership (required for org-level repo listing) |

7. Click **Generate token** and copy the token immediately — GitHub shows it only once.

### Fine-grained PAT (enhanced security)

1. Go to **Developer settings** → **Personal access tokens** → **Fine-grained tokens**.
2. Click **Generate new token**.
3. Under **Repository access**, choose **All repositories** or select specific repositories.
4. Under **Permissions**, grant:
   - **Contents** → Read-only
   - **Issues** → Read-only
   - **Pull requests** → Read-only
   - **Metadata** → Read-only (mandatory)
5. Click **Generate token** and copy it.

---

## Step 2 — Configure the Connector

In the Shielva connector install form, fill in:

| Field | Key | Required | Description |
|---|---|---|---|
| Personal Access Token | `access_token` | Yes | The token generated in Step 1 |
| Organization Name | `org` | No | GitHub org slug for org-level access (e.g. `my-org`). Leave blank for user-level. |

---

## What the Connector Syncs

| Entity | Properties Synced |
|---|---|
| Repositories | full_name, description, language, default_branch, stars, forks, open_issues, visibility, topics, created_at, updated_at |
| Issues | number, title, state, body, author, labels, comments, created_at, updated_at, closed_at |
| Pull Requests | number, title, state, merged, author, head_ref, base_ref, draft, commits, additions, deletions, changed_files, labels, created_at, updated_at, merged_at |

The connector fetches all repos first, then for each repo fetches open issues and open pull requests. Issues that are actually PRs (GitHub quirk — the `/issues` endpoint returns both) are excluded to avoid double-counting.

---

## Pagination

The GitHub API paginates using `Link` response headers. The connector follows `rel="next"` links automatically until all pages are exhausted. Default page size is 100 items.

---

## Rate Limiting

GitHub enforces rate limits:

- **Unauthenticated**: 60 requests/hour
- **Authenticated (PAT)**: 5,000 requests/hour
- **GitHub App**: 15,000 requests/hour

The connector checks the `X-RateLimit-Remaining` header on every response and raises `GitHubRateLimitError` if it reaches 0. The retry logic (up to 3 attempts, exponential backoff + jitter) handles transient 429 responses.

---

## Troubleshooting

### 401 Unauthorized — Bad Credentials

- The token has expired or been revoked.
- Go to **GitHub Settings → Developer settings → Personal access tokens**, regenerate the token, and update the connector's `access_token` field.

### 403 Forbidden — Insufficient Scope

- The token is missing the required scopes.
- For a classic PAT: edit the token in GitHub and add the `repo` (and optionally `read:org`) scope.
- For a fine-grained PAT: regenerate with Issues + Pull requests + Metadata read permissions.

### 404 Not Found — Organization Not Found

- The `org` field is set to an organization slug that does not exist or the token does not have `read:org` scope.
- Verify the organization name (case-insensitive slug, e.g. `my-org`, not `My Org`).

### Rate Limit Exhausted

- The connector automatically retries with backoff.
- For large organizations with thousands of repos, consider scheduling syncs during off-peak hours.

### Connector Shows as Degraded

- The circuit breaker opens after 5 consecutive failures.
- Resolve the underlying error (auth, network, rate limit), then trigger a health check to reset.

---

## Security Notes

- Store your PAT in the Shielva secrets vault — never hard-code it in application config.
- Rotate your PAT regularly (90-day expiry recommended).
- Use fine-grained PATs with the minimum required permissions for production deployments.
- The connector sends requests to `https://api.github.com` with TLS enforced.

---

## API Reference

| Method | Endpoint |
|---|---|
| `health_check()` | `GET /user` |
| `list_repos()` (user) | `GET /user/repos` |
| `list_repos(org=...)` | `GET /orgs/{org}/repos` |
| `list_issues(owner, repo)` | `GET /repos/{owner}/{repo}/issues` |
| `get_issue(owner, repo, number)` | `GET /repos/{owner}/{repo}/issues/{number}` |
| `list_pull_requests(owner, repo)` | `GET /repos/{owner}/{repo}/pulls` |
| `get_pull_request(owner, repo, number)` | `GET /repos/{owner}/{repo}/pulls/{number}` |

Base URL: `https://api.github.com`
Auth header: `Authorization: Bearer {access_token}`
API version header: `X-GitHub-Api-Version: 2022-11-28`

---

## Support

For additional help, refer to the [GitHub REST API documentation](https://docs.github.com/en/rest) or contact Shielva support.
