# Amplitude Connector — Setup Guide

This guide walks you through finding your Amplitude API credentials and connecting them to Shielva.

---

## 1. Obtain Your Amplitude API Key and Secret

1. Log in to your Amplitude account at [amplitude.com](https://amplitude.com).
2. Click **Settings** (gear icon) in the bottom-left sidebar.
3. Select **Projects** from the left-hand menu.
4. Click the project you want to connect.
5. Under the **General** tab, locate the **API Key** and **Secret Key** fields.
6. Copy both values — you will need them in the next step.

> **Security note:** Treat your API Secret like a password. Do not share it or commit it to version control. Shielva stores it AES-256-GCM encrypted at rest via the vault.

---

## 2. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **API Key** | Your Amplitude API Key (from Project Settings) |
| **API Secret** | Your Amplitude API Secret (from Project Settings) |
| **Region** | `us` (default) or `eu` — must match your project's data residency |

The connector validates the credentials by calling `GET /taxonomy/category` before completing installation.

### Region selection

- **US region (default):** `https://amplitude.com/api/2/` — use for projects hosted in the United States.
- **EU region:** `https://analytics.eu.amplitude.com/api/2/` — use for projects with EU data residency. Enter `eu` in the Region field.

---

## 3. What Gets Synced

The connector syncs three data categories on each run (last 30 days):

| Resource | Amplitude API | Description |
|----------|--------------|-------------|
| Event segmentation | `GET /events/segmentation` | Daily event counts for "Any Active Event" and "Any Event" |
| Active users | `GET /active` | Daily active user (DAU) counts |
| Cohorts | `GET /cohorts` | All defined cohorts with name, size, and description |

Each data point becomes a separate `ConnectorDocument` with a stable ID based on `SHA-256(api_key:event_type:date)[:16]` — ensuring deduplication across syncs without storing seen-ID lists.

---

## 4. Available API Operations

| Operation | Amplitude API | Description |
|-----------|--------------|-------------|
| `install()` | `GET /taxonomy/category` | Validate credentials on connector install |
| `health_check()` | `GET /taxonomy/category` | Verify credentials are still valid |
| `sync()` | Multiple | Full sync: events + DAU + cohorts (last 30 days) |
| `export_events(start, end)` | `GET /export` | Download raw event ZIP archive (YYYYMMDDTHH format) |
| `get_event_segmentation(event, start, end)` | `GET /events/segmentation` | Event counts over a date range |
| `list_cohorts()` | `GET /cohorts` | List all cohorts in the project |
| `get_cohort(cohort_id)` | `GET /cohorts/{id}/members` | Cohort member user IDs |
| `get_active_users(start, end)` | `GET /active` | DAU/WAU/MAU data |
| `get_user_activity(user_id)` | `GET /usersearch` | Event stream for a specific user |

---

## 5. API Details

- **US Base URL:** `https://amplitude.com/api/2/`
- **EU Base URL:** `https://analytics.eu.amplitude.com/api/2/`
- **Authentication:** HTTP Basic Auth — API Key as username, API Secret as password
- **Date format (segmentation):** `YYYYMMDD` (e.g. `20240101`)
- **Date format (export):** `YYYYMMDDTHH` (e.g. `20240101T00`)

---

## 6. Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `api_key is required` | No key provided | Enter the API Key in the install form |
| `api_secret is required` | No secret provided | Enter the API Secret in the install form |
| `401 Unauthorized` / `403 Forbidden` | Invalid or wrong credentials | Verify the key and secret from Amplitude → Settings → Projects |
| `429 Too Many Requests` | Rate limit exceeded | The connector retries automatically with exponential backoff |
| No data synced | Empty project / no events in last 30 days | Check that the Amplitude project has data in the last 30 days |
| Wrong region | EU project using US URL | Set region to `eu` in the connector config |

---

## 7. Retry & Resilience

- **Automatic retry:** Up to 3 attempts with exponential backoff (1s base, 2× factor, ±0.5s jitter, 30s cap)
- **Rate limit aware:** Respects `Retry-After` header from Amplitude on 429 responses
- **Circuit breaker:** Opens after 5 consecutive failures, resets after 60s — prevents cascade failures
- **Auth errors are not retried:** A 401/403 requires re-configuring the connector with valid credentials

---

## 8. Running the Test Suite

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/amplitude_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
python -m pytest tests/ -v
```

All tests are mocked — no live Amplitude credentials are needed to run the suite.
