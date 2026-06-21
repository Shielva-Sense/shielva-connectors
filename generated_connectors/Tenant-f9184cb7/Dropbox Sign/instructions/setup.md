# Dropbox Sign Connector — Setup

The Dropbox Sign (formerly HelloSign) connector authenticates with a single
HTTP Basic API key. No OAuth flow required.

## 1. Generate an API key

1. Sign in at <https://app.hellosign.com/>.
2. Open **Settings → API**.
3. Click **Create API Key** and copy the generated value. Treat it like a
   password — it grants full access to your account's signature requests,
   templates, and team data.

## 2. (Optional) Create an API app for embedded signing

Only required if you intend to call `create_embedded_signature_request`:

1. From the same **Settings → API** screen, scroll to **API Apps** and click
   **Create API App**.
2. Set the **Domain** to the host that will render the embedded iframe.
3. Save and copy the **Client ID**.

## 3. Install the connector

In the Shielva platform, install **Dropbox Sign** and fill in:

| Field | Notes |
|-------|-------|
| `api_key` | The API key from step 1. |
| `client_id` | (Optional) The API app client ID from step 2. |
| `test_mode_default` | Default `true`. Set to `false` once you're ready to send legally binding requests. |
| `base_url` | Leave default (`https://api.hellosign.com/v3`) unless you've been told otherwise. |
| `rate_limit_per_min` | Default `60`. Lower if you share the key with other systems. |

On install the connector hits `GET /account`; a healthy install returns
`auth_status=connected`.

## 4. Verify

Hit the connector's `health_check` action — it must return
`{"health": "healthy", "auth_status": "connected"}`.

## Test mode vs. live mode

Dropbox Sign offers a free **test mode** that produces watermarked,
non-binding signatures. Every method on this connector forwards
`test_mode_default` unless the caller explicitly passes `test_mode=false`.
Flip the default to `false` only after you've validated the full workflow.

## Troubleshooting

- **401 / 403** → The API key is wrong, revoked, or lacks the requested
  permission. Generate a new key in the dashboard.
- **404 on a signature_request_id** → The ID belongs to a different account
  (each API key is scoped to a single account).
- **429** → The connector retries with exponential backoff up to three
  attempts. Persistent 429s mean you've hit the per-minute quota — raise
  `rate_limit_per_min` or stagger calls.
