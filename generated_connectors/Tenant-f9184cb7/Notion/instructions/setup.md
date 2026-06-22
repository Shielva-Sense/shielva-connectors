# Notion Connector — Setup Guide

This guide walks you through creating a Notion Internal Integration and obtaining the Integration Token required by the Shielva Notion connector.

---

## 1. Create a Notion Integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) and sign in to your Notion account.
2. Click **+ New integration**.
3. Enter a **Name** (e.g. "Shielva Connector") and select the **Associated workspace**.
4. Under **Capabilities**, select at minimum:
   - **Read content**
   - **Read user information including email addresses** (optional — for user metadata)
5. Click **Submit** to create the integration.
6. Copy the **Internal Integration Token** — it starts with `secret_`.

---

## 2. Share Pages with the Integration

Notion integrations only have access to pages explicitly shared with them.

For each page or database you want Shielva to sync:

1. Open the page in Notion.
2. Click the **···** (three dots) menu at the top right.
3. Click **Add connections** (or **Connect to** in older UI).
4. Search for your integration name and click it to grant access.

To grant access to an entire workspace, share your workspace root page (the top-level page in your sidebar) with the integration.

---

## 3. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **Integration Token** | `secret_your-token-here` |

---

## 4. What the Connector Syncs

The connector calls `POST /search` to discover all pages and databases accessible to your integration, then:

- **Pages** — fetched with full block content (paragraphs, headings, lists, code, etc.)
- **Databases** — fetched with property schema summary

Each object is normalized into a `ConnectorDocument` with a stable SHA-256-based ID for deduplication across syncs.

---

## 5. Verify the Connection

Once installed, the connector calls `GET /users/me` to confirm the integration token is valid. A successful health check returns the bot name.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `401 Unauthorized` | Invalid or expired token | Re-copy the Internal Integration Token from your integration settings |
| `403 Forbidden` | Integration not shared with the page | Open the page in Notion → **···** → **Add connections** → select your integration |
| `404 Not Found` | Page or database does not exist or is not shared | Share the page with the integration |
| `429 Too Many Requests` | Rate limit exceeded | The connector retries automatically with exponential back-off |
| No pages returned | Integration has no shared pages | Share at least one page with the integration |

### Notion API Rate Limits

The Notion API is rate-limited at **3 requests per second** per integration. The connector includes automatic retry with exponential back-off for 429 responses.

---

## Pagination

All API calls that return lists use cursor-based pagination (`has_more` + `next_cursor`). The connector follows all pages automatically — there is no page-count limit imposed by the connector.

---

## Block Content

`get_page_content()` recursively fetches child blocks up to any nesting depth. This includes:

| Block Type | Rendered As |
|------------|-------------|
| `paragraph` | Plain text |
| `heading_1/2/3` | `# / ## / ###` prefixed text |
| `bulleted_list_item` | `- ` prefixed text |
| `numbered_list_item` | `1. ` prefixed text |
| `to_do` | `[ ] ` or `[x] ` prefixed text |
| `quote` | `> ` prefixed text |
| `callout` | `> ` prefixed text |
| `code` | ```` ```language\ncode\n``` ```` |
| `divider` | `---` |
| `equation` | LaTeX expression string |

---

## Security Notes

- The Integration Token grants access only to pages explicitly shared with it. Store it securely — do not share or commit it to version control.
- Tokens can be regenerated at any time from [notion.so/my-integrations](https://www.notion.so/my-integrations) → your integration → **Secrets**.
- Shielva stores the token encrypted at rest using the vault's AES-256-GCM encryption.
