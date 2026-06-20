# Coda Connector — Setup Guide

## Overview

This connector integrates with **Coda** (coda.io) — a document platform that combines spreadsheets, databases, and rich text pages into collaborative docs. It syncs docs, pages, tables, and row-level data via the [Coda API v1](https://coda.io/developers/apis/v1).

---

## Prerequisites

- A Coda account with at least one doc accessible to your API token
- API token scoped to read access on the relevant docs/workspaces

---

## Generating an API Token

1. Log in to [coda.io](https://coda.io)
2. Click your **profile avatar** (top-right) → **Account settings**
3. Navigate to the **API** tab (or go directly to [https://coda.io/account](https://coda.io/account))
4. Under **API settings**, click **Generate API token**
5. Give the token a descriptive name (e.g. `Shielva Sync`)
6. **Copy the token immediately** — it will not be shown again

> **Tip:** Coda API tokens have access to all docs the account owner can access. There is no per-scope granularity at token creation time; read access is implicit for all accessible resources.

---

## Install Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `api_token` | password | Yes | Your Coda API token (Bearer token) |

---

## Authentication

The connector authenticates using the `Authorization: Bearer {api_token}` HTTP header on every request. No OAuth flow is required.

---

## Resources Synced

| Resource | API Endpoint | Notes |
|----------|-------------|-------|
| Docs | `GET /docs` | All docs accessible to the token |
| Pages | `GET /docs/{docId}/pages` | Canvas pages, sync pages, and subpages per doc |
| Tables | `GET /docs/{docId}/tables` | Tables and views per doc |
| Rows | `GET /docs/{docId}/tables/{tableId}/rows` | All rows per table with `valueFormat=rich` |

Sync order: docs → pages (per doc) + tables (per doc) → rows (per table).

---

## Pagination

Coda uses **cursor-based pagination**:

- Response bodies include `nextPageToken` (a string cursor) and `nextPageLink` (a ready-to-use URL)
- Pass `pageToken=<value>` as a query parameter on the next request
- When `nextPageToken` is absent or `null`, all pages have been consumed

Default page sizes used by this connector:

| Resource | `limit` param |
|----------|--------------|
| Docs | 25 |
| Pages | 50 |
| Tables | 50 |
| Rows | 500 |

---

## Rate Limits

- **10 requests per second** per API token
- 429 responses are retried with exponential backoff (up to 3 attempts)
- For large workspaces with many tables/rows, syncs may take several minutes

---

## Error Codes

| HTTP Status | Meaning | Connector Exception |
|-------------|---------|-------------------|
| 401 | Invalid or missing API token | `CodaAuthError` |
| 403 | Forbidden (token lacks access) | `CodaAuthError` |
| 404 | Resource not found | `CodaNotFoundError` |
| 429 | Rate limited | `CodaRateLimitError` |
| 5xx | Server error | `CodaNetworkError` |

---

## Troubleshooting

**"api_token is required"** — The `api_token` field was not provided during install. Re-run install with the token value.

**401 on `/whoami`** — The token is invalid or was revoked. Generate a new one in Coda Account Settings.

**403 on a doc** — The token's owner does not have access to that specific doc. Share the doc with the account or use a token from an account that has access.

**Rows returning empty values** — Coda uses column IDs (e.g. `c-xyzAbcDef`) as keys in the `values` dict, not column names. Column-to-name mapping can be derived from the table's column list returned by `GET /docs/{docId}/tables/{tableId}/columns`.

---

## Links

- [Coda API v1 Reference](https://coda.io/developers/apis/v1)
- [Coda Account Settings / API Tokens](https://coda.io/account)
- [Coda API Rate Limits](https://coda.io/developers/apis/v1#section/Rate-Limiting)
