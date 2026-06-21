# Microsoft Outlook (Mail) connector — setup

This connector talks to Microsoft Outlook mail through the **Microsoft Graph v1.0** API using OAuth2 authorization-code with refresh tokens.

## 1. Register an Azure AD application

1. Sign in at <https://entra.microsoft.com> → **App registrations** → **New registration**.
2. **Supported account types**:
   - Multi-tenant SaaS → *Accounts in any organizational directory and personal Microsoft accounts*.
   - Single-tenant → *Accounts in this organizational directory only*.
3. **Redirect URI** (Web): the gateway's OAuth callback, e.g.
   `https://gateway.shielva.example.com/connectors/oauth/callback`.
4. After creation, copy:
   - **Application (client) ID** → `client_id`
   - **Directory (tenant) ID** → `tenant_id` (use `common` for multi-tenant).

## 2. Add a client secret

1. **Certificates & secrets** → **New client secret** → 24 months (rotate before expiry).
2. Copy the **Value** column immediately — Azure shows it once. This is `client_secret`.

## 3. Grant delegated Graph permissions

Under **API permissions → Add a permission → Microsoft Graph → Delegated permissions**:

- `Mail.Read`
- `Mail.Send`
- `Mail.ReadWrite`
- `offline_access` (required for refresh tokens)

If your tenant requires admin consent, click **Grant admin consent**.

## 4. Install the connector

In Shielva, install the connector with:

| Field | Value |
|---|---|
| `client_id` | from step 1 |
| `client_secret` | from step 2 |
| `tenant_id` | `common` (multi-tenant) or your tenant GUID |
| `scopes` | `Mail.Read Mail.Send Mail.ReadWrite offline_access` (default) |
| `auth_url` | leave default unless using a sovereign cloud |
| `token_url` | leave default unless using a sovereign cloud |
| `base_url` | leave default |
| `rate_limit_per_min` | default `120` |

Complete the OAuth consent screen — Shielva stores the access + refresh tokens.

## 5. Verify

Call **Health Check** in the connector dashboard — a green status means `/me` returned successfully and the token is valid.

## 6. Operational notes

- **Throttling**: Microsoft Graph returns `429 Too Many Requests` with a `Retry-After` header during burst load. The HTTP client honours the header automatically for waits ≤ 30 s; longer windows fall through to caller-level retry with exponential backoff.
- **Token refresh**: tokens auto-refresh on 401. Lose your refresh token (e.g. by revoking app consent) and the connector re-enters `TOKEN_EXPIRED` — re-authorize from the dashboard.
- **Sovereign clouds**: for GCC-High / DoD / China clouds, replace `auth_url`, `token_url`, and `base_url` with the appropriate sovereign endpoints.
- **Secret rotation**: rotate the client secret in Azure before it expires; update the Shielva connector config and re-authorize.
