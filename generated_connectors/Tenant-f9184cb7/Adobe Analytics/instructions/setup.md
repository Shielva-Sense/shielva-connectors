# Adobe Analytics Connector — Setup Guide

## Overview

The **Adobe Analytics connector** integrates with the Adobe Analytics 2.0 API. It enables Shielva to sync report suites, segments, and calculated metrics from your Adobe Analytics organization into the knowledge base.

**Auth:** OAuth2 client_credentials grant via Adobe IMS (`ims-na1.adobelogin.com`).

---

## Prerequisites

1. An **Adobe Developer Console** account with access to your Adobe Analytics organization.
2. An Adobe Analytics product profile assigned in **Adobe Admin Console**.
3. A service account project in Adobe Developer Console with the **Adobe Analytics API** added.

---

## Step 1: Create a project in Adobe Developer Console

1. Go to [https://developer.adobe.com/console/projects](https://developer.adobe.com/console/projects).
2. Click **Create new project** → **Add API**.
3. Select **Adobe Analytics** and click **Next**.
4. Choose **OAuth Server-to-Server** as the credential type.
5. Select the product profiles that give access to the report suites you want to sync.
6. Click **Save configured API**.

---

## Step 2: Collect your credentials

From the project's **OAuth Server-to-Server** credential page, copy:

| Field | Where to find it |
|-------|-----------------|
| **Client ID** | Listed as "Client ID" or "API Key" on the credential screen |
| **Client Secret** | Click **Retrieve client secret** (one-time reveal) |
| **Organization ID** | Top-right of the Developer Console (format: `ABCDEF@AdobeOrg`) |

---

## Step 3: Find your Global Company ID

Your company ID is used in all Adobe Analytics 2.0 API URLs:

```
https://analytics.adobe.io/api/{company_id}/reportsuites
```

To find it:
1. Log into [https://analytics.adobe.com](https://analytics.adobe.com).
2. Go to **Admin → Company settings**.
3. Look for **Global Company ID** — it is a short alphanumeric slug (e.g. `mycompany`).

Alternatively, call the discovery API with your token:

```bash
curl -X GET "https://analytics.adobe.io/discovery/me" \
  -H "Authorization: Bearer {access_token}" \
  -H "x-api-key: {client_id}"
```

The response contains `imsOrgs[].companies[].globalCompanyId`.

---

## Step 4: Install the connector in Shielva

In the Shielva connector install form, fill in:

| Field | Value |
|-------|-------|
| Client ID (API Key) | From Step 2 |
| Client Secret | From Step 2 |
| Global Company ID | From Step 3 |
| Organization ID | From Step 2 (optional) |

Click **Install**. Shielva will:
1. POST to `https://ims-na1.adobelogin.com/ims/token/v3` with your credentials.
2. Use the access token to call `GET /api/{company_id}/reportsuites`.
3. Return **Connected** on success, or an error message explaining what failed.

---

## OAuth2 Token Details

| Parameter | Value |
|-----------|-------|
| Token URL | `https://ims-na1.adobelogin.com/ims/token/v3` |
| Grant type | `client_credentials` |
| Scopes | `openid,AdobeID,read_organizations,additional_info.projectedProductContext,additional_info.job_function` |
| Token TTL | 24 hours (refreshed automatically with 30s buffer) |
| Auth headers | `Authorization: Bearer {access_token}`, `x-api-key: {client_id}` |

Tokens are stored in process memory only and refreshed before expiry. They are never written to disk or logs.

---

## What the sync collects

| Resource | API endpoint |
|----------|-------------|
| Report suites | `GET /api/{company_id}/reportsuites` |
| Segments | `GET /api/{company_id}/segments?rsid={first_rsid}` |
| Calculated metrics | `GET /api/{company_id}/calculatedmetrics` |

Each resource is normalized to a `ConnectorDocument` with a stable `source_id` (SHA-256 hash) for deduplication across syncs.

---

## Troubleshooting

| Error | Likely cause | Fix |
|-------|-------------|-----|
| `client_id is required` | Missing field | Enter your Client ID |
| `client_secret is required` | Missing field | Enter your Client Secret |
| `company_id is required` | Missing field | Enter your Global Company ID |
| `Authentication failed: invalid_client` | Wrong client_id or secret | Re-copy credentials from Developer Console |
| `Authentication failed: 401` | Token expired or revoked | Re-install the connector |
| `404 Not Found` | Wrong company_id | Verify company_id via the discovery API |
| `429 Too Many Requests` | Rate limit hit | Connector retries automatically with exponential back-off |
| `500 Server Error` | Adobe API outage | Check [status.adobe.com](https://status.adobe.com) |

---

## Running tests locally

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/adobe_analytics_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
python -m pytest tests/ -v
```

All 60+ tests are fully mocked — no live Adobe credentials are required.

---

## Security notes

- Client secrets are stored AES-256-GCM encrypted at rest by Shielva vault.
- Neither `client_secret` nor `access_token` values appear in any log output.
- Tokens are stored in process memory only, never on disk.
- The connector scopes are read-only — it cannot modify Adobe Analytics data.
