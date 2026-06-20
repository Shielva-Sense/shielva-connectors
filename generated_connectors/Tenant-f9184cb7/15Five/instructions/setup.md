# 15Five Connector — Setup Guide

## Overview

The 15Five connector syncs check-in reports, OKR objectives, high-five recognition, 1-on-1 meetings, users, and group data from your 15Five account into Shielva. It uses the 15Five REST API v1 with Bearer token authentication.

---

## Step 1 — Generate a 15Five API Key

1. Log in to 15Five as an account administrator.
2. Navigate to **Settings → Integrations → API**.
3. Click **Generate New API Key**.
4. Enter a name for the key (e.g. "Shielva Connector") and click **Create**.
5. Copy the API key — it is shown only once.

> **Note:** The user whose API key is used must have sufficient permissions to read all the data you want to sync. Use an Administrator account for full access.

---

## Step 2 — Gather your install fields

| Field | Where to find it | Example |
|-------|-----------------|---------|
| **API Key** | Generated in Step 1 | `abc123xyz...` |

---

## Step 3 — Install in Shielva ACP

1. In the Shielva ACP, navigate to **Integrations → Add Connector → 15Five**.
2. Paste your **API Key** into the install field.
3. Click **Install**. Shielva calls `GET /api/public/v1/user/` to verify credentials.
4. On success, the connector status shows **ONLINE**.

---

## Required Permissions

The 15Five user whose API key is used must have read access to:

- **Users** — employee/user directory
- **Reports** — weekly check-ins
- **Objectives** — OKRs
- **Meetings** — 1-on-1 meetings
- **High Fives** — recognition/shoutouts
- **Groups** — teams

Administrators have full access by default.

---

## API Endpoints Used

| Resource | Method | Endpoint |
|----------|--------|----------|
| Users | GET | `/api/public/v1/user/` |
| Reports (Check-ins) | GET | `/api/public/v1/report/` |
| Single Report | GET | `/api/public/v1/report/{id}/` |
| Objectives (OKRs) | GET | `/api/public/v1/objective/` |
| Meetings | GET | `/api/public/v1/meeting/` |
| High Fives | GET | `/api/public/v1/highfive/` |
| Groups | GET | `/api/public/v1/group/` |

All requests use Bearer token authentication: `Authorization: Bearer {api_key}`

All list endpoints use DRF-style pagination with `count`, `next`, `previous`, and `results` fields.

---

## Authentication Details

15Five uses Bearer token authentication:

```
Authorization: Bearer {api_key}
Accept: application/json
```

The connector handles this automatically. You never need to construct auth headers manually.

---

## Sync Behavior

The sync engine fetches and normalizes three primary data types:

| Data Type | Normalized As | Document Type |
|-----------|--------------|---------------|
| Check-in Reports | `ConnectorDocument` | `checkin` |
| OKR Objectives | `ConnectorDocument` | `objective` |
| High Fives | `ConnectorDocument` | `recognition` |

Each document has a stable 16-character SHA-256 source ID (`sha256("report:{id}")[:16]` etc.) ensuring idempotent re-syncs.

Objectives and high-five sync failures are non-fatal — check-ins always complete first.

---

## Troubleshooting

### 401 Unauthorized

**Cause:** API key is invalid, expired, or has been revoked.

**Fix:**
- Confirm the API key was copied correctly (no extra spaces).
- Regenerate the key at **Settings → Integrations → API** in 15Five.
- Ensure the key belongs to an active user account.

---

### 403 Forbidden

**Cause:** The API key user lacks permission to access the requested resource.

**Fix:**
- Promote the 15Five user to Administrator, or assign appropriate permissions.

---

### 404 Not Found

**Cause:** The requested report, objective, or resource ID does not exist.

**Fix:**
- Verify the resource exists in 15Five.
- Check that pagination is not exceeding available pages.

---

### 429 Too Many Requests (Rate Limit)

**Cause:** 15Five enforces rate limits on API requests.

**Fix:**
- The connector automatically retries with exponential backoff (up to 3 attempts).
- 15Five's `Retry-After` header is honoured when present.
- If 429s persist, reduce sync frequency.

---

### Sync returns 0 documents

**Cause:** No data exists in 15Five for the authenticated user's scope, or the API key lacks permissions.

**Fix:**
- Ensure there are active users, reports, and objectives in 15Five.
- Use an Administrator API key to access all organization data.

---

### `ModuleNotFoundError: No module named 'aiohttp'`

**Fix:** Install dependencies:
```bash
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
pip install aiohttp>=3.9.0
```

---

## Running Tests

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/fifteen_five_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
python -m pytest tests/ -v
# Expected: 99 passed
```
