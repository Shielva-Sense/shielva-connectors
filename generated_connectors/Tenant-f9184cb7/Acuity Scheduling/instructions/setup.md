# Acuity Scheduling Connector — Setup Guide

## Overview

The Acuity Scheduling connector syncs your appointments, clients, and appointment types from Acuity Scheduling (Squarespace) into Shielva using the Acuity Scheduling REST API v1. Authentication uses **HTTP BasicAuth** — your numeric User ID as the username and your API Key as the password.

---

## Step 1 — Find your User ID and API Key

1. Log in to [Acuity Scheduling](https://secure.squarespace.com/user/login).
2. In the left sidebar, click **Integrations**.
3. Scroll down to the **API** section and click **API Credentials** (or navigate directly to **Business Settings → Integrations → API**).
4. You will see:
   - **User ID** — a numeric identifier (e.g. `12345678`)
   - **API Key** — a long alphanumeric string

> **Tip:** The API Key is the credential that acts as your password. Keep it secret. If compromised, you can generate a new one from the same page.

---

## Step 2 — Gather your install fields

| Field | Description | Where to find it |
|-------|-------------|-----------------|
| **User ID** | Numeric Acuity account ID | Integrations → API Credentials |
| **API Key** | Secret API key for BasicAuth | Integrations → API Credentials |

---

## Step 3 — Install in Shielva ACP

1. In the Shielva ACP, navigate to **Integrations → Add Connector → Acuity Scheduling**.
2. Enter your **User ID** in the first field.
3. Paste your **API Key** into the second field.
4. Click **Install**. Shielva calls `GET /api/v1/me` to verify the credentials.
5. On success, the connector status shows **ONLINE** with your business name.

---

## What gets synced

| Resource | API Endpoint | Notes |
|----------|-------------|-------|
| Appointments | `GET /api/v1/appointments` | Paginated; supports date range filters |
| Clients | `GET /api/v1/clients` | Paginated; deduplicated by client ID |
| Appointment Types | `GET /api/v1/appointment-types` | All types in one request |

Shielva derives a stable 16-character `source_id` from `SHA-256("appointment:{id}")[:16]` (or `"client:{id}"` / `"appointment_type:{id}"`) so records can be deduplicated across incremental syncs.

---

## API limits

Acuity Scheduling enforces rate limits on its REST API. The connector retries automatically with exponential backoff (up to 3 attempts) on 429 responses, honouring the `Retry-After` header.

---

## Permissions

The API Key inherits the full permissions of the account that owns it. A single Acuity account can own multiple calendars — all of them are accessible with the same User ID and API Key.

---

## Troubleshooting

### 401 Unauthorized

**Cause:** The User ID or API Key is incorrect.

**Fix:**
- Go to Acuity Scheduling → Integrations → API Credentials.
- Confirm the User ID matches the numeric value shown.
- Regenerate the API Key if it may have been rotated or revoked, then update the connector install fields.

---

### 403 Forbidden

**Cause:** The credentials are valid but the requested resource is not accessible with this account.

**Fix:**
- Ensure you are using the credentials of the account that owns the data you want to sync.
- Sub-users or restricted API scopes may not have access to all endpoints.

---

### 404 Not Found

**Cause:** An endpoint or resource does not exist at the given URL.

**Fix:**
- Verify that the Acuity API base URL is `https://acuityscheduling.com`.
- Check that your Acuity account is active and not suspended.

---

### 429 Too Many Requests

**Cause:** Rate limit exceeded.

**Fix:**
- The connector retries automatically up to 3 times with backoff.
- If persistent, reduce sync frequency.

---

### Sync returns 0 appointments

**Cause:** No appointments exist in the account, or the date filter excludes all results.

**Fix:**
- Remove date filters to run a full sync (no `min_date` / `max_date`).
- Verify that appointments exist in your Acuity Scheduling dashboard.
