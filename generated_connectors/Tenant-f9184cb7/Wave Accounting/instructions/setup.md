# Wave Accounting Connector — Setup

## 1. Register a developer application

1. Sign in at <https://developer.waveapps.com>.
2. Click **Manage Applications → Create application**.
3. Set a **Name**, **Description**, and the **Redirect URI** that your Shielva
   tenant will redirect to after consent (must match exactly — Wave is strict
   on trailing slashes).
4. Submit. Wave will issue a **Client ID** and **Client Secret** — copy both.

## 2. Choose scopes

The connector requests the following scopes by default. Trim them only if your
tenant doesn't need a given capability:

```
user.profile:read
business:read business.write
customer:read customer.write
invoice:read invoice.write
product:read product.write
account:read
transaction:read
```

## 3. Install the connector in Shielva

In the Shielva connector hub:

1. Pick **Wave Accounting** from the marketplace.
2. Paste **Client ID** and **Client Secret** from step 1.
3. (Optional) Override **Redirect URI**, **scopes**, or the GraphQL/auth URLs.
4. Click **Install** — this validates the credentials and moves the connector
   into `PENDING` auth status.

## 4. Authorize

1. Click **Connect** on the installed connector. Shielva will open Wave's
   consent screen at `https://api.waveapps.com/oauth2/authorize`.
2. Sign into Wave, pick the businesses to grant access to, click **Authorize**.
3. Wave will redirect to your configured Redirect URI with a `code` query
   parameter. Shielva exchanges this code for an access + refresh token at
   `https://api.waveapps.com/oauth2/token` and stores both.

The connector is now in `CONNECTED` state.

## 5. Health check + first calls

- The **Health check** action runs the trivial GraphQL query
  `{ user { id } }` against `https://gql.waveapps.com/graphql/public` to
  confirm the token is live.
- Use **List Businesses** first — every other call needs a `business_id`.
- All write paths (`create_customer`, `update_customer`, `create_invoice`,
  `send_invoice`, `create_product`) return Wave's `didSucceed` flag plus an
  `inputErrors[]` array if the mutation failed validation.

## 6. Rate limits

Wave documents 60 requests per minute per access token on the public GraphQL
endpoint. The connector's `rate_limit_per_min` field defaults to 60. 429
responses are retried with exponential backoff (1s, 2s, 4s, 8s, 16s, capped at
32s) by `helpers/utils.with_retry`.

## 7. Token refresh

The HTTP client retries any GraphQL call once on HTTP 401 after asking
`connector.on_token_refresh()` for a fresh access token via the OAuth refresh
grant. If Wave rejects the refresh token, the connector transitions to
`EXPIRED` and the operator must re-authorize.
