# Vanta Connector — Setup

This connector calls the Vanta REST API at `https://api.vanta.com/v1` using
an OAuth2 **client-credentials** grant.

## 1. Create a Vanta API Application

1. Sign in to Vanta as an admin: <https://app.vanta.com>.
2. Navigate to **Settings → Integrations → API**.
3. Click **New API Application**.
4. Name the application (e.g. *Shielva*) and select the scopes you need:
   - `vanta-api.all:read` — read every resource (people, devices, controls, tests, findings, evidence, policies, integrations).
   - `vanta-api.vendors:write` — required for `create_vendor`.
5. Click **Create**. Copy the **Client ID** and **Client Secret** — the
   secret is shown only once.

## 2. Install the connector in Shielva

In the Shielva Connectors UI, install **Vanta** and fill the form:

| Field | Value |
|-------|-------|
| OAuth2 Client ID | the Client ID from step 1 |
| OAuth2 Client Secret | the Client Secret from step 1 |
| Scopes | `vanta-api.all:read vanta-api.vendors:write` (default) |
| Base URL | `https://api.vanta.com/v1` (default) |
| Token URL | `https://api.vanta.com/oauth/token` (default) |
| Rate Limit (req/min) | `60` (default) |

Click **Install**. The connector validates the credentials and persists
the configuration.

## 3. Authenticate

Call **Authenticate** (or `connector.authenticate()` in code) to mint the
client-credentials access token. The token is cached in-process until 60 s
before its server-reported expiry, then auto-re-minted; it is also
re-minted automatically the first time the API returns 401.

## 4. Health check

Run **Health Check**. The connector probes `GET /people?pageSize=1` —
if it returns HTTP 200, the credentials and network path are good.

## 5. Common API calls

```python
people = await connector.list_people(page_size=50)
vendor = await connector.create_vendor(
    name="Acme Corp",
    description="Cloud infra vendor",
    website_url="https://acme.example",
    owner_email="security@yourco.com",
)
findings = await connector.list_findings(severity="high", status="open")
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `VantaAuthError: client_credentials grant failed (HTTP 401)` | wrong client_id / client_secret | regenerate in Vanta UI, reinstall |
| `VantaAuthError: ... insufficient_scope ...` | scope missing | edit scopes in Vanta API Application + reinstall |
| `VantaRateLimitError` | exceeding Vanta's rate limit | lower `rate_limit_per_min`, the client retries with backoff |
| `VantaNetworkError` | transient 5xx or DNS | the client retries 3 × with exponential backoff before surfacing |
