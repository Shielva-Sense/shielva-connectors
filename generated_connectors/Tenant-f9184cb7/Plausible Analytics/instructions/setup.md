# Setup Instructions: Plausible Analytics

## Overview

The Plausible Analytics connector integrates your organization's Plausible account (cloud or self-hosted) with the Shielva platform. Once connected, Shielva can read aggregate, time-series, and breakdown stats; fetch live visitor counts; record pageviews and custom events; and (with a provisioning-enabled key) manage sites and conversion goals.

Plausible exposes three API surfaces:

- **Stats API** (`/stats/...`) — Bearer-authenticated reads of analytics data
- **Sites API** (`/sites/...`) — Bearer-authenticated CRUD for sites and goals
- **Events API** (`/events`) — anonymous writes of pageviews and custom events (identity is taken from the User-Agent header, not the API key)

---

## Prerequisites

- A **Plausible account** — sign up at [plausible.io](https://plausible.io) or run your own instance
- At least one **tracked site** in Plausible
- A **Stats API key** with the scopes you need (read-only for Stats, sites-provisioning for full CRUD)
- The site's **domain** (e.g. `example.com`) — Plausible uses the domain as the site ID

---

## Step-by-Step Configuration

### Step 1: API Key (`api_key`) — **Required**

1. Sign in to Plausible at [plausible.io](https://plausible.io) (or your self-hosted URL).
2. Open the user menu (top-right) and choose **Settings**.
3. Click **API Keys** in the left sidebar.
4. Click **+ New API Key**. Give it a name (e.g. `Shielva integration`) and choose the access level you need:
   - **Stats API access** — read-only for `aggregate`, `timeseries`, `breakdown`, `realtime`
   - **Sites Provisioning** — required for `list_sites`, `create_site`, `update_site`, `delete_site`, `list_goals`, `create_goal`
5. Copy the key shown immediately — Plausible only displays it once.
6. Paste it into the **Plausible API Key** field in Shielva.

> **Tip:** Plausible never sends the key in plaintext over the wire — all Bearer headers are sent over HTTPS. Shielva also stores this value encrypted.

---

### Step 2: API Base URL (`base_url`) — **Optional**

- **Default value:** `https://plausible.io/api/v1`
- Leave blank if you use Plausible cloud.
- Set this to your self-hosted instance URL (e.g. `https://plausible.mycompany.com/api/v1`) when running Plausible Community Edition.

---

### Step 3: Default Site ID (`default_site_id`) — **Optional**

- The domain of the site Shielva should target by default — e.g. `example.com`.
- Required if you want **Health Check** and **Sync** to work without passing a `site_id` explicitly.
- All per-call APIs (`aggregate`, `timeseries`, `breakdown`, `realtime_visitors`, …) still accept an explicit `site_id` parameter, so you can leave this blank when the integration always specifies the site at call time.

---

### Step 4: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `600` (requests per minute)
- Plausible cloud's default Stats API quota is 600 requests per minute. Leave blank to use this value.
- If your account has a higher tier, enter that limit.

---

## Testing the Connection

1. After saving, click **Run Health Check** on the connector card. The connector calls `/stats/realtime/visitors?site_id=<default_site_id>` — a successful 200 confirms both the key and the site ID.
2. Try **Aggregate Stats** with `site_id=<your-site>` and `period=30d` to verify Stats API access.
3. Try **List Sites** to verify Sites Provisioning access (if enabled on the key).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | API key is wrong, expired, or revoked | Generate a new key in **Plausible → Settings → API Keys** and update the field in Shielva |
| `404 Not Found` on health check | `default_site_id` is wrong or you don't own that site | Verify the domain in **Plausible → Sites** and update `default_site_id` |
| `429 Too Many Requests` during sync | Quota exceeded | Lower the sync frequency, or upgrade your Plausible plan / raise the limit on your self-hosted instance |
| `403 Forbidden` on `list_sites` | API key lacks **Sites Provisioning** scope | Create a new key with Sites Provisioning enabled |
| `Network error` repeatedly | Plausible instance unreachable from the Shielva worker | For self-hosted: verify firewall rules and that `/api/v1` is publicly reachable from the Shielva network |
| Pageview shows in Plausible but with wrong location/device | `user_agent` defaults to `Shielva/1.0` — Plausible can't derive a realistic device profile | Forward the real visitor's `User-Agent` to `record_pageview` instead of relying on the default |
