# Plivo Connector — Setup

## 1. Obtain Plivo credentials

1. Sign in to the [Plivo Console](https://console.plivo.com/).
2. Navigate to **Account → Account Settings → Account**.
3. Copy the **Auth ID** (looks like `MAxxxxxxxxxxxxxxxxxx`).
4. Copy the **Auth Token** (treat as a password — never share or commit).

## 2. Install the connector through Shielva

When prompted by the Shielva connector installer, supply:

| Field | Required | Example | Notes |
|-------|----------|---------|-------|
| Auth ID | yes | `MAxxxxxxxxxxxxxxxxxx` | From Plivo Console → Account → Account ID |
| Auth Token | yes | `••••••••••••••••` | Stored as a secret; never logged |
| Base URL | no | `https://api.plivo.com/v1` | Override for Plivo regional endpoints if needed |
| Default Caller ID | no | `+14155550100` | Default sender number for SMS / outbound calls |
| Rate limit (requests/min) | no | `60` | Local throttle hint; Plivo enforces its own quotas |

The connector validates the credentials at install time and runs a health
check by calling `GET /Account/{auth_id}/` against Plivo. A 401 response
surfaces immediately so misconfigured tokens fail fast.

## 3. APIs available after install

Once installed, the following methods can be invoked through the Shielva
connector runtime:

- **Account** — `health_check`, `get_account`
- **Messaging** — `send_sms`, `get_message`, `list_messages`
- **Voice** — `make_call`, `get_call`, `list_calls`, `hangup_call`, `transfer_call`
- **Numbers** — `list_numbers`, `search_phone_numbers`, `buy_phone_number`
- **Applications** — `list_applications`, `create_application`

## 4. Production hardening

- Store `auth_token` only in the Shielva sealed-config envelope (per project
  security rules). Plaintext `.env` is dev-only.
- If you front Plivo with a private API gateway, override `base_url` to point
  at the gateway and keep the `/Account/{auth_id}` path convention intact.
- Configure Plivo webhook URLs (`answer_url`, `hangup_url`, `message_url`) so
  they target Shielva-managed endpoints; never expose internal hosts to the
  public internet without authentication.
