# Ramp Connector — Setup

The Shielva Ramp connector talks to the Ramp Developer API (corporate cards +
spend management) using the OAuth2 client-credentials grant.

## 1. Create a Ramp API App

1. Sign in to Ramp as a business admin.
2. Navigate to **Developer → API Apps → Create App**.
3. Pick the scopes your integration needs. The connector defaults to:
   `users:read users:write cards:read cards:write transactions:read
   transactions:write reimbursements:read bills:read departments:read
   locations:read`. Granting fewer scopes is fine — only the matching
   connector methods will work.
4. Save the app. Ramp displays the `client_id` and the `client_secret`. The
   `client_secret` is shown **once** — copy it somewhere safe before closing
   the dialog.

## 2. Install the connector in Shielva

1. Go to **Connectors → Add → Ramp** in the Shielva UI.
2. Paste the `client_id` and `client_secret` into the matching fields.
3. Leave the other fields at their defaults unless you operate against the
   Ramp sandbox (in which case override `base_url` and `token_url`).
4. Click **Install**. The connector mints an access token immediately and
   surfaces `connected` on success.

## 3. Verify

Hit the `health_check` API. A 200 response with `health: healthy` and
`auth_status: connected` means the connector is working.

## 4. Rate limits and idempotency

- Ramp enforces tenant-level rate limits. The connector defaults to **60
  requests/minute** and retries 429/5xx with exponential backoff.
- All POST endpoints accept an `Idempotency-Key` — pass a stable client-side
  UUID via the `idempotency_key` parameter when you want safe retries
  (`invite_user`, `create_card`, `terminate_card`).

## 5. Rotating the client secret

1. Create a fresh secret in Ramp → Developer → API Apps.
2. Update the connector config with the new `client_secret`.
3. The connector's in-memory token cache is invalidated automatically the
   next time the credentials are set; the first subsequent request will
   re-mint a token with the new secret.
