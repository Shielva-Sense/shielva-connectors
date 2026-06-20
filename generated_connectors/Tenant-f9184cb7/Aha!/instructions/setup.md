# Aha! Connector — Setup Guide

## Prerequisites

You need an Aha! account with API access. API keys are available on all Aha! plans.

---

## Step 1 — Generate an API Key

1. Log in to your Aha! account at `https://{yoursubdomain}.aha.io`.
2. Click your **profile avatar** in the top-right corner.
3. Select **Settings** → **Security**.
4. Under **API Key**, click **Generate API key** (or copy your existing one).
5. Copy the key — it is shown only once per regeneration.

---

## Step 2 — Find your subdomain

Your Aha! subdomain is the part before `.aha.io` in your account URL.

For example, if your Aha! URL is `https://mycompany.aha.io`, your subdomain is `mycompany`.

---

## Step 3 — Install the connector

In the Shielva integration builder:

1. Navigate to **Integrations → Aha!**.
2. Click **Connect** or **Install**.
3. Enter your **API Key** in the `api_key` field.
4. Enter your **subdomain** (e.g. `mycompany`) in the `subdomain` field.
5. Click **Save / Install**.

The connector verifies your API key by calling `GET /api/v1/me`. On success, status is set to **Connected** and displays your Aha! user name.

---

## Required permissions

The Aha! API key grants the same read permissions as your user account. The connector uses read-only operations:

- Listing all products/workspaces
- Listing features, releases, ideas, and goals per product

No write permissions are required.

---

## What gets synced

| Resource | Aha! API endpoint | Notes |
|----------|------------------|-------|
| Products | `GET /api/v1/products` | All products the user has access to |
| Features | `GET /api/v1/products/{id}/features` | All features per product, paginated |
| Releases | `GET /api/v1/products/{id}/releases` | All releases per product, paginated |
| Ideas | `GET /api/v1/products/{id}/ideas` | All ideas per product, paginated |
| Goals | `GET /api/v1/products/{id}/goals` | All goals per product, single call |

Pagination uses Aha!'s page-number (`page` / `per_page`) mechanism with `pagination.total_pages` — all pages are fetched automatically.

---

## Normalized document types

Each synced item is converted to a `ConnectorDocument` with a stable `source_id` derived from `sha256("{type}:{id}")[:16]`:

| Type | Source ID prefix | Title |
|------|-----------------|-------|
| Feature | `sha256("feature:{id}")[:16]` | Feature name |
| Release | `sha256("release:{id}")[:16]` | Release name |
| Idea | `sha256("idea:{id}")[:16]` | Idea name |
| Goal | `sha256("goal:{id}")[:16]` | Goal name |

---

## Troubleshooting

### 401 Unauthorized
- The API key is wrong or has been regenerated.
- Go to **Settings → Security → API Key** and regenerate, then update the connector.

### 403 Forbidden
- Your account does not have access to a specific product.
- Verify you are an owner or contributor of the product in Aha!.

### 429 Too Many Requests — rate limit
- Aha! enforces per-key rate limits. The connector retries automatically with exponential backoff (up to 3 attempts).
- If the limit is consistently hit, reduce sync frequency.

### Connector returns 0 documents
- Verify the account has at least one product with features, releases, or ideas.
- Run `health_check()` to confirm connectivity and API key validity.

### Wrong subdomain
- If the subdomain is incorrect, all requests will fail with a connection error.
- Confirm your Aha! URL and extract the subdomain from `https://{subdomain}.aha.io`.
