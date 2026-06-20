# Monday.com Connector — Setup Guide

This guide walks you through obtaining the API token required by the Shielva Monday.com connector.

---

## 1. Get your Monday.com API Token

1. Log in to your Monday.com account at [monday.com](https://monday.com).
2. Click your **avatar** (profile picture) in the top-right corner.
3. Select **Developers** from the dropdown menu.
4. In the developer panel, click **My Access Tokens** in the left sidebar.
5. Click **Show** next to your personal API token to reveal it.
6. Copy the token.

> **Note:** You can also find the token at **Profile → Developers → My Access Tokens**. The token is a long alphanumeric string tied to your account.

---

## 2. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **API Token** | Paste your Monday.com personal API token here |

---

## 3. Permissions

The personal API token inherits the permissions of the account it belongs to:

- **Boards** — the connector can read all boards the account has access to (public, subscribed private, and shareable boards).
- **Items** — all items and their column values on accessible boards are synced.
- **Workspaces** — lists all workspaces the account belongs to.

If the account has limited board access (e.g. a viewer-only account), only those boards will be synced.

---

## 4. What Gets Synced

| Resource | Description |
|----------|-------------|
| Boards | All boards accessible to the token account (id, name, description, state) |
| Items | All items on each board (id, name, column values — status, person, date, text, etc.) |
| Workspaces | All workspaces the account belongs to |

Sync is a full snapshot — there is no incremental cursor since Monday.com GraphQL API v2 does not expose a built-in `updated_at` filter on `boards()`.

---

## 5. Verify the Connection

Once installed, the connector runs a health check by calling:

```graphql
{ me { name email } }
```

A successful health check returns the name of the authenticated user (e.g. `Connected as: Alice Smith`).

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `MondayAuthError: not authenticated` | Invalid or expired API token | Re-copy the token from **Profile → Developers → My Access Tokens** |
| `MondayAuthError: invalid api key` | Token was revoked or belongs to a deactivated account | Generate a new token from an active account |
| `MondayRateLimitError: rate limited` | Too many GraphQL requests | The connector retries automatically with exponential back-off. Large workspaces with many boards may hit the complexity budget |
| `MondayNetworkError` | Connection timeout or DNS failure | Check network connectivity to `api.monday.com` |
| `MondayNotFoundError: board X not found` | Board was deleted or access was revoked | Re-run sync; the board will be skipped |
| No items returned | Board is empty | No items exist on the board — this is expected |

### Monday.com API Rate Limits

Monday.com GraphQL API v2 uses a **complexity budget** system:

| Limit | Value |
|-------|-------|
| Max complexity per request | 10,000,000 points |
| Minute budget | 10,000,000 points |

Simple queries (boards list, items page) are low complexity. The connector uses cursor-based `items_page` pagination to stay within budget. The retry helper backs off automatically on `complexity budget exhausted` errors.

---

## API Details

- **Endpoint:** `POST https://api.monday.com/v2`
- **Auth header:** `Authorization: <api_token>` (no `Bearer` prefix)
- **API version header:** `API-Version: 2023-10`
- **Query language:** GraphQL

---

## Security Notes

- The personal API token grants broad read access to your Monday.com account. Store it securely — do not share it or commit it to version control.
- Tokens can be regenerated at any time from **Profile → Developers → My Access Tokens → Regenerate**.
- Shielva stores the token AES-256-GCM encrypted at rest via the vault. The plaintext token is never written to disk or logs.
