# GoHighLevel Connector — Setup

This connector talks to the **GoHighLevel (LeadConnector) v2 REST API** at
`https://services.leadconnectorhq.com`. It authenticates via OAuth2
authorization-code flow against a **Marketplace App** that you register in the
GoHighLevel developer portal.

## 1. Register a Marketplace App

1. Visit <https://marketplace.gohighlevel.com/> and sign in with the GoHighLevel
   developer / agency account that will own this integration.
2. Open **My Apps** → **Create App**. Choose:
   - **Distribution type:** Private (single agency) or Public (marketplace), to
     match how you intend to roll this connector out.
   - **App type:** Standard.
3. Under **Settings → Client Keys**, copy the **Client ID** and **Client
   Secret**. You will paste these into Shielva in step 4.

## 2. Configure the Redirect URI

Under **Settings → Redirect URLs**, add the exact URL that your Shielva gateway
will receive the OAuth callback on. The gateway-canonical value in local dev is:

```
https://localhost:8000/connectors/oauth/callback
```

In production, replace `https://localhost:8000` with the public gateway origin.
The URI must match what you enter in Shielva (step 4) byte-for-byte — GHL
rejects mismatches with `redirect_uri_mismatch` and no error body.

## 3. Select Scopes

Under **Settings → Scopes**, enable at least the scopes Shielva ships with by
default. Each one maps to a connector capability:

| Scope                       | Used by                                              |
|-----------------------------|------------------------------------------------------|
| `contacts.readonly`         | `list_contacts`, `get_contact`                       |
| `contacts.write`            | `create_contact`, `update_contact`, `delete_contact` |
| `opportunities.readonly`    | `list_opportunities`, `list_pipelines`               |
| `opportunities.write`       | `create_opportunity`                                 |
| `calendars.readonly`        | `list_calendars`                                     |
| `conversations.write`       | `send_sms`, `send_email`, `create_appointment`       |
| `locations.readonly`        | `health_check` (fetches `/locations/{id}`)           |

You can request fewer scopes if you only need a subset of the connector's
capabilities — just remove the corresponding tokens from the **OAuth2 Scopes**
field in step 4.

## 4. Connect from Shielva

1. In the Shielva ACP UI, open **Connectors → Marketplace → GoHighLevel** and
   click **Install**.
2. Fill in:
   - **Marketplace App Client ID** — from step 1.
   - **Marketplace App Client Secret** — from step 1.
   - **OAuth2 Scopes** — leave the default unless you intentionally requested
     fewer scopes in step 3.
   - **OAuth2 Redirect URI** — must match step 2 exactly.
3. Click **Save** to persist the credentials. Shielva will validate that both
   credentials are present (this step does NOT touch the network).
4. Click **Connect**. You will be redirected to
   `https://marketplace.gohighlevel.com/oauth/chooselocation`, where you pick
   the **GHL location** (sub-account) this connector should operate against.
5. After consent, the gateway exchanges the code for an access token + refresh
   token at `https://services.leadconnectorhq.com/oauth/token` and stashes the
   returned `locationId` on the connector. The Connectors page should now show
   the connector as **Healthy**.

## 5. Verify

Run **Health Check** from the connector detail page. It performs a
`GET /locations/{location_id}` and returns `Healthy` on a 2xx response. If
health check fails:

- **`Auth error: …`** → the refresh token is no longer valid. Click **Connect**
  again to redo the consent flow.
- **`HTTP 401 …`** without auto-refresh → the GHL marketplace app credentials
  changed. Re-paste the Client Secret in step 4 and retry.
- **Transport error …** → network/DNS issue between the gateway and
  `services.leadconnectorhq.com`. Check the gateway's egress allowlist.

## Notes

- The connector sends `Version: 2021-07-28` on every request — this is the
  current GHL v2 API pin and the only value GHL accepts for these endpoints.
- Tokens auto-refresh on the first 401; you do not need to schedule a refresh
  job. The refresh token returned by GHL rotates on each refresh, so don't copy
  it out for external use.
- The client throttles itself to `rate_limit_per_min` requests (default 100,
  matching GHL's per-location quota). 429s are retried with exponential backoff,
  honoring `Retry-After`.
