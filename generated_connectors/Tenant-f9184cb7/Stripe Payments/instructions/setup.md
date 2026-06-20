# Stripe Payments Documentation

## Overview

The **Stripe Payments** connector integrates Shielva with the [Stripe API](https://stripe.com/docs/api) to provide full access to Stripe's payments infrastructure.

Stripe is the gold-standard payments platform used by millions of businesses to accept and manage online payments. This connector exposes **28 APIs** across 9 resource domains — Balances, Customers, Charges, Payment Intents, Subscriptions, Products & Prices, Invoices, Refunds, Events, and Webhooks — enabling real-time payment data ingestion, customer management, and financial reporting inside Shielva workflows.

**Key capabilities:**
- Query live account balances and transaction history
- Full CRUD for customers, subscriptions, and invoices
- Create and manage payment intents and refunds
- Subscribe to Stripe events via webhook management
- Circuit-breaker and retry logic for production resilience
- All data normalized to Shielva's canonical event and customer schemas

## Quick Start

Get the Stripe Payments connector running in under 5 minutes.

### 1. Obtain a Stripe API Key

1. Log in to the [Stripe Dashboard](https://dashboard.stripe.com)
2. Go to **Developers → API keys**
3. Copy your **Secret key** — it starts with `sk_test_` (test mode) or `sk_live_` (production)

> **Tip:** Always use `sk_test_...` during development. The connector works identically in both modes.

### 2. Install the Connector

In the Shielva ACP:
1. Navigate to **Integrations → Stripe**
2. Click **Connect**
3. Enter your Secret API Key in the `api_key` field
4. Click **Install**

The connector will verify the key against `GET https://api.stripe.com/v1/balance` and set status to `ONLINE` on success.

### 3. Verify

```python
# Health check — should return status=ONLINE
result = connector.health_check()
assert result['health'] == 'ONLINE'
```

### 4. Run Your First Sync

```python
# Incremental sync — fetches all events since last run
events = await connector.sync(config={}, state={"last_sync": "2026-01-01T00:00:00Z"})
print(f"Ingested {len(events)} records")
```

## Authentication

The Stripe Payments connector uses **API Key authentication** — specifically Stripe's Secret API Key passed as an HTTP Bearer token.

### Credential

| Field | Key | Type | Required | Description |
|-------|-----|------|----------|-------------|
| Secret API Key | `api_key` | password | Yes | Stripe secret key (`sk_test_...` or `sk_live_...`) |

### How it works

Every request to the Stripe API includes the key in the `Authorization` header:

```
Authorization: Bearer sk_test_4eC39HqLyjWDarjtT7pr...
```

The connector's `StripeHTTPClient` injects this header automatically — no manual header management needed.

### Key Prefixes

| Prefix | Environment | Use |
|--------|-------------|-----|
| `sk_test_` | Test | Development & CI — no real charges |
| `sk_live_` | Production | Real money — protect carefully |
| `rk_test_` / `rk_live_` | Restricted | Scoped keys — not recommended for this connector |

### Install Validation

During `install()`, the connector calls `GET /v1/balance`. A `401` response sets `auth_status=INVALID_CREDENTIALS` and `health=OFFLINE`. Any other error sets `health=DEGRADED`.

### Security

- The key is stored encrypted in the Shielva vault and never logged
- Use the minimum-scope key needed (full secret key is required for write operations)
- Rotate keys in the Stripe Dashboard under Developers → API keys → Roll key

## Configuration

The Stripe Payments connector has minimal configuration — the only required field is the API key.

### Install Fields

```json
{
  "api_key": "sk_test_4eC39HqLyjWDarjtT7pr..."
}
```

### Internal Defaults

These are set inside the connector and do not need to be configured:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `BASE_URL` | `https://api.stripe.com/v1` | Stripe API base URL |
| `API_VERSION` | `2023-10-16` | Stripe API version (sent as `Stripe-Version` header) |
| `PAGE_SIZE` | `100` | Results per page for list operations |
| `TIMEOUT` | `30s` | HTTP request timeout |
| `MAX_RETRIES` | `3` | Retry attempts on transient failures |
| `RETRY_BACKOFF` | Exponential | 1s → 2s → 4s with jitter |

### Circuit Breaker

The connector uses a circuit breaker pattern:

| State | Behavior |
|-------|----------|
| CLOSED | Normal operation |
| OPEN | After 5 consecutive failures — all calls raise `StripeCircuitOpenError` |
| HALF_OPEN | After 60s cooldown — one probe request allowed; success closes, failure reopens |

## API Methods

The connector exposes 28 API methods across 9 resource domains. All list methods support automatic pagination.

## Sync & Incremental Ingestion

The `sync()` method implements incremental event ingestion using Stripe's event log.

### How sync works

1. Reads `state.last_sync` (ISO 8601 timestamp) from the stored connector state
2. Calls `GET /v1/events` with `created[gte]={last_sync_epoch}` and `limit=100`
3. Follows the `has_more` cursor for pagination
4. Normalizes each event through `StripeNormalizer.normalize_event()`
5. Calls `ingest_batch()` to write normalized records to Shielva storage
6. Updates `state.last_sync` to the current timestamp

### Usage

```python
# First run — fetches all events
result = await connector.sync(config={}, state={})

# Subsequent runs — only fetches new events since last sync
result = await connector.sync(config={}, state={"last_sync": "2026-06-01T00:00:00Z"})

print(result["records_ingested"])  # number of events processed
print(result["state"])             # updated state for next run
```

### Normalized Event Schema

All Stripe events are normalized to:

| Field | Source | Description |
|-------|--------|-------------|
| `id` | `event.id` | Stripe event ID (`evt_...`) |
| `type` | `event.type` | Event type (e.g. `payment_intent.succeeded`) |
| `created` | `event.created` | Unix timestamp |
| `livemode` | `event.livemode` | `true` for production events |
| `api_version` | `event.api_version` | Stripe API version used |
| `data` | `event.data.object` | The affected resource |

### Recommended Schedule

Run sync every 5–15 minutes for near-real-time data. The Stripe event log retains events for 30 days.

## Error Handling

The connector raises typed exceptions from `exceptions.py` for all error conditions.

### Exception Hierarchy

```
StripeError (base)
├── StripeAuthError          — 401: invalid or missing API key
├── StripeRateLimitError     — 429: too many requests
├── StripeNotFoundError      — 404: resource not found
├── StripeServerError        — 5xx: Stripe service error
├── StripeValidationError    — 400: invalid request parameters
└── StripeCircuitOpenError   — circuit breaker open (no request sent)
```

### Retry Behavior

| Error type | Retried? | Strategy |
|-----------|----------|----------|
| `StripeRateLimitError` (429) | Yes | Exponential backoff, up to 3 attempts |
| `StripeServerError` (5xx) | Yes | Exponential backoff, up to 3 attempts |
| `StripeAuthError` (401) | No | Immediate failure — check API key |
| `StripeNotFoundError` (404) | No | Immediate failure — check resource ID |
| `StripeValidationError` (400) | No | Immediate failure — fix request params |

### Circuit Breaker

After 5 consecutive failures (of any type), the circuit opens:
```python
try:
    result = await connector.get_balance()
except StripeCircuitOpenError:
    # Circuit is open — wait 60s before retrying
    print("Stripe connector circuit open — backing off")
```

The circuit automatically enters HALF_OPEN after 60 seconds and allows one probe request.

## Troubleshooting

Common issues and their fixes.

### `health=OFFLINE` after install

**Cause:** Invalid or expired API key.

**Fix:** Verify the key in the [Stripe Dashboard](https://dashboard.stripe.com/apikeys). Ensure you're using the *secret* key (`sk_...`), not the publishable key (`pk_...`). Re-run install with the correct key.

---

### `StripeAuthError: 401 Unauthorized`

**Cause:** The stored API key was rotated or revoked in Stripe.

**Fix:** Generate a new key in the Stripe Dashboard → Developers → API keys → Create secret key. Update the connector via ACP → Integrations → Stripe → Edit credentials.

---

### `StripeRateLimitError: 429 Too Many Requests`

**Cause:** Stripe's default rate limit is ~100 read requests/second per account.

**Fix:** The connector retries automatically with exponential backoff. If this is persistent, reduce sync frequency or contact Stripe to increase your rate limits.

---

### `StripeCircuitOpenError` in logs

**Cause:** 5 consecutive failures caused the circuit breaker to open.

**Fix:** The circuit auto-resets after 60 seconds. Check the Stripe Status page (https://status.stripe.com) for outages. Check the Terminal logs in SAD for the underlying error before the circuit opened.

---

### Unit tests fail with `ModuleNotFoundError: shared`

**Cause:** `sys.path` not set correctly for the test environment.

**Fix:** The root `conftest.py` adds the connector directory to `sys.path`. Ensure you run tests from the connector root:
```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/stripe_payments_connector
pytest tests/test_connector.py -v
```

---

### Sync returns 0 records on first run

**Cause:** No events exist in the Stripe account (new or empty test account).

**Fix:** Create a test payment in the Stripe Dashboard, or use the Stripe CLI to trigger events:
```bash
stripe trigger payment_intent.succeeded
```

---

### `asyncio_mode` warning in pytest

**Cause:** `pytest-asyncio` version mismatch.

**Fix:** Ensure `pytest.ini` sets `asyncio_mode = auto` (already configured). Upgrade if needed:
```bash
pip install pytest-asyncio --upgrade
```

## Data Models

Key Python models used by the connector (`models.py`).

### `StripeCustomer`

```python
@dataclass
class StripeCustomer:
    id: str           # cus_...
    email: str
    name: str
    created: int      # Unix timestamp
    metadata: dict
    livemode: bool
```

### `StripeEvent`

```python
@dataclass
class StripeEvent:
    id: str           # evt_...
    type: str         # e.g. "payment_intent.succeeded"
    created: int
    livemode: bool
    api_version: str
    data: dict        # event.data.object
```

### Normalizer

The `StripeNormalizer` class in `helpers/normalizer.py` converts raw Stripe API responses to these models:

```python
normalizer = StripeNormalizer()
customer = normalizer.normalize_customer(raw_api_response)
event = normalizer.normalize_event(raw_event)
```

## Testing

The connector ships with a full test suite.

### Unit Tests (38 tests, zero I/O)

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/stripe_payments_connector
pytest tests/test_connector.py -v
# Expected: 38 passed in < 5 seconds
```

All HTTP calls are mocked via `AsyncMock` — no Stripe account required.

**Coverage:**
- `install()` — success, missing key, invalid key (401), circuit open
- `health_check()` — success, invalid key, 5xx error
- `sync()` — empty, 2 events, pagination, partial failure
- Balance, Customers (CRUD), Charges, Payment Intents, Subscriptions
- Refunds (partial + full), Events, Webhooks
- Circuit breaker state machine
- Normalizer field validation

### Integration Tests (real Stripe API)

```bash
export STRIPE_API_KEY="sk_test_..."
pytest tests/test_integration.py -v
```

Tests skip gracefully when `STRIPE_API_KEY` is not set.

**Write-gated tests** (create/delete operations) require:
```bash
export STRIPE_ALLOW_WRITES="true"
```
