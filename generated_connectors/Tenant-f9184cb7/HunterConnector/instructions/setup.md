# Hunter.io Connector — Setup

## 1. Obtain an API key

1. Sign up or log in at <https://hunter.io>.
2. Navigate to **API → API Key**.
3. Copy the API key shown on that page.

## 2. Install the connector in Shielva

1. Open **Connectors** in the Shielva ARC UI.
2. Select **Hunter.io** from the catalogue.
3. Paste the API key into the `api_key` field.
4. Leave `base_url` and `rate_limit_per_min` blank to accept the defaults
   (`https://api.hunter.io/v2`, `60` requests/min).
5. Click **Install** — the connector will validate the credential and report
   `CONNECTED` once Hunter accepts the key.

## 3. Run a health check

The connector's `health_check()` calls `GET /account` and returns
`HEALTHY / CONNECTED` when the key is valid. Use the platform's **Test
Connection** button to trigger it on demand.

## 4. Common operations

- **Find an email** — `email_finder(domain="stripe.com", first_name="Patrick", last_name="Collison")`
- **Verify an email** — `email_verifier(email="patrick@stripe.com")`
- **Domain search** — `domain_search(domain="stripe.com", department="engineering")`
- **Manage leads** — `list_leads`, `create_lead`, `update_lead`, `delete_lead`
- **Manage lead lists** — `list_lead_lists`, `create_lead_list`

## 5. Rate limits

Hunter enforces per-plan quotas server-side. The connector retries on `429`
and `5xx` responses with exponential backoff (honouring `Retry-After` when
the server provides it). If you see persistent `429`s, upgrade your Hunter
plan or lower `rate_limit_per_min`.
