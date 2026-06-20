# Braintree Connector Setup

## Overview

The Braintree connector syncs payment data — transactions, customers, subscriptions, plans, and disputes — from your Braintree (PayPal) merchant account into Shielva.

Authentication uses HTTP Basic Auth: your **Public Key** as the username and **Private Key** as the password, plus a **Merchant ID** to scope all API calls.

---

## Step 1 — Find your API credentials

1. Log in to the [Braintree Control Panel](https://sandbox.braintreegateway.com) (sandbox) or [production Control Panel](https://www.braintreegateway.com).
2. Click your name in the top-right corner → **My User**.
3. Scroll to the **API Keys** section.
4. Note your **Merchant ID** (displayed at the top of the API Keys section).
5. Under **API Keys**, click **View** next to an existing key or **Generate New API Key** to create one.
6. Copy the **Public Key** and **Private Key** shown.

> The Private Key is only shown once at creation time. Store it securely before dismissing the dialog.

---

## Step 2 — Choose your environment

| Environment | Use for | API base URL |
|---|---|---|
| `sandbox` | Development and testing | `https://api.sandbox.braintreegateway.com/merchants/{merchant_id}/` |
| `production` | Live transactions | `https://api.braintreegateway.com/merchants/{merchant_id}/` |

Start with `sandbox` to verify connectivity before switching to `production`.

---

## Step 3 — Enter credentials in Shielva

Fill in the install form with:

| Field | Value |
|---|---|
| **Merchant ID** | From Control Panel → My User → API Keys |
| **Public Key** | From the API Keys section |
| **Private Key** | From the API Keys section (password field) |
| **Environment** | `sandbox` or `production` |

Click **Connect**. Shielva calls `GET /merchants/{merchant_id}` to verify the credentials. A green status indicator confirms a successful connection.

---

## Step 4 — Webhook setup (optional)

To receive real-time event notifications from Braintree:

1. In the Braintree Control Panel, go to **Settings** → **Webhooks**.
2. Click **Create New Webhook**.
3. Set the **Destination URL** to your Shielva webhook endpoint:
   `https://<your-shielva-domain>/api/v1/webhooks/braintree`
4. Select the event types you want to receive (e.g., `subscription_charged_successfully`, `transaction_settled`, `dispute_opened`).
5. Click **Create**.

Braintree sends a test notification to verify the URL is reachable.

---

## Resources synced

| Resource | Description |
|---|---|
| Transactions | Payments including amount, status, currency, and processor response |
| Customers | Customer records with payment methods and contact details |
| Subscriptions | Recurring billing subscriptions with status and pricing |
| Plans | Billing plan definitions with pricing and frequency |
| Disputes | Chargebacks and disputes with status and evidence |

---

## Troubleshooting

**401 Authentication Failed** — Verify your Public Key and Private Key are correct. Keys are environment-specific; sandbox keys will not work against the production API.

**Merchant ID not found** — Ensure the Merchant ID matches the environment. Sandbox and production accounts have different Merchant IDs.

**Rate limiting (429)** — Braintree enforces API rate limits. The connector retries automatically with exponential backoff. If you consistently hit limits, reduce sync frequency.

**Sandbox transactions not appearing** — In the sandbox environment, use Braintree's [test card numbers](https://developer.paypal.com/braintree/docs/reference/general/testing) to create test transactions before syncing.
