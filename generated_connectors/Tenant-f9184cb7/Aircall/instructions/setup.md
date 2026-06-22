# Setup Instructions: Aircall

## Overview

The Aircall connector integrates your organization's Aircall (cloud telephony) account with the Shielva platform. Once connected, Shielva can list users / numbers / calls / contacts / teams / tags, place outbound calls, transfer or assign in-flight calls, and create CRM contacts on your behalf. The connector uses Aircall's HTTP Basic API-key authentication (`api_id` + `api_token`) — no OAuth flow is required.

This connector requires an active Aircall account with administrative access to **Settings → API Keys**.

---

## Prerequisites

Before you begin, make sure you have:

- An **Aircall account** with Admin or Owner role
- Access to **Aircall Dashboard → Settings → API Keys**
- A clear understanding that the API Token behaves like a password and is shown **only once** at creation time

---

## Step-by-Step Configuration

### Step 1: API ID (`api_id`) — **Required**

1. Sign in at [dashboard.aircall.io](https://dashboard.aircall.io).
2. Open **Settings → API Keys** (admin-only).
3. Click **Generate an API key** (or open an existing key).
4. Copy the **API ID** — it is the public half of your credential pair, safe to store in configuration files.
5. Paste the value into the **API ID** field in Shielva.

> **Tip:** The API ID is not a secret on its own, but rotate it together with the API Token if you suspect compromise.

---

### Step 2: API Token (`api_token`) — **Required**

1. On the same **API Keys** screen, copy the **API Token** that was shown when the key was generated.
2. Paste it into the **API Token** field in Shielva (stored encrypted at rest).
3. **If you have lost the original token**, generate a new key — Aircall does not display the token a second time.

> **Common mistake:** If you rotate the API token in Aircall, you must update it in Shielva — the old token immediately stops working and the connector will report `401 Unauthorized` on the next health check.

---

### Step 3: Aircall API Base URL (`base_url`) — **Optional**

- **Default value:** `https://api.aircall.io/v1`
- Leave blank unless you route Aircall traffic through an approved proxy or use a regional endpoint.

---

### Step 4: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `60`
- Aircall's standard quota is 60 requests per minute per API key.
- Lower this if Shielva must coexist with other consumers on the same key.

---

## Completing the Installation

After filling in the two required fields, click **Install** in the Shielva connector dashboard. Shielva will call `GET /ping` once with your credentials to verify them. On success the connector status changes to **Connected**; on a 401 you will see **Invalid Credentials** — re-check Steps 1 and 2.

---

## Testing the Connection

1. After install, the connector status badge should show **Connected** (green).
2. Click **Run Health Check** — a successful check confirms the credentials are valid.
3. Open **APIs → list_users** and click **Run** with no arguments — you should see your Aircall agents listed.
4. To test outbound calling, open **APIs → start_outbound_call**, supply a valid `user_id`, `number_id`, and an E.164 `to` number, then click **Run**. The selected agent's Aircall app rings within a few seconds.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on install or health check | Wrong `api_id` / `api_token` | Re-copy both from Aircall Dashboard → Settings → API Keys |
| `404 Not Found` on `start_outbound_call` | The `user_id` or `number_id` does not belong to your account | Use `list_users` / `list_numbers` first to retrieve valid IDs |
| `429 Rate limit exceeded` | More than 60 requests per minute | The connector automatically retries with exponential backoff; if it persists, lower `rate_limit_per_min` |
| `ValueError: 'to' is not a valid phone number` | Phone number is not in E.164 format | Use the leading `+` and country code, e.g. `+14155551234` |
| Connector shows **Missing Credentials** | `api_id` or `api_token` blank | Fill both required fields and click **Save** |
