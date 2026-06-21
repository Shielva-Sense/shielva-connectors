# Bitrix24 Connector — Setup

The Bitrix24 connector talks to a single Bitrix24 portal over the public REST
API. Authentication is OAuth2 against `oauth.bitrix.info`; data calls go to
`https://{your-domain}.bitrix24.com/rest/{method}.json`.

## 1 — Register a Bitrix24 OAuth2 application

Bitrix24 supports two app modes — pick the one that matches your deployment.

### Option A: Local application (single-portal, fastest)

1. Sign into your Bitrix24 portal as administrator.
2. Open **Developer resources → Other → Local application**.
3. Create a new local application with these settings:
   - **Type:** *Server-side*
   - **Handler path:** the Shielva connector callback URL provided by the gateway
     (`https://<gateway-host>/connectors/<connector_id>/oauth/callback`).
   - **Permissions (scopes):** `crm`, `task`, `user` (mirror the connector's
     default scopes — add more as needed).
4. Save and copy the **Application ID** (`client_id`) and **Application key**
   (`client_secret`).

### Option B: Marketplace application (multi-portal distribution)

1. Use the **Bitrix24 Partner Cabinet** at <https://partners.bitrix24.com/>.
2. Submit a new application with the same scopes; the workflow is the same once
   approved — you'll get a `client_id` / `client_secret` pair.

## 2 — Configure the Shielva connector

In the Shielva admin console, install the **Bitrix24** connector and fill in:

| Field                | Value                                                                                              |
|----------------------|----------------------------------------------------------------------------------------------------|
| Portal Subdomain     | `mycompany` (the bit before `.bitrix24.com`)                                                       |
| OAuth2 Client ID     | from step 1                                                                                        |
| OAuth2 Client Secret | from step 1                                                                                        |
| OAuth2 Scopes        | `crm task user` (default)                                                                          |
| Authorization URL    | `https://oauth.bitrix.info/oauth/authorize/` (default)                                              |
| Token URL            | `https://oauth.bitrix.info/oauth/token/` (default)                                                  |
| Rate Limit           | `2` requests/minute (Bitrix is aggressive — bump higher only if your plan permits)                  |

Click **Install**, then **Connect** to launch the OAuth consent screen. After
approval Bitrix24 redirects back with an authorization code; the connector
exchanges it for access + refresh tokens and the status flips to
**CONNECTED**.

## 3 — Verify with a health check

The connector calls `app.info` for its health probe. From the connector detail
page in the Shielva console click **Run health check** — a green
**HEALTHY** / **CONNECTED** state confirms the token works and your domain
is reachable.

## 4 — API surface

Each method maps 1:1 to a Bitrix24 REST endpoint:

- `list_contacts(start, limit)` → `crm.contact.list`
- `get_contact(contact_id)` → `crm.contact.get`
- `create_contact(name, last_name, phone, email)` → `crm.contact.add`
- `update_contact(contact_id, fields)` → `crm.contact.update`
- `delete_contact(contact_id)` → `crm.contact.delete`
- `list_deals(start, limit)` → `crm.deal.list`
- `create_deal(title, contact_id, opportunity, stage_id)` → `crm.deal.add`
- `list_companies(start, limit)` → `crm.company.list`
- `list_tasks(start, limit)` → `tasks.task.list`
- `create_task(title, responsible_id, description)` → `tasks.task.add`
- `list_users(start)` → `user.get`

## 5 — Quotas and retries

Bitrix24 enforces both a **per-second** quota (~2 r/s) and an **Operation Time
Limit** (cumulative compute time per portal). The connector uses
exponential-backoff retries (1 s → 2 s → 4 s → 8 s …, up to 32 s) for any
`QUERY_LIMIT_EXCEEDED`, `OPERATION_TIME_LIMIT`, HTTP 429, or HTTP 503 response.

If you see repeated rate-limit errors, lower the connector's request rate or
use Bitrix24 batch calls — extend the connector with a `batch` method rather
than firing many parallel calls from the caller.

## 6 — Troubleshooting

- **`expired_token` / `invalid_token`** — the refresh token expired or was
  revoked. Re-authorize the connector from the Shielva console.
- **`NO_AUTH_FOUND`** — you have not completed OAuth yet. Click **Connect**.
- **`ACCESS_DENIED`** — the OAuth scopes don't cover the method you called.
  Add the missing scope to the Bitrix24 application and re-authorize.
- **HTTP 503** — Bitrix24 maintenance window or operating-quota saturation;
  the connector retries automatically.
