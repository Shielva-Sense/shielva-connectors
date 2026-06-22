# Mercury Connector — Setup

The Shielva Mercury connector talks to the Mercury Business Banking REST API at
`https://api.mercury.com/api/v1` using a static Bearer API token.

## 1. Mint an API token

1. Sign in to Mercury at <https://app.mercury.com>.
2. Go to **Settings → API Tokens** (Owner / Admin only).
3. Click **Generate token**. Choose scope:
   - **Read-only** — sufficient for accounts, balances, transactions, cards,
     recipients, statements.
   - **Read-write** — required for `request_ach_transfer`, `request_send_money`,
     `create_recipient`, `update_recipient`.
4. Copy the token value once — Mercury does not show it again.

## 2. Install the connector

In the Shielva connector marketplace, install **Mercury** and paste the token
into the **Mercury API Token** field. Optional fields:

| Field                | Default                              | Use when |
|----------------------|--------------------------------------|----------|
| `default_account_id` | (empty)                              | You want `sync()` and convenience helpers to default to a single account |
| `base_url`           | `https://api.mercury.com/api/v1`     | You're testing against a Mercury sandbox / region override |
| `rate_limit_per_min` | `60`                                 | Tune the client-side soft rate limit |

The connector's `install()` step calls `GET /accounts` to verify the token
before marking the install as healthy.

## 3. Money-movement requires an Idempotency-Key

Mercury rejects `POST /account/{id}/transactions` without an `Idempotency-Key`
header. The connector enforces this client-side too — both `request_ach_transfer`
and `request_send_money` raise `MercuryError` if the caller passes an empty
key.

Generate a stable key per logical retry bucket:

```python
key = connector.new_idempotency_key("payroll-2026-06-21")
await connector.request_ach_transfer(
    account_id="acc_xxx",
    recipient_id="rec_yyy",
    amount=2500.00,
    idempotency_key=key,
    note="Payroll run 2026-06-21",
)
```

## 4. Local development

```bash
cd mercury_connector
pip install -r requirements.txt
pytest -q
```

The test suite uses `respx` to mock every Mercury API call — no real network
access is performed and no real token is required.
