# DocuSign Connector — Setup Guide

## Overview

The DocuSign connector integrates Shielva with the [DocuSign eSignature REST API v2.1](https://developers.docusign.com/docs/esign-rest-api/). It uses OAuth2 Authorization Code Grant so your credentials are never sent to Shielva servers — users authorize directly on DocuSign's login page.

---

## Prerequisites

- A DocuSign account (Developer Sandbox or Production)
- Access to the [DocuSign Developer Center](https://admindemo.docusign.com/) (sandbox) or [DocuSign Admin](https://admin.docusign.com/) (production)
- Ability to create an Integration Key (OAuth2 app)

---

## Step 1 — Create a DocuSign Integration Key

1. Log in to the **DocuSign Developer Center** (sandbox) or **DocuSign Admin** (production).
2. Go to **Settings → Apps and Keys** (or **Integrations → Apps and Keys**).
3. Click **Add App and Integration Key**.
4. Give it a name (e.g., "Shielva Connector").
5. Under **Authentication**, select **Authorization Code Grant**.
6. Under **Additional settings → Redirect URIs**, add:
   ```
   https://app.shielva.ai/oauth/callback/docusign
   ```
   (or your custom redirect URI if self-hosted)
7. Click **Save**.
8. Copy the **Integration Key** (UUID format) and the **Secret Key** shown on the page.

---

## Step 2 — Install the Connector in Shielva ACP

1. Navigate to **Integrations → DocuSign**.
2. Click **Connect**.
3. Fill in the install fields:

   | Field | Value |
   |-------|-------|
   | **Integration Key** | The UUID from step 1 (e.g. `11111111-2222-3333-4444-555555555555`) |
   | **Client Secret** | The secret key from step 1 |
   | **Redirect URI** | Leave blank to use the default Shielva callback, or enter your custom URI |

4. Click **Install**.

The connector will validate that both credentials are present and return `PENDING_OAUTH` status — this is expected. Proceed to Step 3.

---

## Step 3 — Complete OAuth2 Authorization

1. In the connector settings, click **Authorize**.
2. You will be redirected to the DocuSign login page.
3. Log in with your DocuSign account and click **Allow Access**.
4. DocuSign redirects back to Shielva with an authorization code.
5. Shielva automatically exchanges the code for tokens and calls `GET /oauth/userinfo` to retrieve your `account_id` and `base_uri`.
6. The connector status will change to **CONNECTED / HEALTHY**.

---

## Step 4 — Verify the Connection

Run a health check from the Shielva ACP or via the API:

```python
result = await connector.health_check()
print(result.health)        # ConnectorHealth.HEALTHY
print(result.account_name)  # e.g., "Acme Corp"
```

---

## Step 5 — Run Your First Sync

The connector fetches completed envelopes from the past 30 days by default:

```python
result = await connector.sync()
print(f"Found: {result.documents_found}")
print(f"Synced: {result.documents_synced}")
print(f"Failed: {result.documents_failed}")
```

---

## API Operations

| Method | Description | DocuSign Endpoint |
|--------|-------------|-------------------|
| `install()` | Validate credentials | — |
| `authorize(state)` | Returns OAuth2 URL | `https://account.docusign.com/oauth/auth` |
| `handle_oauth_callback(code)` | Exchange code for tokens | `https://account.docusign.com/oauth/token` |
| `health_check()` | Check account health | `GET /accounts/{account_id}` |
| `sync(full, since)` | Sync envelopes to knowledge base | `GET /accounts/{account_id}/envelopes` |
| `list_envelopes(from_date, status)` | List envelopes with filters | `GET /accounts/{account_id}/envelopes` |
| `get_envelope(envelope_id)` | Get a single envelope | `GET /accounts/{account_id}/envelopes/{id}` |
| `list_envelope_documents(envelope_id)` | List envelope documents | `GET /accounts/{account_id}/envelopes/{id}/documents` |
| `list_envelope_recipients(envelope_id)` | List envelope recipients | `GET /accounts/{account_id}/envelopes/{id}/recipients` |

---

## Base URL

After OAuth, the connector derives its base URL from the `base_uri` returned by `/oauth/userinfo`:

```
{base_uri}/restapi/v2.1
# Example: https://na4.docusign.net/restapi/v2.1
```

---

## Token Refresh

DocuSign access tokens expire after 8 hours. The connector automatically refreshes the token using the stored `refresh_token` when a 401 is encountered during `health_check()`. For long-lived connectors, ensure the refresh token is persisted in config.

---

## Scopes

The connector requests these OAuth2 scopes:

| Scope | Purpose |
|-------|---------|
| `signature` | Read and manage envelopes |
| `impersonation` | Act on behalf of users (required for service integrations) |

---

## Sandbox vs Production

| Environment | Auth Domain | API Domain |
|-------------|------------|------------|
| Sandbox (Developer) | `account-d.docusign.com` | `demo.docusign.net` |
| Production | `account.docusign.com` | Per account (e.g. `na4.docusign.net`) |

This connector targets the **production** DocuSign environment. For sandbox testing, you must register a separate Integration Key in the DocuSign Developer Center and use sandbox credentials.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `MISSING_CREDENTIALS` | `integration_key` or `client_secret` not set | Re-enter credentials in ACP |
| `PENDING_OAUTH` | OAuth2 flow not completed | Click Authorize and log in to DocuSign |
| `INVALID_CREDENTIALS` (401) | Token expired, no refresh token | Re-authorize the connector |
| `DEGRADED` (network) | DocuSign API unreachable | Check network / DocuSign status page |
| Empty sync result | No completed envelopes in date range | Adjust `from_date` or use `full=True` |
| `No DocuSign accounts found` | User has no active DocuSign accounts | Log in with a DocuSign account that has access |

---

## Security

- The `client_secret` and all OAuth tokens are stored encrypted in the Shielva Vault and never logged.
- The `integration_key` is not a secret but should be treated as one.
- Rotate your client secret in DocuSign Admin → Apps and Keys → Generate New Secret Key, then update the connector.
