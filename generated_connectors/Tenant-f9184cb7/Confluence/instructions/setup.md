# Confluence Connector — Setup Guide

## Overview

The Confluence connector syncs spaces, pages, and blog posts from your Confluence Cloud instance into the Shielva knowledge base. It uses the Confluence Cloud REST API v2 with HTTP Basic Auth (Atlassian account email + API token).

---

## Prerequisites

- A Confluence Cloud account at `yourcompany.atlassian.net`
- View access to the spaces you want to sync (Space Viewer permission)
- An Atlassian API token (not your account password — password auth is not supported)

---

## Step 1 — Generate an Atlassian API Token

1. Log in to [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens).
2. Go to **Security** → **API tokens**.
3. Click **Create API token**.
4. Give the token a descriptive label (e.g., "Shielva Confluence Connector").
5. Copy the generated token — Atlassian displays it only once.
6. Store the token securely (a password manager is recommended).

---

## Step 2 — Identify Your Confluence Cloud Domain

Your Confluence Cloud URL follows the pattern: `https://<subdomain>.atlassian.net/wiki`.

- If your URL is `https://mycompany.atlassian.net/wiki`, your domain is **`mycompany`**.
- Enter only the subdomain portion — do **not** include `https://`, `.atlassian.net`, or `/wiki`.

---

## Step 3 — Required Confluence Permissions

The Atlassian account whose credentials you use must have:

| Permission | Where to grant |
|---|---|
| **Can use** (site-level) | Atlassian Admin → Products → Confluence → User access |
| **Space Viewer** on each space | Space Settings → Permissions → Add member |

A Confluence admin can grant these. The connector only reads data — no write permissions are needed.

---

## Step 4 — Configure the Connector

In the Shielva connector install form, fill in the following fields:

| Field | Key | Value |
|---|---|---|
| Atlassian Account Email | `email` | Your Atlassian account email address |
| API Token | `api_token` | The token generated in Step 1 |
| Confluence Domain | `domain` | Your subdomain only (e.g., `mycompany`) |

---

## Install Fields Reference

| Field | Key | Type | Required | Description |
|---|---|---|---|---|
| Atlassian Account Email | `email` | string | Yes | Email address of the Atlassian account that owns the API token |
| API Token | `api_token` | password | Yes | Atlassian API token — generated at id.atlassian.com/manage-profile/security/api-tokens |
| Confluence Domain | `domain` | string | Yes | Your Atlassian subdomain (e.g. `mycompany` from `mycompany.atlassian.net`) |

---

## What the Connector Syncs

| Content Type | API Endpoint | Document Type |
|---|---|---|
| Pages | `GET /wiki/api/v2/pages` | `confluence_page` |
| Blog Posts | `GET /wiki/api/v2/blogposts` | `confluence_blog_post` |

The connector iterates all accessible spaces and syncs every page and blog post with cursor-based pagination. Page body content is fetched in Confluence storage format and HTML tags are stripped to produce clean plain text for indexing.

---

## API Reference

The connector uses both Confluence API versions:

- **Base URL (v2):** `https://{domain}.atlassian.net/wiki/api/v2/`
- **Base URL (v1 legacy):** `https://{domain}.atlassian.net/wiki/rest/api/`
- **Auth:** HTTP Basic Auth — Base64-encoded `email:api_token`
- **Pagination:** Cursor-based via `_links.next` URL in the response body (v2); `start`/`limit` offset for v1 search

| Endpoint | Method | Purpose |
|---|---|---|
| `/wiki/rest/api/user/current` | GET | Validate credentials (install / health check) |
| `/wiki/api/v2/spaces` | GET | List all spaces (supports `type` filter) |
| `/wiki/api/v2/spaces/{id}` | GET | Single space by ID |
| `/wiki/api/v2/pages` | GET | List pages (supports `spaceId`, `status` filter) |
| `/wiki/api/v2/pages/{id}?body-format=storage` | GET | Single page with HTML body |
| `/wiki/api/v2/pages/{id}/children` | GET | Child pages of a given page |
| `/wiki/api/v2/blogposts` | GET | List blog posts (supports `spaceId` filter) |
| `/wiki/rest/api/search?cql=text~"{query}"` | GET | Full-text CQL search (supports cursor pagination) |

---

## Retry and Rate Limiting

The connector retries transient errors automatically:

- **Max attempts:** 3
- **Backoff:** Exponential with jitter (base 1s, max 30s)
- **Auth errors (401/403):** Never retried — human intervention required
- **Rate limits (429):** Waits for the `Retry-After` header value before retrying

For very large Confluence instances (thousands of pages), consider scheduling syncs during off-peak hours.

---

## Troubleshooting

### 401 Unauthorized

- The API token has been revoked or is incorrect.
- Go to [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens), generate a new token, and update the connector's `api_token` field.
- Ensure you are using the full token value (starts with `ATATT...`), not your Atlassian account password.

### 403 Forbidden

- The Atlassian account does not have view access to one or more spaces.
- Ask a Confluence admin to grant **Space Viewer** access, or use credentials for an account with broader access.

### 404 Not Found

- The `domain` field is incorrect.
- Double-check you entered only the subdomain (e.g., `mycompany`, not `mycompany.atlassian.net`).

### 429 Too Many Requests

- Atlassian enforces rate limits on Confluence Cloud REST API calls.
- The connector retries automatically with exponential backoff (up to 3 attempts, honouring `Retry-After`).
- For large Confluence instances, schedule sync during off-peak hours.

### Network Timeouts

- Default request timeout is 30 seconds.
- The connector retries transient network errors up to 3 times.

### Connector Shows as Degraded

- Transient network or Atlassian service errors have occurred.
- The connector recovers automatically on the next health check or sync.

### documents_failed > 0 in Sync Result

- One or more records returned unexpected data or the account lacks access to specific spaces.
- Common causes: private spaces the account cannot read, corrupted page metadata.
- The sync continues for all other spaces and pages — only affected records are skipped.

---

## Support

- [Confluence Cloud REST API v2 documentation](https://developer.atlassian.com/cloud/confluence/rest/v2/intro/)
- [Atlassian API token management](https://id.atlassian.com/manage-profile/security/api-tokens)
- Shielva support: contact your Shielva administrator or the Shielva support team
