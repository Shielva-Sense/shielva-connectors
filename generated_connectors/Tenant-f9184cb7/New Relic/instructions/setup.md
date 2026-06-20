# New Relic Connector — Setup Guide

## Overview

The New Relic connector syncs alert policies, APM applications, and alert incidents from your New Relic account into Shielva. It uses the New Relic REST v2 API and the NerdGraph GraphQL API.

---

## Prerequisites

- A New Relic account with at least one active project
- Your Account ID (found in Account Settings)
- A User API Key (not a License Ingest Key)

---

## Step 1 — Generate a User API Key

1. Log in to [one.newrelic.com](https://one.newrelic.com)
2. Click your user avatar in the bottom-left corner
3. Select **API keys**
4. Click **Create a key**
5. Set the **Key type** to **User**
6. Give it a name (e.g. `Shielva Integration`)
7. Click **Create a key**
8. Copy the key — it starts with `NRAK-`

> **Note:** User API Keys grant access to the REST v2 API and NerdGraph. License Ingest Keys (starting with `NRIQ-` or `NRAK-`) are for data ingest only and will not work.

---

## Step 2 — Find Your Account ID

1. In New Relic, click your user avatar → **Account settings**
2. Your Account ID appears in the URL: `https://one.newrelic.com/admin-portal/accounts/{ACCOUNT_ID}/...`
3. It also appears under **Administration → Access management → Accounts**

---

## Step 3 — Choose Your Region

New Relic operates two data centers:

| Region | API base URL |
|--------|-------------|
| **US** (default) | `https://api.newrelic.com/v2` |
| **EU** | `https://api.eu.newrelic.com/v2` |

If your account was created at [one.eu.newrelic.com](https://one.eu.newrelic.com), set region to `EU`. Otherwise leave it as `US`.

---

## Step 4 — Connect in Shielva

Fill in the install form with:

| Field | Value |
|-------|-------|
| **User API Key** | Your `NRAK-...` key from Step 1 |
| **Account ID** | Your numeric Account ID from Step 2 |
| **Region** | `US` or `EU` (default: `US`) |

Click **Install**. Shielva will verify connectivity by calling the New Relic Users API.

---

## What Gets Synced

| Resource | New Relic API | Description |
|----------|--------------|-------------|
| Alert Policies | `GET /alerts_policies.json` | All alert policy definitions and their incident preferences |
| APM Applications | `GET /applications.json` | All monitored applications with health status and performance metrics |
| Alert Incidents | `GET /alerts_incidents.json` | Recent alert violations and incidents |

Dashboards can be retrieved via the NerdGraph GraphQL endpoint (`https://api.newrelic.com/graphql`) using the `graphql_query` method.

---

## Troubleshooting

**401 / 403 error on install**
- Verify the key starts with `NRAK-` (User API Key, not License Ingest Key)
- Confirm the key has not been deleted or rotated in New Relic

**Cannot find my Account ID**
- Navigate to `https://one.newrelic.com/admin-portal/accounts` — the ID is listed next to each account name

**EU accounts returning empty data**
- Ensure you set Region to `EU`; US and EU have separate data stores

**Rate limits (429)**
- The connector respects `Retry-After` headers and surfaces `NewRelicRateLimitError`. Reduce sync frequency or contact New Relic support to increase your quota.
