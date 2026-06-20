# Pendo Connector — Setup Guide

This guide walks you through finding your Pendo Integration Key and connecting it to Shielva.

---

## 1. Obtain Your Pendo Integration Key

1. Log in to your Pendo account at [app.pendo.io](https://app.pendo.io).
2. Click the **Settings** gear icon in the bottom-left navigation.
3. Under the **Integrations** section, select **Integration Keys**.
4. Click **+ Add Integration Key** (or copy an existing one).
5. Give the key a descriptive name (e.g. "Shielva Connector") and save.
6. Copy the generated Integration Key — you will need it in the next step.

> **Security note:** Treat your Integration Key like a password. Do not share it or commit it to version control. Shielva stores it AES-256-GCM encrypted at rest via the vault.

---

## 2. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **Integration Key** | Your Pendo Integration Key (from Settings → Integrations → Integration Keys) |

The connector validates the key by calling `GET /api/v1/app` before completing installation.

---

## 3. What Gets Synced

The connector syncs the following data on each run:

| Resource | Pendo API | Description |
|----------|-----------|-------------|
| Apps | `GET /api/v1/app` | All Pendo applications in the subscription |
| Guides (per app) | `GET /api/v1/guide` | In-app guides — onboarding tours, tooltips, lightboxes |
| Features (per app) | `GET /api/v1/feature` | Tagged UI features — clicks, hovers, form fields |

Each guide and feature becomes a separate `ConnectorDocument` with a stable SHA-256-based ID, enabling upsert deduplication without storing seen-ID lists.

---

## 4. Available API Operations

| Operation | Pendo API | Description |
|-----------|-----------|-------------|
| `install()` | `GET /api/v1/app` | Validate integration key on connector install |
| `health_check()` | `GET /api/v1/app` | Verify key is still valid; returns app count |
| `sync()` | Multiple | Full sync: guides + features for all apps |
| `list_apps()` | `GET /api/v1/app` | List all Pendo applications |
| `list_guides(app_id)` | `GET /api/v1/guide` | List guides for a specific app |
| `list_features(app_id)` | `GET /api/v1/feature` | List tagged features for a specific app |
| `list_pages(app_id)` | `GET /api/v1/page` | List pages for a specific app |
| `list_accounts()` | `POST /api/v1/aggregation` | Fetch accounts via aggregation pipeline |
| `list_visitors()` | `POST /api/v1/aggregation` | Fetch visitors via aggregation pipeline |

---

## 5. API Details

- **Base URL:** `https://app.pendo.io`
- **Authentication:** `x-pendo-integration-key: <key>` header on every request
- **Content-Type:** `application/json`
- **Aggregation API:** POST with pipeline body — `{"response": {"mimeType": "application/json"}, "request": {"pipeline": [...]}}`

---

## 6. Document ID Scheme

Stable IDs prevent duplicate documents across syncs:

| Resource | ID formula |
|----------|-----------|
| Guide | `SHA-256("guide:" + guide_id)[:16]` |
| Feature | `SHA-256("feature:" + feature_id)[:16]` |
| Page | `SHA-256("page:" + page_id)[:16]` |
| Account | `SHA-256("account:" + accountId)[:16]` |

---

## 7. Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `integration_key is required` | No key provided | Enter the Integration Key in the install form |
| `401 Unauthorized` / `403 Forbidden` | Invalid or revoked key | Generate a new Integration Key in Pendo Settings |
| `429 Too Many Requests` | Rate limit exceeded | The connector retries automatically with exponential backoff |
| `404 Not Found` | Resource or app not found | Verify the app ID is valid for your subscription |

---

## 8. Retry & Resilience

- **Automatic retry:** Up to 3 attempts with exponential backoff (1s base, 2× factor, ±0.5s jitter, 30s cap)
- **Rate limit aware:** Respects `Retry-After` header from Pendo on 429 responses
- **Circuit breaker:** Opens after 5 consecutive failures, resets after 60s — prevents cascade failures
- **Auth errors are not retried:** A 401/403 requires re-configuring the connector with a valid Integration Key

---

## 9. Running the Test Suite

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/pendo_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
python -m pytest tests/ -v
```

All tests are mocked — no live Pendo credentials are needed to run the suite.
