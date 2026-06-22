# Setup Instructions: Wix

## Overview

The Wix connector integrates your Wix account with the Shielva platform via the
Wix REST API. Once connected, Shielva can list and manage your sites, query and
mutate Stores products, search Ecom orders, manage Contacts / Members, and read
Blog posts, Bookings, and Subscriptions.

This connector authenticates with an **API key**, sent RAW (no `Bearer` prefix)
in the `Authorization` header alongside `wix-account-id` and (when site-scoped)
`wix-site-id` headers.

---

## Prerequisites

- A **Wix account** with admin access.
- The **Wix account ID** (visible in your Wix account settings).
- A **Wix API key** generated under **Headless → API Keys** (account-level or
  site-level scope, depending on which endpoints you need).
- Optionally: a default **site ID** so the connector can omit `site_id` from
  shorthand calls.

---

## Step 1: API Key (`api_key`) — **Required**

1. Open your Wix dashboard → **Settings** → **Headless** → **API Keys**.
2. Click **Generate API Key**.
3. Choose the desired permissions:
   - Stores (Read/Write) — for product CRUD
   - Ecom Orders (Read) — for order search
   - Contacts (Read/Write) — for contact listing/creation
   - Members (Read), Blog (Read), Bookings (Read), Subscriptions (Read)
4. Copy the generated key and paste it into **Wix API Key** in Shielva.

> **Important:** Wix expects the raw key in the `Authorization` header —
> no `Bearer ` prefix. The connector handles this automatically.

---

## Step 2: Account ID (`account_id`) — **Required**

1. In your Wix dashboard, open **Settings → Account Settings**.
2. Copy the **Account ID** (a UUID).
3. Paste it into the **Wix Account ID** field in Shielva.

The account ID is sent as the `wix-account-id` header on every request.

---

## Step 3: Default Site ID (`default_site_id`) — Optional

If most calls target a single site, set the default:

1. In your Wix dashboard, switch to the site you want as default.
2. Open **Settings → Site Info** and copy the **Site ID**.
3. Paste into **Default Site ID** in Shielva.

When set, methods that omit a `site_id` argument will use this value. When
blank, callers must pass `site_id` on every site-scoped call.

---

## Step 4: Base URL (`base_url`) — Optional

Defaults to `https://www.wixapis.com`. Override only if you have a private
Wix endpoint (rare).

---

## Step 5: Rate Limit (`rate_limit_per_min`) — Optional

Defaults to `100`. The Wix platform enforces its own quotas; this setting
governs the connector's client-side limit.

---

## Verification

After saving the config, the connector runs **health_check** automatically. A
green status means the API key, account_id, and base_url are correct. A red
status with "401 Unauthorized" means the API key is wrong or has expired —
regenerate and re-save.
