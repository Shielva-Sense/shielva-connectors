# Greenhouse Connector — Setup Guide

## Prerequisites

- A Greenhouse account with admin or API-level access
- Permissions to manage API credentials in Greenhouse Dev Center

---

## Step 1: Generate a Harvest API Key

1. Log in to your Greenhouse account
2. Navigate to **Configure → Dev Center → API Credential Management**
3. Click **Create New API Key**
4. Select **Harvest** as the API type
5. Give the key a descriptive name (e.g. "Shielva Integration")
6. Set permissions — at minimum enable read access for:
   - Jobs
   - Candidates
   - Applications
   - Departments
   - Users (required for health check)
7. Click **Create** and copy the API key shown — it will only be displayed once

---

## Step 2: Install in Shielva

1. Open **Shielva ACP → Integrations → Greenhouse**
2. Click **Connect**
3. Enter:
   - **Harvest API Key**: the key you created in Step 1
4. Click **Install** — Shielva will validate the key by calling `GET /users?per_page=1`
5. Status changes to **Connected** on success

---

## Step 3: Verify

The install validation calls `GET https://harvest.greenhouse.io/v1/users?per_page=1` using HTTP Basic Auth (api_key as username, empty string as password). A **Connected** status confirms the key has the required access.

---

## Authentication Details

Greenhouse Harvest API uses HTTP Basic Auth:

- **Username**: Your Harvest API key
- **Password**: Empty string (no password required)

The connector constructs `aiohttp.BasicAuth(api_key, "")` for every request.

---

## API Endpoints Used

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/v1/users?per_page=1` | Health check / credential validation |
| GET | `/v1/jobs` | List jobs (paginated) |
| GET | `/v1/jobs/{id}` | Get single job |
| GET | `/v1/candidates` | List candidates (paginated) |
| GET | `/v1/candidates/{id}` | Get single candidate |
| GET | `/v1/applications` | List applications (paginated) |
| GET | `/v1/applications/{id}` | Get single application |
| GET | `/v1/departments` | List all departments |

---

## Pagination

The Greenhouse Harvest API paginates results using:
- `per_page` and `page` query parameters (default page size: 100)
- `Link` header with `rel="next"` pointing to the next page URL

The connector follows `Link: <...>; rel="next"` headers automatically during `sync()`.

---

## Sync Behavior

The `sync()` operation:
1. Fetches all jobs (paginated via Link header)
2. Fetches all candidates (paginated)
3. Fetches all applications (paginated)
4. Normalizes each record into a `ConnectorDocument` with a stable SHA-256 ID
5. Optionally ingests each document into the Shielva knowledge base

**Document ID scheme:**
- Job: `SHA-256("job:" + str(job_id))[:16]`
- Candidate: `SHA-256("candidate:" + str(candidate_id))[:16]`
- Application: `SHA-256("application:" + str(application_id))[:16]`

---

## Troubleshooting

### 401 Unauthorized

The API key is invalid or has been revoked.

- Verify the key in **Greenhouse → Configure → Dev Center → API Credential Management**
- Ensure the key type is **Harvest** (not Ingestion API)
- Generate a new key if the current one was rotated

### 403 Forbidden

The API key does not have sufficient permissions.

- Edit the key in Dev Center and enable read permissions for Jobs, Candidates, Applications, Users
- Greenhouse permission grants are per-endpoint — check each resource type

### Rate Limit (429)

The Harvest API has per-minute rate limits (default ~50 requests/10 seconds for free plans). The connector retries automatically with exponential backoff and honours the `Retry-After` header.

For large accounts, consider increasing the `per_page` parameter to reduce total requests, or contact Greenhouse support to increase your rate limit.

### Candidates or Applications Not Appearing

- Confirm the API key has **read** permission for the relevant resource types
- Private candidates (`is_private: true`) require explicit permission in the API key settings
- Archived jobs and rejected applications are included in results by default

### Health Check Passes but Sync Finds No Data

- The API key may have job or candidate permissions but the account has no data yet
- Check that the Greenhouse account is active and has been used to post jobs

---

## Security Notes

- The Harvest API key is stored encrypted in the Shielva vault and never logged
- The connector uses HTTPS for all requests to `harvest.greenhouse.io`
- The key is transmitted as HTTP Basic Auth credentials on each request
- Rotate the key in Greenhouse Dev Center and update it in Shielva ACP if compromised
