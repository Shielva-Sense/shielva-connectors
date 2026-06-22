# Datadog Connector — Setup Guide

## Overview

This connector integrates Datadog with Shielva to sync your monitors, dashboards, incidents, hosts, and events. Datadog requires **two separate credentials** for API access: an API Key and an Application Key.

---

## Step 1 — Create a Datadog API Key

1. Log in to your Datadog account at [app.datadoghq.com](https://app.datadoghq.com) (or your regional equivalent).
2. Navigate to **Organization Settings** (bottom-left avatar menu → Organization Settings).
3. In the left sidebar, click **API Keys** under the *Access* section.
4. Click **+ New Key** (top right).
5. Give the key a descriptive name, e.g. `shielva-connector`.
6. Click **Create Key**.
7. **Copy the API key now** — Datadog will only show the full value once.

---

## Step 2 — Create a Datadog Application Key

The Application Key grants access to read-only management endpoints (monitors, dashboards, hosts, events).

1. In **Organization Settings**, click **Application Keys** in the left sidebar.
2. Click **+ New Key**.
3. Name it `shielva-connector-app` (or similar).
4. Click **Create Key**.
5. **Copy the Application Key now** — it is only shown once.

---

## Step 3 — Required Permissions

The credentials need **read** access to:

| Resource | Required scope / permission |
|---|---|
| Monitors | `monitors_read` |
| Dashboards | `dashboards_read` |
| Hosts | `infrastructure_read` |
| Events | `events_read` |
| Incidents | `incident_read` |
| Validate (health check) | Any valid key |

For a principle-of-least-privilege setup, create a **service account** user in Datadog with the built-in **Datadog Read Only** role, and generate keys under that account.

---

## Step 4 — Select Your Datadog Site

Datadog operates multiple regional sites. Choose the one that matches your account:

| Site | URL | Region |
|---|---|---|
| `datadoghq.com` | https://app.datadoghq.com | US (default) |
| `datadoghq.eu` | https://app.datadoghq.eu | EU (GDPR) |
| `us3.datadoghq.com` | https://us3.datadoghq.com | US3 (GovCloud-adjacent) |

If you are unsure, leave the site field blank — the connector defaults to `datadoghq.com`.

---

## Step 5 — Enter credentials in Shielva

In the Shielva connector installation form, enter:

- **API Key** — the key from Step 1
- **Application Key** — the key from Step 2
- **Datadog Site** (optional) — leave blank for US, or enter `datadoghq.eu` / `us3.datadoghq.com`

Click **Install** to validate and connect. The connector calls `GET /api/v1/validate` to confirm both keys are valid before saving.

---

## What gets synced

| Resource | Endpoint | Notes |
|---|---|---|
| Monitors | `GET /api/v1/monitor` | All monitors, paginated |
| Dashboards | `GET /api/v1/dashboard` | All dashboards |
| Hosts | `GET /api/v1/hosts` | All reporting hosts, paginated |
| Events | `GET /api/v1/events` | Last 24 hours by default |
| Incidents | `GET /api/v2/incidents` | Requires Incident Management feature |

---

## Troubleshooting

**403 Forbidden** — One or both keys are invalid, or the Application Key does not belong to the same organization as the API Key. Regenerate both from the same Datadog org.

**401 Unauthorized** — The API Key has been revoked or expired. Create a new one under Organization Settings → API Keys.

**Wrong site** — If the health check fails with a connection error, verify you selected the correct Datadog site for your account.

**Incident sync returns empty** — Incident Management must be enabled on your Datadog plan (requires a paid subscription with the Incident Management add-on).
