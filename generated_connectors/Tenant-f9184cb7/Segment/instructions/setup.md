# Segment Connector — Setup Guide

This guide walks you through obtaining a Segment Public API access token and connecting it to Shielva.

---

## 1. Obtain Your Segment Public API Access Token

1. Log in to your Segment workspace at [app.segment.com](https://app.segment.com).
2. Click **Settings** in the left sidebar.
3. Under **Access Management**, select **Tokens**.
4. Click **Create Token**.
5. Give the token a descriptive name (e.g. "Shielva Connector") and choose a workspace role:
   - **Workspace Owner** — full read/write access. Suitable for all connector operations.
   - **Workspace Member** — read access is sufficient for the connector's sync, health check, and listing operations.
6. Click **Create** and immediately copy the token — it is shown only once.

> **Security note:** Treat your API access token like a password. Do not share it or commit it to version control. Shielva stores it AES-256-GCM encrypted at rest.

---

## 2. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **Public API Access Token** | Your Segment Public API token |

The connector validates the token by calling `GET /workspaces` before completing installation.

---

## 3. What Gets Synced

The connector syncs three resource types on each run:

| Resource | Segment API | Description |
|----------|-------------|-------------|
| Sources | `GET /sources` | All data sources with name, slug, enabled status, write key, and metadata categories |
| Spaces | `GET /spaces` | Segment Profiles AI spaces with name and slug |
| Functions | `GET /functions` | Source, destination, and insert functions with display name, type, and timestamps |

Source sync uses cursor-based pagination (`pagination[cursor]`) to handle workspaces with large numbers of sources — there is no record limit.

---

## 4. API Details

- **Base URL:** `https://api.segmentapis.com`
- **Auth Header:** `Authorization: Bearer {access_token}`
- **Content Type:** `Content-Type: application/json`
- **Response Format:** JSON envelope — `{ "data": { "<resource>": [...] } }`
- **Pagination:** Cursor-based via `data.pagination.next` — the connector follows all pages automatically

### Pagination format

```json
{
  "data": {
    "sources": [...],
    "pagination": {
      "current": "MA==",
      "next": "MjAw"
    }
  }
}
```

When `pagination.next` is present, the connector passes it as `pagination[cursor]` on the next request until `next` is null.

---

## 5. Verify the Connection

After installation, click **Test Connection**. The connector calls `GET /workspaces` and returns the workspace name if the token is valid.

A healthy response looks like:
```
Connected to Segment — Acme Analytics
```

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `access_token is required` | No token provided | Enter the Public API token in the install form |
| `401 Unauthorized` | Invalid or revoked token | Re-generate the token in Segment → Settings → Access Management → Tokens |
| `403 Forbidden` | Insufficient token scope | Ensure the token has at minimum Workspace Member (read) role |
| `429 Too Many Requests` | Rate limit exceeded | The connector retries automatically with exponential back-off |
| No sources returned | Empty workspace or wrong token | Verify the workspace has sources and the token belongs to the correct workspace |

---

## Rate Limits

Segment Public API enforces rate limits per token. The connector includes automatic retry with exponential back-off for 429 responses (up to 3 attempts). The `Retry-After` header is honoured when present.

---

## Document ID Stability

Each synced document receives a stable ID computed as `SHA-256("source:" + resource_id)[:16]`. The same Segment source, space, or function always maps to the same Shielva document ID across syncs, enabling upsert deduplication without storing seen-ID lists.

---

## Available API Methods

| Method | Endpoint | Description |
|--------|----------|-------------|
| `install()` | `GET /workspaces` | Validate token and complete installation |
| `health_check()` | `GET /workspaces` | Verify connectivity and return workspace name |
| `sync()` | Multiple | Full sync of sources, spaces, and functions |
| `list_workspaces()` | `GET /workspaces` | Retrieve the workspace for this token |
| `list_sources(pagination_cursor)` | `GET /sources` | Paginated list of all sources |
| `get_source(source_id)` | `GET /sources/{source_id}` | Retrieve a single source |
| `list_destinations(source_id)` | `GET /sources/{source_id}/destinations` | Destinations for a source |
| `list_spaces()` | `GET /spaces` | All Profiles AI spaces |
| `list_functions(pagination_cursor)` | `GET /functions` | Paginated list of all functions |
