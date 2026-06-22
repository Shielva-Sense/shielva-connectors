# Bandwidth Connector — Setup Guide

This connector talks to three Bandwidth APIs over HTTP Basic auth:

| Surface | What you get |
|---|---|
| Messaging | Send and receive SMS / MMS, manage media |
| Voice | Place outbound calls, manage recordings |
| Numbers / Dashboard | List applications, phone-number orders |

## 1. Provision an API user in the Bandwidth Dashboard

1. Sign in to **https://dashboard.bandwidth.com**.
2. Navigate to **Account → API Credentials**.
3. Click **Add new API user**, give it a name, and select the permissions you need (Messaging Read+Write, Voice Read+Write, Dashboard Read).
4. Record the **username** and the **password** shown once at creation time — Bandwidth will not show the password again.
5. From **Account → Account Settings**, copy the numeric **Account ID** (e.g. `5000123`).

## 2. Install the connector

In ACP → **Manage Connectors** → choose `Bandwidth`. Fill in:

| Field | Where to find it |
|---|---|
| **Account ID** | Dashboard → Account Settings (numeric) |
| **API Username** | The user you provisioned in §1 |
| **API Password** | The password shown once at creation in §1 |
| **Webhook Signing Secret** *(optional)* | Whatever HMAC secret you configure on your Bandwidth application's callbacks. If omitted, incoming webhooks are accepted but flagged `unverified`. |
| **HTTP Timeout (seconds)** *(optional)* | Default 60s. Increase for low-bandwidth networks. |

Click **Install**. The connector probes `GET /accounts/{accountId}/applications`. If credentials are good you'll see status **Connected**.

## 3. Configure webhooks (only if you want inbound events)

In the Bandwidth dashboard for the application you intend to use:

1. Set **Messaging Callback URL** to `https://<your-acp>/webhooks/bandwidth/messaging/{tenant_id}/{connector_id}`
2. Set **Voice Callback URL** to `https://<your-acp>/webhooks/bandwidth/voice/{tenant_id}/{connector_id}`
3. Set the **Callback HMAC secret** to the same value you entered in **Webhook Signing Secret** at install.
4. Save.

The connector routes these event types out of the box:

| `eventType` | Surface | Handler |
|---|---|---|
| `message-received` | Messaging | inbound SMS / MMS |
| `message-delivered` | Messaging | delivery receipt |
| `message-failed` | Messaging | delivery failure |
| `bridge-complete` | Voice | bridge ended |
| `recording-available` | Voice | recording is ready to download |

## 4. Verify

Use ACP's **Test** tab on the connector to run any method. Recommended quick checks:

- `list_applications` — should return your application list
- `list_phone_numbers` — should return your phone-number orders
- `send_message` with a small text payload → returns a message id

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `MISSING_CREDENTIALS` at install | One of account_id / username / password is blank | Re-enter all three; password must be the one Bandwidth showed at user creation, not your dashboard password |
| `INVALID_CREDENTIALS` at install | Username / password mismatch or API user lacks Dashboard read | Confirm the API user has at least Dashboard Read; rotate the API password if you're unsure |
| `BandwidthAuthError: 403` on a method | API user lacks permission on the surface (Messaging / Voice / Dashboard) | Grant the missing permission in **Account → API Credentials** |
| 429 backoff loops | You're hitting Bandwidth's per-account rate limit | The connector already honours `Retry-After`; lower your call frequency or split across applications |
| Webhooks land but `verified: false` | `webhook_secret` doesn't match the Callback HMAC secret in the Bandwidth dashboard | Re-enter the same secret in both places |
