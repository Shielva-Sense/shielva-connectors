# Setup Instructions: Kommo (formerly amoCRM)

## Overview

The Kommo connector integrates your Kommo CRM account with the Shielva platform
via the Kommo REST API (v4). Once connected, Shielva can read and manage
leads, contacts, companies, customers, tasks, events, notes, custom fields,
pipelines, users, and outbound webhooks.

This connector authenticates with a **long-lived OAuth access token** treated
as an API key. The token is sent as `Authorization: Bearer <token>`. The
connector base URL is computed per tenant from the Kommo **subdomain**.

---

## Prerequisites

- A **Kommo account** (your URL looks like `https://mycompany.kommo.com`).
- Administrator access to **Settings → Integrations**.
- Permission to create a **private integration** and generate a long-lived
  access token.

---

## Step 1: Kommo Subdomain (`subdomain`) — **Required**

1. Open Kommo in your browser.
2. Note the prefix of the URL — in `https://mycompany.kommo.com` the
   subdomain is `mycompany`.
3. Paste **only the subdomain** (or the full URL — both are accepted) into
   the **Kommo Subdomain** field in Shielva. The connector normalises both.

---

## Step 2: Long-Lived Access Token (`access_token`) — **Required**

1. In Kommo, open **Settings → Integrations**.
2. Click **Create Integration → Private Integration** (or open an existing
   private integration).
3. Configure the integration — give it a name (e.g. `Shielva`) and an optional
   description.
4. Under **Access Rights**, tick the read/write rights your team needs
   (leads, contacts, companies, tasks, etc.). The minimum is "read leads"
   for the health check to pass.
5. Save the integration.
6. Open the **Keys and Scopes** tab on the saved integration.
7. Click **Generate Long-Lived Access Token**, copy the value — Kommo shows
   it only once. Treat it as a password.
8. Paste it into the **Long-Lived Access Token** field in Shielva.

> **Important:** the connector sends `Authorization: Bearer <token>` on every
> request. The token is the only secret stored — there is no client_id or
> client_secret to manage, no OAuth code-exchange step, and no refresh token.

---

## Step 3: (Optional) API Base URL (`base_url`)

Leave **blank** in 99% of cases. The connector automatically computes
`https://{subdomain}.kommo.com/api/v4` from the subdomain you entered.

Override only if you front Kommo behind a corporate proxy or are testing
against a sandbox at a non-standard host.

---

## Step 4: (Optional) Rate Limit + Timeout

- `rate_limit_per_min` — soft per-minute cap (default 100). Kommo enforces
  ~7 requests/sec server-side; the client retries 429 with exponential
  backoff regardless.
- `timeout_s` — per-request httpx timeout in seconds (default 30).

---

## Step 5: Install + Verify

1. Click **Install** in Shielva.
2. Watch for `ConnectorStatus(HEALTHY, AUTHENTICATED)` — the install hook only
   validates config; it does NOT call Kommo.
3. The gateway then calls `health_check()`, which probes `GET /api/v4/account`.
   - Success → `HEALTHY + CONNECTED`.
   - 401 → `OFFLINE + TOKEN_EXPIRED` (regenerate the token).
   - 403 → `UNHEALTHY + INVALID_CREDENTIALS` (token lacks scope — re-tick
     the access rights in Kommo and re-generate the token).
   - 429 → `DEGRADED + CONNECTED` (you're being throttled; lower request rate).

---

## Step 6: First sync

Once installed, the gateway can call `sync(full=True)` to seed your knowledge
base with all Kommo leads. Subsequent syncs use the `updated_at` cursor stored
via `set_metadata("last_lead_updated_at")` so only modified leads are pulled.

---

## Rotating the token

1. Open the integration in Kommo → **Keys and Scopes**.
2. Generate a new long-lived access token.
3. In Shielva, re-open the Kommo connector install form, paste the new token,
   and **Save**.
4. The next `health_check()` will succeed and the previous token can be
   revoked safely.

---

## Troubleshooting

See the in-app documentation (`.shielva/docs/connector_docs.json → troubleshooting`)
for the full table of error symptoms and fixes.
