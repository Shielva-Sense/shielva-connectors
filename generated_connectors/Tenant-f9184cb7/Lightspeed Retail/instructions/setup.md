# Setup Instructions: Lightspeed Retail (R-Series POS)

## Overview

The Lightspeed Retail connector integrates your Lightspeed Retail (R-Series) point-of-sale account with the Shielva platform. Once connected, Shielva can read items, customers, sales, inventory, shops, employees, and vendors; create new items and customers; and update item fields. The connector uses Lightspeed's OAuth 2.0 Authorization Code flow — your team never shares passwords with Shielva; instead, Lightspeed issues an access token that the connector renews automatically using a long-lived refresh token.

This connector requires a Lightspeed Developer Portal account and an OAuth 2.0 client (Client ID and Client Secret), plus your Lightspeed **Account ID** so the connector can build the per-account REST URLs.

---

## Prerequisites

- A **Lightspeed Retail (R-Series)** account with administrator access
- A **Lightspeed Developer Portal** account at https://developers.lightspeedhq.com/
- The **Shielva redirect URI** that your platform administrator provides — you will register it on your OAuth client before connecting

---

## Step-by-Step Configuration

### Step 1: Lightspeed Account ID (`account_id`) — **Required**

1. Sign in to the Lightspeed Retail dashboard at https://retail.lightspeedhq.com.
2. Open **Settings → Account**. The numeric **Account ID** is shown at the top of the page (and also embedded in URLs like `https://us.merchantos.com/?name=main&form_name=settings`).
3. Copy the value and paste it into the **Lightspeed Account ID** field in Shielva.

> The Account ID is used in every API URL: `https://api.lightspeedapp.com/API/V3/Account/{account_id}/...`.

---

### Step 2: OAuth2 Client ID (`client_id`) — **Required**

1. Open the **Lightspeed Developer Portal** → https://developers.lightspeedhq.com/.
2. Go to **API** → **My Apps** → **Create app**.
3. Choose **Retail (R-Series)** as the product family.
4. Under **Redirect URIs**, click **Add** and paste the redirect URI provided by Shielva. Save.
5. After creation, the portal shows your **Client ID**. Copy it and paste it into the **OAuth2 Client ID** field in Shielva.

---

### Step 3: OAuth2 Client Secret (`client_secret`) — **Required**

1. On the same app detail page, copy the **Client Secret** value.
2. Paste it into the **OAuth2 Client Secret** field in Shielva. This field is stored encrypted by Shielva.

> If you regenerate the client secret in the Developer Portal, you **must** also update this field in Shielva — the old secret immediately stops working.

---

### Step 4: OAuth2 Scopes (`scopes`) — **Optional**

- **Default value:** `employee:all employee:register`
- Leave blank to use the default, which grants full read/write access to items, customers, sales, registers, and reporting.
- For a read-only deployment, narrow this to a smaller scope (e.g. `employee:inventory` only).
- Scopes must be space-separated.

---

### Step 5: OAuth2 Authorization URL (`auth_url`) — **Optional**

- **Default value:** `https://cloud.lightspeedapp.com/oauth/authorize.php`
- Leave blank. Override only if directed by your Lightspeed account manager.

---

### Step 6: OAuth2 Token URL (`token_url`) — **Optional**

- **Default value:** `https://cloud.lightspeedapp.com/auth/oauth/token`
- Leave blank.

---

### Step 7: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `50` (requests per minute)
- Lightspeed enforces a **leaky-bucket** rate limit (see header `X-LS-API-Bucket-Level: "current/max"`). The connector also honors this header at runtime, throttling automatically when the bucket reaches 90% capacity.
- Override only if Lightspeed has approved a higher tier for your account.

---

## Completing the OAuth Authorization

After saving your credentials, click **Connect** in the Shielva connector dashboard. You will be redirected to the Lightspeed authorization screen. Sign in with your Lightspeed account, review the requested permissions, and click **Authorize**.

Shielva will receive an authorization code, exchange it for an access token (≈30 min lifetime) and a long-lived refresh token, and store both securely. The connector renews the access token automatically — you will not need to re-authorize unless you explicitly revoke the app in the Lightspeed Developer Portal.

---

## Testing the Connection

1. After OAuth completes, the connector status badge should show **Connected**.
2. Click **Run Health Check** — a successful check confirms the access token is valid and `/Account.json` is reachable.
3. Click **List Items** with `limit=5` — a list of your first 5 items confirms data access.
4. To test mutation: click **Create Customer** with a test first/last name and email. The returned object should include a numeric `customerID`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | Refresh token revoked | Re-authorize the connector via **Connect** |
| `404 Not Found` on every call | Wrong `account_id` | Verify the Account ID in Lightspeed → Settings → Account |
| `invalid_client` during OAuth | Wrong Client ID or Client Secret | Re-copy both values from the Developer Portal |
| `redirect_uri_mismatch` during OAuth | Shielva's redirect URI not registered | Add it to your OAuth app's **Redirect URIs** (Step 2) |
| `429 Rate Limit` errors | Bucket overflow | Lower the sync frequency; the connector will also throttle automatically |
| Connector shows **Missing Credentials** | `account_id`, `client_id`, or `client_secret` blank | Fill in the missing field(s) and click **Save** |
