# Setup Instructions: Vonage

## Overview

The Vonage connector lets the Shielva platform send SMS, place voice calls, verify phone numbers via OTP, and manage the phone numbers owned by your Vonage account. Authentication uses an API key + secret pair issued by Vonage — there is no OAuth flow.

---

## Prerequisites

Before you begin, make sure you have:

- A **Vonage account** — sign up at [dashboard.nexmo.com](https://dashboard.nexmo.com) if you do not have one.
- The **API key** and **API secret** for that account (Dashboard → Getting Started).
- Sufficient **account balance** for any paid actions (SMS, voice, verify, number purchase). Free trial credit is usually enough for evaluation.

---

## Step-by-Step Configuration

### Step 1: API Key (`api_key`) — **Required**

1. Sign in to [dashboard.nexmo.com](https://dashboard.nexmo.com).
2. On the **Getting Started** card you'll see your **API key** — copy it.
3. Paste it into the **Vonage API Key** field in Shielva.

### Step 2: API Secret (`api_secret`) — **Required**

1. On the same **Getting Started** card, click the eye icon next to **API secret** and copy it.
2. Paste it into the **Vonage API Secret** field in Shielva. The secret is stored encrypted.

> **Common mistake:** Vonage lets you rotate the API secret. If you rotate it in the dashboard, you must also update this field in Shielva — the old secret stops working immediately.

### Step 3: REST Base URL (`rest_base_url`) — **Optional**

- **Default:** `https://rest.nexmo.com`
- Leave blank unless Vonage has assigned you a regional endpoint.

### Step 4: API Base URL (`api_base_url`) — **Optional**

- **Default:** `https://api.nexmo.com`
- Used by the Voice API (`/v1/calls`). Leave blank for the standard global endpoint.

### Step 5: Default Sender (`default_from`) — **Optional**

- Set this to a Vonage-purchased long number or an alphanumeric sender ID supported by the destination country.
- Some countries (US, Canada, India domestic) require a long code; others accept alphanumeric senders.

### Step 6: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default:** `30`
- Increase only if Vonage has explicitly granted your account a higher throughput tier.

---

## Testing the Connection

1. Click **Save** — the connector immediately calls `/account/get-balance` to validate the credentials.
2. The connector status badge should show **Connected** (green).
3. Click **Run Health Check** at any time to re-verify.
4. To test **Send SMS**, open **APIs → send_sms**, fill in `from_`, `to`, and `text`, and click **Run**. A response with `messages[0].status: "0"` is a success.
5. To test **Verify**, run **verify_request** with `number` and `brand`, wait for the OTP on the test phone, then run **verify_check** with the `request_id` returned in step 4 and the code received.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Invalid api_key or api_secret` | Wrong credentials or rotated secret | Re-copy both values from the Vonage dashboard |
| `Insufficient balance` on send | Account credit too low | Top up at [dashboard.nexmo.com → Billing](https://dashboard.nexmo.com/billing) |
| SMS `status: "15"` | Sender ID not allowed in destination country | Use a Vonage-purchased long number for that country |
| 429 errors during bursts | Hitting the per-minute quota | Lower `rate_limit_per_min` or request a higher tier from Vonage support |
| Voice call rejected | Neither `ncco` nor `answer_url` supplied | The connector requires exactly one — supply it in the action input |
| `verify_check` returns `status: "16"` | Wrong PIN entered | Have the user retry; after `next_event_wait` seconds Vonage will offer a new attempt |
