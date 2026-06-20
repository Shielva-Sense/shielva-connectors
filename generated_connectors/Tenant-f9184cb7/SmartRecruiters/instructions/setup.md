# SmartRecruiters Connector — Setup Guide

## Prerequisites

- A SmartRecruiters account with Admin access
- Permissions to manage API credentials in SmartRecruiters Admin panel

---

## Step 1: Generate an API Token

1. Log in to your SmartRecruiters account
2. Navigate to **Admin → Apps & Integrations → API**
3. Click **Generate Token** (or find an existing token with sufficient scope)
4. Give the token a descriptive name (e.g. "Shielva Integration")
5. Ensure the token has access to:
   - Company information
   - Job postings
   - Candidates
   - Users
   - Departments
6. Copy the token — store it securely, as it will only be shown once

---

## Step 2: Install in Shielva

1. Open **Shielva ACP → Integrations → SmartRecruiters**
2. Click **Connect**
3. Enter:
   - **API Token (X-SmartToken)**: the token you generated in Step 1
4. Click **Install** — Shielva will validate the token by calling `GET /v1/companies/me`
5. Status changes to **Connected** on success

---

## Step 3: Verify

The install validation calls `GET https://api.smartrecruiters.com/v1/companies/me` with the `X-SmartToken` header. A **Connected** status and company name confirm the token has the required access.

---

## Authentication Details

SmartRecruiters REST API v1 authenticates using the `X-SmartToken` HTTP header:

```
X-SmartToken: <your_api_token>
```

The connector sets this header on every request. It does NOT use HTTP Basic Auth or Bearer token format.

---

## API Endpoints Used

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/v1/companies/me` | Health check / company info / get company ID |
| GET | `/v1/companies/{id}/postings` | List job postings (paginated) |
| GET | `/v1/companies/{id}/postings/{job_id}` | Get single job posting |
| GET | `/v1/candidates` | List candidates (paginated) |
| GET | `/v1/candidates/{id}` | Get single candidate |
| GET | `/v1/users` | List users (paginated) |
| GET | `/v1/configuration/departments` | List all departments |

---

## Pagination

The SmartRecruiters API paginates using:
- `limit` — number of records per page (default: 100)
- `offset` — starting position in the result set
- Response body contains `totalFound` (total matching records) and `items` (current page)

The connector follows offset pagination automatically during `sync()`.

---

## Sync Behavior

The `sync()` operation:
1. Calls `GET /v1/companies/me` to obtain the company ID
2. Fetches all job postings (paginated via limit/offset + totalFound)
3. Fetches all candidates (paginated)
4. Normalizes each record into a `ConnectorDocument` with a stable SHA-256 ID
5. Optionally ingests each document into the Shielva knowledge base

**Document ID scheme:**
- Job: `SHA-256("job:" + str(job_id))[:16]`
- Candidate: `SHA-256("candidate:" + str(candidate_id))[:16]`
- User: `SHA-256("user:" + str(user_id))[:16]`

---

## Troubleshooting

### 401 Unauthorized

The API token is invalid or has been revoked.

- Verify the token in **SmartRecruiters → Admin → Apps & Integrations → API**
- Generate a new token if the current one was rotated or expired

### 403 Forbidden

The token does not have sufficient permissions for the requested resource.

- Review the token's scope in the SmartRecruiters admin panel
- Ensure access to Jobs, Candidates, Users, and Departments

### Rate Limit (429)

SmartRecruiters enforces per-minute rate limits. The connector retries automatically with exponential backoff and honours the `Retry-After` response header.

For large accounts with many job postings or candidates, the sync may take longer. The `limit=100` page size minimizes total requests.

### Job Postings Not Appearing

- The token must have read access to job postings in the authenticated company
- Only postings in the company associated with the API token are returned
- Draft/internal postings require appropriate permission scope

### Health Check Passes but Sync Finds No Data

- Confirm the company has active job postings and candidates
- The `GET /v1/companies/me` endpoint validates auth but does not fetch job data

---

## Security Notes

- The API token is stored encrypted in the Shielva vault and never logged
- The connector uses HTTPS for all requests to `api.smartrecruiters.com`
- The token is transmitted as the `X-SmartToken` header on each request
- Rotate the token in SmartRecruiters Admin and update it in Shielva ACP if compromised
