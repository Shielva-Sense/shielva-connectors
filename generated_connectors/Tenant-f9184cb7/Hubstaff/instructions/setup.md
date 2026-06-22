# Hubstaff Connector — Setup

## 1. Create a Hubstaff Personal Access Token

1. Sign in at https://hubstaff.com.
2. Open https://developer.hubstaff.com/personal_access_tokens.
3. Click **Create token**, label it (e.g. `Shielva integration`), and copy the
   token shown once — it cannot be retrieved later.

Hubstaff treats this Personal Access Token as a **refresh token**: it is
exchanged at `https://account.hubstaff.com/access_tokens` for short-lived
bearer access tokens.

## 2. Install the connector

Provide the following install fields to the Shielva platform when registering
the connector instance:

| Field | Required | Default | Notes |
| --- | --- | --- | --- |
| `refresh_token` | yes | — | The Personal Access Token from step 1 |
| `default_organization_id` | no | — | Numeric Hubstaff org ID; `sync()` falls back to the first accessible org if blank |
| `base_url` | no | `https://api.hubstaff.com/v2` | Override only for staging / private deployments |
| `token_url` | no | `https://account.hubstaff.com/access_tokens` | Override only for staging |
| `rate_limit_per_min` | no | `60` | Soft cap; Hubstaff's standard quota is ~60 rpm |

The platform calls `install()` first, which performs an immediate refresh-token
exchange to confirm the credential is live. A successful install returns
`auth_status=CONNECTED`.

## 3. Re-authorize

If the refresh token is rotated externally, call `authorize(auth_code=<new PAT>)`
on the connector. The new token is stored and immediately exchanged for an
access token.

## 4. Operating notes

- **Refresh-on-401** — the HTTP layer transparently rotates the access token on
  any 401 response and retries the failed request once.
- **Retry-on-429/5xx** — exponential backoff (0.5s → 16s) honouring `Retry-After`.
- **Tenant isolation** — every connector instance is scoped to `tenant_id` +
  `connector_id`; no global state is shared between tenants.
- **Sync** — defaults to a 1-day window when no checkpoint exists; uses the
  daily-activities API as the canonical document source. Adjust by passing
  `since=<datetime>` or `full=True`.
