# Jotform Connector — Setup Guide

## Overview

The Jotform connector syncs form definitions and submissions from your Jotform account into Shielva. It uses the Jotform REST API v1 with API Key authentication (passed as a query parameter). Each form and submission is normalized into a searchable `ConnectorDocument`.

---

## Step 1 — Generate a Jotform API Key

1. Log in to your Jotform account at [https://www.jotform.com](https://www.jotform.com).
2. Click your profile icon (top-right) → **My Account**.
3. Go to the **API** section (URL: `https://www.jotform.com/myaccount/api`).
4. Click **Create New Key**.
5. Copy the generated API key.

---

## Step 2 — Gather your install fields

| Field | Key | Where to find it |
|-------|-----|-----------------|
| **API Key** | `api_key` | Generated in Step 1 — My Account → API |

> The API key belongs to a single Jotform account and grants access to all forms owned by that account.

---

## Step 3 — Install in Shielva ACP

1. In the Shielva ACP, navigate to **Integrations → Add Connector → Jotform**.
2. Paste your **API Key** into the install field.
3. Click **Install**. Shielva calls `GET https://api.jotform.com/user` to verify the key.
4. On success, the connector status shows **ONLINE** and displays your Jotform username/email.

---

## Required Permissions

The API key must belong to an account with access to:

- **Forms** — all forms owned by the account are synced
- **Submissions** — submission data for each form is fetched and normalized

Jotform's free plan includes API access. Paid plans have higher rate limits.

---

## API Endpoints Used

| Resource | Endpoint | Description |
|----------|----------|-------------|
| Account info | `GET /user` | Verify API key + get username/email |
| Forms list | `GET /user/forms` | Paginated list of all forms |
| Form detail | `GET /form/{id}` | Single form definition |
| Form questions | `GET /form/{id}/questions` | All questions for a form |
| Form submissions | `GET /form/{id}/submissions` | Paginated submissions for a form |
| All submissions | `GET /user/submissions` | Paginated submissions across all forms |
| Form reports | `GET /form/{id}/reports` | All reports for a form |

All requests pass `apiKey={api_key}` as a query parameter. The Jotform API returns `{"responseCode": 200, "content": {...}}` — the connector always unwraps `content`.

---

## Troubleshooting

### 401 Unauthorized / 403 Forbidden

**Cause:** The API key is invalid, expired, or lacks permission.

**Fix:**
- Verify the key at `https://www.jotform.com/myaccount/api`.
- Generate a new key if the existing one was deleted.
- Ensure the key belongs to the account that owns the forms you want to sync.

---

### 404 Not Found

**Cause:** A form ID does not exist or has been deleted.

**Fix:**
- Verify the form exists in your Jotform workspace.
- Deleted forms cannot be recovered.

---

### 429 Too Many Requests

**Cause:** Jotform API rate limit exceeded.

**Fix:**
- The connector automatically retries with exponential backoff (up to 3 attempts).
- Reduce sync frequency if 429s persist during large syncs.
- Upgrade to a Jotform paid plan for higher API rate limits.

---

### Sync returns 0 documents

**Cause:** No forms exist, or the account has no submissions.

**Fix:**
- Confirm forms exist at `https://www.jotform.com/myforms/`.
- Ensure at least one form has received a submission.
- Verify the API key belongs to the correct Jotform account.

---

### `ModuleNotFoundError: No module named 'aiohttp'`

**Fix:** Install dependencies:
```bash
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
pip install aiohttp>=3.9.0
```
