# Setup Instructions: Harvest

## Overview

The Harvest connector integrates your organization's Harvest account with the Shielva platform for time tracking, project management, and invoicing data. The connector authenticates with a **Personal Access Token (PAT)** scoped to a specific Harvest **Account ID** — Shielva never sees a user password, and you can revoke the token at any time from Harvest's developer console.

---

## Prerequisites

- A **Harvest account** with admin or owner-level access.
- Permission to generate a Personal Access Token at <https://id.getharvest.com/developers>.
- Your **Harvest Account ID** (a short numeric value).

---

## Step-by-Step Configuration

### Step 1: Harvest Account ID (`account_id`) — **Required**

1. Sign in to <https://id.getharvest.com/developers>.
2. Click **Create New Personal Access Token**.
3. Below the token field you will see a list of accounts you can access. Each entry shows an **Account ID** — copy the numeric value for the account you want to connect.
4. Paste it into the **Harvest Account ID** field in Shielva.

> The Account ID is also visible in Harvest under **Settings → Developers → Account ID**.

---

### Step 2: Personal Access Token (`access_token`) — **Required**

1. On the same developer page, give the token a name (e.g. `Shielva Connector`).
2. Click **Create Personal Access Token**.
3. Copy the token value — Harvest only shows it once.
4. Paste it into the **Personal Access Token** field in Shielva. The field is stored encrypted at rest.

> **If you lose the token, revoke it and create a new one** — Harvest cannot recover a token after the page closes.

---

### Step 3: Harvest API Base URL (`base_url`) — **Optional**

- **Default:** `https://api.harvestapp.com/v2`
- Leave blank. Only override if your operator routes Harvest API traffic through an approved proxy.

---

### Step 4: User-Agent (`user_agent`) — **Optional**

- **Default:** `Shielva Connector`
- Harvest **requires** a User-Agent header on every request. The default is fine for almost everyone; only change it if your account contact at Harvest has asked you to register a specific UA.

---

### Step 5: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default:** `100` (Harvest's documented limit per account).
- The connector retries on `429` responses automatically with exponential backoff, so the only reason to lower this is if you share the account with other automations.

---

## Testing the Connection

1. After saving, click **Run Health Check** — the connector calls `GET /users/me` and confirms the token + account combination.
2. Click **Run Sync** — recent time entries and invoices are pulled into the Shielva knowledge base.
3. To smoke-test a write, call `create_time_entry` with a project/task/date you don't mind editing — then delete it via `delete_time_entry`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` | PAT revoked, mistyped, or copied without trimming whitespace | Generate a new token and update Shielva |
| `403 Forbidden` | Token issued for a different Harvest account | Re-check the Account ID matches the one shown next to the token |
| `404 Not Found` | Resource (project/time entry) was deleted in Harvest | The connector surfaces this as `HarvestNotFound` — re-fetch the parent list |
| `429 Too Many Requests` (rare) | Other automations sharing the account | Lower their cadence — the connector already retries with backoff |
| Connector shows **Missing Credentials** | `account_id` or `access_token` is blank | Fill in both required fields and click **Save** |
