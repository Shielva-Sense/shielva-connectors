# Workable Connector — Setup Guide

## Prerequisites

- A Workable account with admin access
- Permissions to create API access tokens in Workable Settings
- Your Workable subdomain (the part before `.workable.com` in your account URL)

---

## Step 1: Generate a Workable API Token

1. Log in to your Workable account
2. Navigate to **Settings → Integrations → API Access Tokens**
3. Click **Generate new token**
4. Give the token a descriptive name (e.g. "Shielva Integration")
5. Copy the generated token — it will only be shown once

---

## Step 2: Find Your Workable Subdomain

Your Workable subdomain is the part before `.workable.com` in your account URL.

- If your account URL is `https://acme.workable.com`, your subdomain is `acme`
- If your account URL is `https://mycompany.workable.com`, your subdomain is `mycompany`

---

## Step 3: Install in Shielva

1. Open **Shielva ACP → Integrations → Workable**
2. Click **Connect**
3. Enter:
   - **API Token**: the token you generated in Step 1
   - **Workable Subdomain**: your subdomain from Step 2 (e.g. `mycompany`)
4. Click **Install** — Shielva will validate the credentials by calling `GET /spi/v3/accounts/{subdomain}`
5. Status changes to **Connected** on success

---

## Step 4: Verify

A **Connected** status confirms the API token has the required read access and the subdomain is correct.

---

## Authentication Details

Workable uses Bearer token authentication:

- **Header**: `Authorization: Bearer {api_token}`
- The connector sends this header on every request to `https://{subdomain}.workable.com`

---

## API Endpoints Used

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/spi/v3/accounts/{subdomain}` | Health check / credential validation |
| GET | `/spi/v3/jobs` | List jobs (paginated) |
| GET | `/spi/v3/jobs/{shortcode}` | Get single job |
| GET | `/spi/v3/candidates` | List candidates (paginated) |
| GET | `/spi/v3/candidates/{candidate_id}` | Get single candidate |
| GET | `/spi/v3/stages` | List pipeline stages |
| GET | `/spi/v3/members` | List team members |

---

## Pagination

The Workable API v3 uses `since_id` cursor pagination:

- Pass `since_id` and `limit` as query parameters
- The response body includes a `paging.next` URL when more pages exist
- The connector follows `paging.next` automatically during `sync()`

---

## Sync Behavior

The `sync()` operation:
1. Fetches all jobs (paginated via `paging.next`)
2. Normalizes each job into a `ConnectorDocument` with a stable SHA-256 ID
3. Fetches all candidates (paginated)
4. Normalizes each candidate into a `ConnectorDocument`
5. Fetches all pipeline stages (single response, no pagination)
6. Normalizes each stage into a `ConnectorDocument`
7. Optionally ingests each document into the Shielva knowledge base

**Document ID scheme:**
- Job: `SHA-256("job:" + shortcode)[:16]`
- Candidate: `SHA-256("candidate:" + candidate_id)[:16]`
- Stage: `SHA-256("stage:" + slug)[:16]`

---

## Troubleshooting

### 401 Unauthorized

The API token is invalid, expired, or has been deleted.

- Verify the token in **Workable → Settings → Integrations → API Access Tokens**
- Generate a new token if the current one was rotated or deleted
- Update the token in Shielva ACP

### 403 Forbidden

The API token does not have sufficient permissions.

- Ensure the token has read access for the required resource types
- Contact your Workable admin to verify account-level API permissions

### 404 Not Found

The subdomain is incorrect or the resource does not exist.

- Verify your Workable subdomain matches exactly what appears in your Workable account URL
- Check that the shortcode or candidate ID you are querying actually exists in Workable

### Rate Limit (429)

Workable enforces rate limits on API requests. The connector retries automatically with exponential backoff and honours the `Retry-After` header.

For high-volume accounts, consider scheduling sync operations during off-peak hours.

### Sync Finds No Jobs or Candidates

- Confirm that your Workable account has active jobs and candidates
- Verify the API token has read permissions for jobs and candidates
- Check that the subdomain is correct

### Health Check Passes but Sync Is Empty

- The account may be newly created with no data
- Check Workable directly to confirm jobs and candidates exist

---

## Security Notes

- The API token is stored encrypted in the Shielva vault and never logged
- The connector uses HTTPS for all requests to `{subdomain}.workable.com`
- Rotate the token in Workable Settings and update it in Shielva ACP if compromised
