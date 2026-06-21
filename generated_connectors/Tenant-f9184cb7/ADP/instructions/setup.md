# ADP Connector — Setup

The ADP connector talks to ADP's HR / Payroll APIs using OAuth 2.0 client
credentials over mutual TLS. You will need an ADP Marketplace app, an SSL
client certificate + private key, and the client_id / client_secret pair.

## 1. Register your app

1. Sign in to the [ADP Marketplace Developer Hub](https://developers.adp.com/).
2. Create an app of type *Web Service* (or claim an existing one).
3. Under **My Apps → your app → Credentials**, copy the **Client ID** and
   **Client Secret**. These will populate the `client_id` and `client_secret`
   fields in the Shielva install form.
4. Under **Products**, add the API products you need (`HR`, `Payroll`,
   `Time Off`, etc.). Add only what your tenants actually call.

## 2. Generate your SSL client certificate

ADP requires mTLS — every request is authenticated with both the bearer token
and the SSL client certificate.

1. In **My Apps → your app → Certificates**, click *Create CSR*.
2. Submit the CSR to ADP. They will issue a signed certificate as a `.pem` file.
3. Save the signed certificate at `cert_path` (e.g.
   `/etc/shielva/adp/client.crt`) and the corresponding private key at
   `key_path` (e.g. `/etc/shielva/adp/client.key`). Both files must be PEM
   encoded.

In production these paths must point at the sealed-config volume; ADP keys
are tenant secrets and never live in `.env`.

## 3. Install the connector

In the Shielva CMS / Connectors UI, install the **ADP** connector with:

| Field | Value |
|---|---|
| `client_id` | from step 1.3 |
| `client_secret` | from step 1.3 |
| `cert_path` | absolute path to the signed certificate (PEM) |
| `key_path` | absolute path to the private key (PEM) |
| `base_url` | leave blank to use `https://api.adp.com` |
| `token_url` | leave blank to use `https://accounts.adp.com/auth/oauth/v2/token` |
| `rate_limit_per_min` | optional — defaults to 60 |

Click **Install**, then **Authenticate**. A successful authentication mints
the first OAuth bearer token. After that, `health_check` should report
`HEALTHY / CONNECTED`.

## 4. Verify

```bash
# Health check
curl -X GET https://<gateway>/connectors/adp/health
# List one worker
curl -X GET 'https://<gateway>/connectors/adp/list_workers?top=1'
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401 Unauthorized` at token mint | Wrong `client_id` / `client_secret`, or app not yet approved for the requested products |
| `SSL: certificate verify failed` | Wrong cert/key files, or cert not yet signed by ADP |
| `404 Not Found` on a worker | Wrong `aoid`, or the worker belongs to a company your app is not entitled to |
| `429 Rate limit` | You exceeded the per-minute quota. The connector retries with backoff but you may need a quota uplift |
