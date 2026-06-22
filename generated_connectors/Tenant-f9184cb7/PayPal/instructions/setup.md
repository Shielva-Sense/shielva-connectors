# PayPal Connector — Setup Guide

## Overview

The PayPal connector integrates Shielva with the [PayPal REST API v2](https://developer.paypal.com/api/rest/) to sync transactions, orders, and payment data. It uses OAuth2 **client credentials** authentication — no user login required.

---

## Prerequisites

- A PayPal Business account
- A PayPal REST app created in the [PayPal Developer Dashboard](https://developer.paypal.com/developer/applications/)
- Your app's **Client ID** and **Client Secret**

---

## Step 1 — Create a PayPal REST App

1. Log in to the [PayPal Developer Dashboard](https://developer.paypal.com/developer/applications/).
2. Click **Create App** under **My Apps & Credentials**.
3. Give your app a name (e.g. "Shielva Integration").
4. Select **Merchant** as the app type.
5. Click **Create App**.

You will be shown:
- **Client ID** — starts with `AeQ...` or `AYS...`
- **Client Secret** — click **Show** to reveal it

> **Tip:** The dashboard shows both Sandbox and Live credentials on the same page. Use **Sandbox** credentials for testing and **Live** credentials for production.

---

## Step 2 — Configure App Permissions

Under your new app, ensure the following features are enabled:

| Feature | Required for |
|---------|-------------|
| **Transaction Search** | `list_transactions`, `sync()` |
| **Reporting** | `get_balance()` |
| **Orders** | `get_order()` |
| **Payments** | `list_payments()` |

---

## Step 3 — Install the Connector in Shielva ACP

1. Navigate to **ACP → Integrations → PayPal**.
2. Click **Connect**.
3. Fill in the install fields:

| Field | Value | Notes |
|-------|-------|-------|
| **Client ID** | From PayPal Developer Dashboard | Required |
| **Client Secret** | From PayPal Developer Dashboard | Required, stored encrypted |
| **Sandbox Mode** | `true` or leave empty | `true` = sandbox, empty = live |

4. Click **Install**.

The connector will POST to `https://api-m.paypal.com/v1/oauth2/token` (or the sandbox URL) to validate credentials. On success, status changes to **Online**.

---

## Step 4 — Verify the Connection

After installation, run a health check:

```python
result = await connector.health_check()
# Expected: health=HEALTHY, auth_status=CONNECTED
```

Or from the ACP UI: **Integrations → PayPal → Health Check**.

---

## API Reference

### Authentication

All API calls use OAuth2 **client credentials** flow:

```
POST /v1/oauth2/token
Authorization: Basic base64(client_id:client_secret)
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
```

The connector acquires tokens automatically and refreshes them 60 seconds before expiry.

---

### Available Methods

#### `install()`
Validates credentials by acquiring an OAuth2 token.

**Returns:** `InstallResult` with `health`, `auth_status`, `connector_id`, `message`.

---

#### `health_check()`
Verifies credentials are still valid by re-acquiring a token.

**Returns:** `HealthCheckResult` with `health`, `auth_status`, `message`.

---

#### `sync(full=False, since=None, kb_id="")`
Syncs PayPal transactions into the Shielva knowledge base.

- `full=True` → fetches last 31 days of transactions
- `since=<datetime>` → fetches transactions after that timestamp
- `kb_id` → knowledge base to ingest into (empty = dry run)

**Returns:** `SyncResult` with `status`, `documents_found`, `documents_synced`, `documents_failed`.

---

#### `list_transactions(start_date, end_date, page=1, page_size=100)`
Retrieve a paginated list of transactions within a date range.

```python
txns = await connector.list_transactions(
    start_date="2024-01-01T00:00:00Z",
    end_date="2024-01-31T23:59:59Z",
    page=1,
    page_size=100,
)
# Returns: {"transaction_details": [...], "total_pages": N, "total_items": N}
```

**PayPal endpoint:** `GET /v1/reporting/transactions`

---

#### `get_order(order_id)`
Retrieve a single PayPal order by ID.

```python
order = await connector.get_order("5O190127TN364715T")
# Returns: {"id": "...", "status": "COMPLETED", "purchase_units": [...], ...}
```

**PayPal endpoint:** `GET /v2/checkout/orders/{order_id}`

---

#### `list_payments(page_size=20, page=1)`
Retrieve a paginated list of PayPal payment records.

```python
payments = await connector.list_payments(page_size=20, page=1)
# Returns: {"payments": [...], "count": N}
```

**PayPal endpoint:** `GET /v1/payments/payment`

---

#### `get_balance()`
Retrieve the current PayPal account balances.

```python
balance = await connector.get_balance()
# Returns: {"balances": [{"currency": "USD", "total_balance": {"value": "1000.00"}}]}
```

**PayPal endpoint:** `GET /v1/reporting/balances`

---

## Error Handling

| Exception | When raised | Retried? |
|-----------|-------------|----------|
| `PayPalInvalidCredentialsError` | 401 — wrong client_id/secret | No |
| `PayPalAuthError` | 401/403 — auth failure | No |
| `PayPalRateLimitError` | 429 — too many requests | Yes (Retry-After) |
| `PayPalServerError` | 5xx — PayPal service error | Yes (3x backoff) |
| `PayPalNetworkError` | Timeout / connection error | Yes (3x backoff) |
| `PayPalNotFoundError` | 404 — resource not found | No |
| `PayPalTokenError` | Token acquisition failed | No |

The connector applies exponential backoff (base 1s, factor 2.0x, jitter 0.5s, max 30s) for retriable errors and a circuit breaker (threshold: 5 failures, recovery: 60s).

---

## Sandbox vs Live

| Setting | Base URL |
|---------|----------|
| Sandbox (`sandbox=true`) | `https://api-m.sandbox.paypal.com` |
| Live (default) | `https://api-m.paypal.com` |

Always develop against the sandbox environment first. Sandbox credentials from the PayPal Developer Dashboard are separate from your live credentials.

---

## Security

- Client Secret is stored encrypted in the Shielva vault and never logged.
- OAuth2 tokens are held in process memory only; they are not persisted to disk.
- Token refresh happens automatically 60 seconds before expiry.

---

## Troubleshooting

### `auth_status=INVALID_CREDENTIALS` after install

**Cause:** Wrong Client ID or Client Secret, or sandbox/live credentials mixed up.

**Fix:** Double-check credentials in the PayPal Developer Dashboard. Ensure you copy from the correct environment tab (Sandbox vs Live). If using sandbox, set `sandbox=true` in install fields.

---

### `health=DEGRADED` in health check

**Cause:** Transient network error or PayPal API degradation.

**Fix:** Check [PayPal Status](https://www.paypalobjects.com/digitalassets/c/website/logo/full-text/pp_fc_hl.svg) for outages. The connector will retry automatically.

---

### `list_transactions` returns empty

**Cause:** No transactions in the specified date range, or the PayPal account has Transaction Search disabled.

**Fix:** Verify the date range contains actual transactions. In the PayPal Developer Dashboard under your app, ensure **Transaction Search** is enabled under **Features**.

---

### Token expires during long sync

**Cause:** Sync took longer than the token TTL (default 9 hours).

**Fix:** The connector refreshes the token automatically before each API call when it detects the token is within 60 seconds of expiry. For very long syncs, the token is refreshed mid-operation transparently.
