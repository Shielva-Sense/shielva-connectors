# BambooHR Connector — Setup Guide

## Overview

The BambooHR connector syncs employee directory records and time-off requests from your BambooHR account into Shielva. It uses the BambooHR REST API v1 with HTTP Basic Auth (API key as the username, `x` as the password).

---

## Step 1 — Generate a BambooHR API Key

1. Log in to BambooHR as an account administrator.
2. Click your name in the upper-right corner and select **API Keys**.
3. Click **Add New Key**.
4. Enter a label (e.g. "Shielva Connector") and click **Generate Key**.
5. Copy the API key — it is shown only once.
6. Click **Save**.

> **Note:** The generating user's permissions determine what data the API key can access. For full employee and time-off data, use an admin account.

---

## Step 2 — Find your Company Domain

Your BambooHR company domain is the subdomain in your BambooHR URL:

```
https://<company_domain>.bamboohr.com/
```

For example, if your URL is `https://acme.bamboohr.com/`, your company domain is `acme`.

---

## Step 3 — Gather your install fields

| Field | Where to find it | Example |
|-------|-----------------|---------|
| **Company Domain** | The subdomain of your BambooHR URL | `acme` |
| **API Key** | Generated in Step 1 | `abc123xyz...` |

---

## Step 4 — Install in Shielva ACP

1. In the Shielva ACP, navigate to **Integrations → Add Connector → BambooHR**.
2. Fill in the two install fields: **Company Domain** and **API Key**.
3. Click **Install**. Shielva calls `GET /employees/directory` to verify credentials.
4. On success, the connector status shows **ONLINE**.

---

## Required Permissions

The BambooHR user whose API key is used must have access to:

- **Employee Directory** — read access to employee records
- **Time-Off** — read access to time-off requests

Administrators have full access by default. For a restricted user, ensure they have the relevant HR data permissions in **Settings → Access Levels**.

---

## API Endpoints Used

| Resource | Method | Endpoint |
|----------|--------|----------|
| Employee Directory | GET | `/employees/directory` |
| Single Employee | GET | `/employees/{id}` |
| Time-Off Requests | GET | `/time_off/requests` |
| Custom Reports | POST | `/reports/{report_id}` |
| Company Info | GET | `/company/info` |

All requests use HTTP Basic Auth: `Authorization: Basic base64(api_key:x)`

---

## Authentication Details

BambooHR uses HTTP Basic Auth where:
- **Username** = your API key
- **Password** = the literal string `x`

The connector handles this automatically via `aiohttp.BasicAuth(api_key, "x")`. You never need to construct auth headers manually.

---

## Troubleshooting

### 401 Unauthorized

**Cause:** API key is invalid or has been revoked.

**Fix:**
- Confirm the API key was copied correctly (no extra spaces).
- Regenerate the key at **My Info → API Keys** in BambooHR.
- Ensure the key belongs to an active user account.

---

### 403 Forbidden

**Cause:** The API key user lacks permission to access the requested resource.

**Fix:**
- Promote the BambooHR user to Administrator, or assign them the appropriate access level.
- For time-off data, ensure the user has "Time Off Manager" permissions.

---

### 404 Not Found

**Cause:** The company domain is incorrect or the employee ID does not exist.

**Fix:**
- Double-check the company domain — it is the subdomain before `.bamboohr.com`.
- Verify the employee or resource exists in BambooHR.

---

### 429 Too Many Requests (Rate Limit)

**Cause:** BambooHR enforces rate limits on API requests.

**Fix:**
- The connector automatically retries with exponential backoff (up to 3 attempts).
- If 429s persist, reduce sync frequency.

---

### Sync returns 0 employees

**Cause:** The API key user has no employees in scope, or the employee directory is empty.

**Fix:**
- Ensure the BambooHR account has active employees.
- Use an admin API key to access all employees across the organization.

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
cd /Users/vivekvarshavaishvik/Documents/client_dir/bamboohr_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
python -m pytest tests/ -v
# Expected: 62+ passed
```
