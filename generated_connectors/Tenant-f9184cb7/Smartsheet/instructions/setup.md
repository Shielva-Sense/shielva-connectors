# Smartsheet Connector — Setup Guide

This guide walks you through obtaining the API access token required by the Shielva Smartsheet connector.

---

## 1. Get your Smartsheet API Access Token

1. Log in to your Smartsheet account at [app.smartsheet.com](https://app.smartsheet.com).
2. Click your **Account** icon (person silhouette) in the top-right corner.
3. Select **Apps & Integrations** from the dropdown menu.
4. In the left sidebar under **API**, click **API Access**.
5. Click **Generate new access token**.
6. Give the token a name (e.g. `Shielva Connector`) and click **OK**.
7. **Copy the token immediately** — Smartsheet only shows it once.

> **Note:** You can also reach API Access via **Account → Personal Settings → API Access** on some plans.

---

## 2. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **API Token** | Paste your Smartsheet API access token here |

---

## 3. Token Scopes

Smartsheet API access tokens inherit the full permissions of the user account that created them:

- **Sheets** — read all sheets the account can access (own sheets + shared sheets).
- **Rows** — read all rows and cell values on accessible sheets.
- **Workspaces** — list all workspaces the account belongs to.
- **Reports** — list all reports the account can access.
- **Folders** — list top-level home folders.

If the account has restricted access (e.g. Viewer on specific sheets only), only those resources will be synced.

---

## 4. What Gets Synced

| Resource | Description |
|----------|-------------|
| Sheets | All sheets accessible to the token account (id, name, accessLevel, totalRowCount, created/modified timestamps, permalink) |
| Rows | All rows on each sheet (id, rowNumber, cells with displayValue, created/modified timestamps) |
| Workspaces | All workspaces the account belongs to (id, name, accessLevel) |
| Reports | All reports accessible to the account (id, name, accessLevel, created/modified timestamps) |
| Folders | Top-level home folders (listed but not recursed) |

Sync is a full snapshot — Smartsheet REST API 2.0 does not expose a built-in `updatedSince` filter on the sheets list endpoint, so each sync fetches all pages.

---

## 5. Pagination Details

The Smartsheet API uses **page-based pagination** for sheets and reports:

- Default page size: 100 for sheets/reports, 500 for rows
- Response includes `totalPages` and `pageNumber` fields
- The connector automatically fetches all pages until `pageNumber >= totalPages`

For workspaces, the connector uses `includeAll=true` to fetch all in a single request.

---

## 6. Verify the Connection

Once installed, the connector runs a health check by calling:

```
GET https://api.smartsheet.com/2.0/users/me
```

A successful health check returns the name of the authenticated user (e.g. `Connected as: Jane Doe`).

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `SmartsheetAuthError: HTTP 401` | Invalid or expired API token | Re-generate the token from **Account → Apps & Integrations → API Access** |
| `SmartsheetAuthError: HTTP 403` | Token lacks permission for the resource | Ensure the account has at least Viewer access to the target sheets/workspaces |
| `SmartsheetNotFoundError: HTTP 404` | Sheet or resource was deleted | Re-run sync; the resource will be skipped |
| `SmartsheetRateLimitError: HTTP 429` | Too many requests | The connector retries automatically with exponential back-off |
| `SmartsheetNetworkError` | Connection timeout or DNS failure | Check network connectivity to `api.smartsheet.com` |

### Smartsheet API Rate Limits

| Limit | Value |
|-------|-------|
| Requests per minute | 300 per account |
| Max response size | 100 MB per request |

The connector retries automatically with exponential back-off on rate limit errors (HTTP 429) and transient network errors.

---

## API Details

- **Base URL:** `https://api.smartsheet.com/2.0/`
- **Auth header:** `Authorization: Bearer {api_token}`
- **Content-Type:** `application/json`
- **API version:** REST API 2.0 (no version header required)

---

## Security Notes

- The API access token grants the same permissions as the account that created it. Store it securely — do not share it or commit it to version control.
- Tokens can be deleted and regenerated at any time from **Account → Apps & Integrations → API Access**.
- Shielva stores the token AES-256-GCM encrypted at rest via the vault. The plaintext token is never written to disk or logs.
