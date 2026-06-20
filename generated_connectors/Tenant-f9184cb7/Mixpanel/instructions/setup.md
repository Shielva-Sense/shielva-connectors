# Mixpanel Connector — Setup Guide

## Overview

The Mixpanel connector integrates your Mixpanel project with Shielva. It exports raw
events via the Data Export API (NDJSON streaming), queries funnels and segmentation via
the Mixpanel Analytics API, and supports both US and EU data-residency regions.

Authentication uses **HTTP Basic Auth** with a Mixpanel Service Account — not a personal
API key. Service accounts are the recommended credential type for programmatic access
because they are revocable and scoped to specific projects.

---

## Step 1 — Locate your Project ID

1. Log in to [mixpanel.com](https://mixpanel.com) and open your project.
2. Navigate to **Settings → Project Settings**.
3. Your **Project ID** is displayed near the top. Copy it (e.g. `2345678`).

---

## Step 2 — Create a Service Account

> **Important:** Use a service account, not a personal API key. Service accounts have
> stable credentials and can be revoked without affecting your personal account.

1. In your Mixpanel project go to **Settings → Project Settings → Service Accounts**.
2. Click **Add Service Account**.
3. Enter a descriptive name (e.g. `shielva-connector`).
4. Assign the **Analyst** role — read-only access, sufficient for all connector operations.
5. Click **Create**. Mixpanel displays:
   - **Service Account Username** — looks like `shielva.connector.abc123@serviceaccount.mixpanel.com`
   - **Service Account Secret** — a long random string
6. **Copy both values immediately** — the secret is shown **only once**.
7. Click **Done**.

---

## Step 3 — EU Data Residency (optional)

If your Mixpanel project stores data in the EU:

1. Confirm in **Settings → Project Settings → Data Residency** that your project uses EU storage.
2. Set the **Region** field to `EU` when installing this connector.

The connector will automatically use the EU API endpoints:
- `https://eu.mixpanel.com/api/` (Analytics API)
- `https://eu.data.mixpanel.com/api/2.0/` (Data Export API)

For US projects, leave **Region** blank or set it to `US`.

---

## Step 4 — Install Fields

| Field | Description | Example |
|-------|-------------|---------|
| **username** | Mixpanel Service Account Username | `shielva.connector.abc123@serviceaccount.mixpanel.com` |
| **secret** | Mixpanel Service Account Secret (password) | `AbCdEfGh1234...` |
| **project_id** | Mixpanel Project ID | `2345678` |
| **region** | `US` or `EU` (optional, default `US`) | `US` |

---

## Step 5 — Install in Shielva ACP

1. In Shielva ACP navigate to **Integrations → Add Connector → Mixpanel**.
2. Fill in **username**, **secret**, **project_id**, and optionally **region**.
3. Click **Install**. Shielva calls `GET https://mixpanel.com/api/app/me/` to verify credentials.
4. On success the connector status shows **ONLINE**.

---

## API Endpoints Used

| Method | URL | Purpose |
|--------|-----|---------|
| GET | `https://mixpanel.com/api/app/me/` | Health check / credential probe |
| GET | `https://data.mixpanel.com/api/2.0/export/` | NDJSON raw event streaming |
| GET | `https://mixpanel.com/api/2.0/funnels/list/` | List saved funnels |
| GET | `https://mixpanel.com/api/2.0/funnels/` | Funnel conversion data |
| GET | `https://mixpanel.com/api/2.0/retention/` | User retention data |
| GET | `https://mixpanel.com/api/2.0/segmentation/` | Event segmentation counts |
| GET | `https://mixpanel.com/api/2.0/events/properties/` | Event property metadata |

All EU requests use `eu.mixpanel.com` and `eu.data.mixpanel.com` instead.

All requests use **HTTP Basic Auth**: `Authorization: Basic base64(username:secret)`.

---

## Data Export Limits

The Mixpanel Data Export API (`/export/`) has the following limits:

- **Date range**: Maximum 365 days per request.
- **Rate limits**: ~60 requests/hour per service account. The connector retries
  automatically with exponential backoff, honouring the `Retry-After` header.
- **NDJSON format**: The export endpoint returns newline-delimited JSON (one event per
  line), not a JSON array. The connector handles this automatically.

---

## Sync Behaviour

**sync()** queries events for the **last 30 days** by default:

1. Calls `query_events()` which streams events via the NDJSON export endpoint.
2. Each event is normalized into a `ConnectorDocument` with:
   - `id`: `sha256("event:" + distinct_id + "_" + time)[:16]` — stable 16-char hex
   - `source`: `"mixpanel"`
   - `type`: `"analytics_event"`
3. Returns a `SyncResult` with `documents_found`, `documents_synced`, `documents_failed`.

---

## Required Permissions

The service account must have the **Analyst** role (read-only) on the target project:

- Grants access to: event export, segmentation reports, funnels, cohorts, retention
- No write permissions required or used

---

## Troubleshooting

### 401 Unauthorized
**Cause:** Wrong username or secret, or the service account was deleted.
**Fix:** Verify the service account exists in **Project Settings → Service Accounts**.
The secret is shown only once — if lost, delete and recreate the service account.

### 403 Forbidden
**Cause:** Service account exists but lacks the Analyst role on this project.
**Fix:** In **Project Settings → Service Accounts**, re-assign the role to **Analyst** or higher.

### 404 Not Found
**Cause:** Project ID is incorrect, or the requested funnel/cohort no longer exists.
**Fix:** Confirm the Project ID in **Project Settings → Project ID**.

### 429 Too Many Requests
**Cause:** Mixpanel rate limits exceeded (~60 requests/hour for data export).
**Fix:** The connector retries automatically with exponential backoff (up to 3 attempts),
honouring the `Retry-After` header. For high-volume projects, reduce sync frequency.

### Export returns no events
**Cause:** No events in the selected date range, or the event name filter matches nothing.
**Fix:** Verify the date range in the Mixpanel dashboard. Confirm the service account
has read access to the project's event data.

### EU region — wrong endpoint
**Cause:** The connector is configured for US but your project uses EU data residency.
**Fix:** Set the **region** install field to `EU`. This switches all API calls to
`eu.mixpanel.com` and `eu.data.mixpanel.com`.
