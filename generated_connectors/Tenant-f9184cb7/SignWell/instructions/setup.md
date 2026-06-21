# SignWell connector — setup

## 1. Provision a SignWell account

1. Sign in to [SignWell](https://www.signwell.com/) and pick a plan that exposes API access (SignWell Business or higher).
2. Decide whether you want to develop against **test mode** (recommended) or production. Test-mode documents are free and do not consume signature credits.

## 2. Create an API key

1. In SignWell → **Settings** → **API** → **API Key**.
2. Click **New API Key** (or **Reveal** if one already exists).
3. Copy the key — you cannot view it again after closing the dialog.

## 3. Install the connector in Shielva

When the gateway prompts for install fields, supply:

| Field | Value |
|---|---|
| `api_key` | The key you copied above (stored as a secret) |
| `test_mode_default` | `true` for development, `false` for production |
| `base_url` | Leave blank to use `https://www.signwell.com/api/v1` |
| `rate_limit_per_min` | Leave blank to use the default of 100 |

The connector verifies the key by calling `GET /me` immediately after install — a 401 response means the key is invalid or has been revoked.

## 4. Verify

In Shielva, run **Health Check** on the connector. A healthy response prints the calling SignWell user's e-mail.

## 5. Production checklist

- Rotate the API key in SignWell after every team-member departure.
- Flip `test_mode_default` to `false` only when you are ready for billable signature events.
- Restrict the key to the minimum scope SignWell exposes for your account tier.
- Monitor `429 Too Many Requests` — the connector retries automatically with exponential backoff but sustained 429s indicate you need to raise the per-minute rate-limit configuration or contact SignWell support to increase your account quota.
