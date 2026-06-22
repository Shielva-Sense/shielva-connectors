# New Relic Connector — Setup Guide

## Overview

This connector integrates New Relic with Shielva to sync your alert policies, APM applications, incidents, and dashboards. New Relic uses a single **User API Key** for authentication, along with your **Account ID** to scope queries to your organization.

---

## Step 1 — Create a New Relic User API Key

New Relic's REST API v2 and NerdGraph (GraphQL) both use a **User key** (not a License key or Ingest key).

1. Log in to your New Relic account at [one.newrelic.com](https://one.newrelic.com) (or [one.eu.newrelic.com](https://one.eu.newrelic.com) for EU).
2. Click your **Profile icon** in the bottom-left corner.
3. Select **API keys** from the menu.
4. Click **+ Create a key**.
5. Set **Key type** to **User** (not Ingest — User keys are required for REST and NerdGraph access).
6. Give it a descriptive name, e.g. `shielva-connector`.
7. Click **Create a key**.
8. **Copy the key immediately** — New Relic will not show the full value again.

> **Note:** License keys and Browser keys do NOT work for this connector. You must use a User key.

---

## Step 2 — Find Your Account ID

Your Account ID is required to scope NRQL queries and NerdGraph operations.

**Method A — From the profile menu:**
1. Click your **Profile icon** in the bottom-left corner.
2. Select **Administration**.
3. Under **Account details**, find your **Account ID** (a 7-digit number like `1234567`).

**Method B — From the URL:**
After logging in, the URL typically contains your account ID: `https://one.newrelic.com/accounts/1234567/...`

**Method C — From API keys page:**
On the API Keys page, your Account ID is shown in the top section alongside your account name.

---

## Step 3 — Select Your Region

New Relic operates two data center regions. Choose the one where your account is registered:

| Region | API Base URL | NerdGraph URL | Dashboard URL |
|--------|-------------|---------------|---------------|
| US (default) | `https://api.newrelic.com/v2/` | `https://api.newrelic.com/graphql` | `https://one.newrelic.com` |
| EU | `https://api.eu.newrelic.com/v2/` | `https://api.eu.newrelic.com/graphql` | `https://one.eu.newrelic.com` |

If your account is based in the EU (registered at `one.eu.newrelic.com`), select **EU**. Otherwise leave blank or select **US**.

---

## Step 4 — Enter Credentials in Shielva

In the Shielva connector configuration:

| Field | Value |
|-------|-------|
| **User API Key** | The User key you created in Step 1 (begins with `NRAK-...`) |
| **Account ID** | Your 7-digit account ID from Step 2 |
| **Region** | `US` or `EU` (default: `US`) |

---

## Step 5 — Verify Permissions

The User API key inherits the permissions of the New Relic user account that created it. For this connector to sync all resources, the user account needs access to:

| Resource | Access needed |
|----------|---------------|
| Alert policies & conditions | Read-only |
| APM applications | Read-only |
| Incidents (NerdGraph) | Read-only (`alerts:incidents:read`) |
| Dashboards (NerdGraph entity search) | Read-only |
| NRQL queries | Read-only (any valid account access) |

For a principle-of-least-privilege setup, create a **dedicated New Relic user** with the built-in **Read Only** base role, and generate the User key under that account.

---

## Technical notes

### REST API vs NerdGraph

This connector uses **both** New Relic APIs:

- **REST API v2** (`/v2/`) — Alert policies, APM applications
- **NerdGraph (GraphQL)** — Incidents, Dashboards, NRQL execution

Both APIs accept the same User API key. The connector sends both `Api-Key` and `X-Api-Key` request headers to satisfy either convention.

### NRQL queries

Use the `run_nrql` method to execute arbitrary NRQL queries scoped to your account:

```
SELECT count(*) FROM Transaction WHERE appName = 'checkout-service' SINCE 1 hour ago
```

Results are returned in the NerdGraph envelope format under `data.actor.account.nrql.results`.

### Pagination

- Alert policies: paginated at 25 per page (New Relic v2 default)
- Applications: paginated at 25 per page
- Incidents and Dashboards: fetched in a single NerdGraph request (cursor-based, first page only for now)

---

## Troubleshooting

| Error | Likely cause | Fix |
|-------|-------------|-----|
| `Authentication failed: Invalid API key` | Wrong key type or revoked key | Verify it is a **User key** (starts with `NRAK-`), not a License key |
| `Authentication failed: Forbidden` | Key lacks permission | Check user account has Read Only role |
| `account_id is required` | Missing Account ID field | Fill in the Account ID field |
| Network timeout | Wrong region selected | Try toggling between US and EU |
| Empty incidents list | Account has no open incidents | Expected — incidents only appear when alerts fire |
| Empty applications list | No APM agents installed | Install the New Relic APM agent in your application |
