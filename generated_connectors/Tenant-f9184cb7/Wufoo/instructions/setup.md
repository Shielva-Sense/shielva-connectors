# Wufoo Connector — Setup

## What you need
1. A Wufoo account.
2. The account **subdomain** — the part before `.wufoo.com` in your dashboard URL. If you log in at `https://acme.wufoo.com`, the subdomain is `acme`.
3. An **API key** — sign in to Wufoo, click your profile, choose **Account** → **API Information**, and copy the API key shown there.

## Install the connector
1. In Shielva, navigate to **Connectors → Add New → Wufoo**.
2. Enter:
   - **Wufoo Subdomain** — e.g. `acme`
   - **API Key** — paste the key from step 3 above
   - **Rate Limit (requests/min)** — leave at the default (`60`) unless your plan allows more
3. Click **Install**. The connector calls `GET /users.json` to verify the credentials before marking the install successful.

## How authentication works
Wufoo uses HTTP Basic auth where:
- the **username** is your API key, and
- the **password** is the literal string `footastic` (Wufoo's documented placeholder).

The connector handles this for you — you only ever supply the API key.

## Verify the connection
After installing you can exercise the connector via the platform's connector test runner:

- `health_check` → should return `health=HEALTHY, auth_status=CONNECTED`
- `list_forms` → returns the JSON `{ "Forms": [...] }` payload

## Send a test form entry
Wufoo entries are posted as `application/x-www-form-urlencoded` payloads where each key is a Wufoo field ID (`Field1`, `Field2`, …). Use `list_form_fields(form_id)` to discover the IDs, then call:

```python
await connector.submit_form_entry(
    form_id_or_hash="m7x4a1",
    field_values={"Field1": "Ada Lovelace", "Field2": "ada@example.com"},
)
```

## Register a webhook
```python
await connector.create_webhook(
    form_id_or_hash="m7x4a1",
    url="https://your-app.example/wufoo-webhook",
    handshake_key="rotate-this-shared-secret",
    metadata=True,
)
```

Wufoo will POST entry payloads to that URL on every submission; the `handshake_key` is included in the request body for verification.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| `401 Unauthorized` during install | API key is wrong, or the subdomain doesn't match the key's account. |
| `404 Not Found` on a known form | The form was deleted, or the hash typed in differs from the canonical one. Confirm via `list_forms`. |
| Intermittent `429 Too Many Requests` | You're above the per-minute quota — the connector retries with exponential backoff, but lower `rate_limit_per_min` if it persists. |
| Webhook never fires | Re-run `list_webhooks(form_id)` to confirm registration succeeded; verify the URL is publicly reachable and returns 200. |
