# Brex Connector — Setup

## 1. Provision an API token

1. Sign in to the [Brex dashboard](https://dashboard.brex.com/).
2. Navigate to **Developer → API Tokens**.
3. Click **Create token** and grant the scopes needed for the endpoints you plan to use (Users, Cards, Expenses, Transactions, Budgets).
4. Copy the token — it is shown only once.

## 2. Install the connector

In the Shielva ARC Connector Catalog, install **Brex** and fill in:

| Field | Required | Notes |
|-------|----------|-------|
| `api_token` | yes | Bearer token from step 1. |
| `base_url` | no | Defaults to `https://platform.brexapis.com`. |
| `default_account_id` | no | Cash account ID used when `list_transactions` is called without one. |
| `rate_limit_per_min` | no | Default `60`. Brex enforces per-token limits; lower if you see 429s. |

## 3. Smoke-test

```python
from connector import BrexConnector

c = BrexConnector(
    tenant_id="my-tenant",
    connector_id="brex-1",
    config={"api_token": "<token>"},
)
print(await c.health_check())
print(await c.get_current_user())
```

A `ConnectorStatus(health=HEALTHY, auth_status=CONNECTED)` confirms the token works.

## 4. Surface area

15 async methods:

- `health_check`, `get_current_user`
- `list_users`, `get_user`
- `list_accounts`, `list_transactions`, `list_card_transactions`
- `list_cards`, `get_card`, `create_card`, `terminate_card`
- `list_expenses`, `get_expense`, `update_expense`
- `list_budgets`

All return raw API dicts (or `ConnectorStatus`/`SyncResult` for the lifecycle methods). `sync()` ingests expenses into the Shielva KB.
