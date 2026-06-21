# Setup Instructions: Attio

## Overview

The Attio connector integrates your Attio CRM workspace with the Shielva platform. Once connected, Shielva can read your objects (people, companies, deals, custom objects), search and create records, manage list entries, and read or write notes — all via Attio's REST API (v2) and OAuth 2.0.

This connector uses the **OAuth 2.0 Authorization Code** flow. Your team never shares a long-lived API key; Attio issues a short-lived access token plus a refresh token that the connector renews automatically.

---

## Prerequisites

Before you begin, make sure you have:

- An **Attio workspace** (any plan that exposes the API)
- **Workspace admin / developer access** so you can register an OAuth application
- The Shielva **redirect URI** your platform administrator provides — you will paste it into the Attio OAuth app before connecting

---

## Step-by-Step Configuration

### Step 1: OAuth2 Client ID (`client_id`) — **Required**

1. Open [Attio](https://app.attio.com) and go to **Workspace settings → Developers → OAuth applications**.
2. Click **+ New application**. Give it a name (e.g. `Shielva Integration`).
3. In **Redirect URIs**, paste the redirect URI Shielva provided. Save the app.
4. After creation, Attio shows a **Client ID**. Copy it.
5. Paste the Client ID into the **OAuth2 Client ID** field in Shielva.

> **Tip:** The Client ID is not a secret — it identifies the app to Attio. The Client Secret (Step 2) is what must be kept confidential.

---

### Step 2: OAuth2 Client Secret (`client_secret`) — **Required**

1. On the same Attio OAuth application page, reveal the **Client Secret**. Attio shows the secret in plain text once at creation — copy it immediately.
2. Paste it into the **OAuth2 Client Secret** field in Shielva. The field is stored encrypted.

> **Common mistake:** If you regenerate the client secret in Attio, you must also update this field — the old secret stops working immediately.

---

### Step 3: OAuth2 Scopes (`scopes`) — **Optional**

- **Default value:** `record:read record:write list:read list:write note:read note:write`
- Leave blank to use the default — full record + list + note access.
- Tighten the scope string if your tenant only needs read access (e.g. `record:read list:read note:read`). Removing `record:write` will cause the **Create Record / Update Record / Delete Record** actions to fail with `403 Forbidden`.
- Scopes must be space-separated and must be authorized in your Attio OAuth application.

---

### Step 4: OAuth2 Redirect URI (`redirect_uri`) — **Optional**

- Leave blank unless you have a specific reason — the Shielva gateway injects the correct value at deploy time.
- Whatever value lands here at OAuth time **must** exactly match a redirect URI registered in your Attio OAuth application, or Attio rejects the flow with `redirect_uri_mismatch`.

---

### Step 5: OAuth2 Authorization URL (`auth_url`) — **Optional**

- **Default value:** `https://app.attio.com/authorize`
- Leave blank. Override only if Attio publishes a new authorization endpoint.

---

### Step 6: OAuth2 Token URL (`token_url`) — **Optional**

- **Default value:** `https://app.attio.com/oauth/token`
- Leave blank. Override only if Attio publishes a new token endpoint.

---

### Step 7: Attio API Base URL (`base_url`) — **Optional**

- **Default value:** `https://api.attio.com/v2`
- Leave blank. Override only if your organization routes Attio API traffic through an approved proxy.

---

### Step 8: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `100`
- Attio's standard quota is 100 requests/min per workspace. If Attio has granted your workspace a higher limit, raise this value to match — sync jobs will run faster.

---

## Completing the OAuth Authorization

After saving your credentials, click **Connect** in the Shielva connector dashboard. You will be redirected to Attio's consent screen. Sign in (if required), review the requested scopes, and click **Allow**.

Shielva will receive an authorization code, exchange it for an access token and refresh token, and store them encrypted. The access token is refreshed automatically before it expires — you will not need to re-authorize unless you revoke the OAuth app inside Attio.

---

## Testing the Connection

1. After OAuth completes, the connector status badge should turn **Connected** (green).
2. Click **Run Health Check** — a successful response means the access token is valid and `GET /self` is reachable.
3. Click **APIs → list_objects → Run** to see every object type defined in your workspace.
4. Try **APIs → list_records** with `object_slug = people` and `limit = 5` to fetch a small sample of records.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | Access token expired and refresh failed (or the OAuth app was deleted) | Click **Re-authorize** and complete the OAuth flow again |
| `403 Forbidden` on create / update / delete | A `record:write` (or `list:write` / `note:write`) scope is missing | Re-authorize with the default scope string in Step 3 |
| `invalid_client` during OAuth | Wrong Client ID or Client Secret | Re-paste both values from the Attio OAuth application page |
| `redirect_uri_mismatch` during OAuth | Shielva's redirect URI is not registered in Attio | Add the redirect URI shown in the error to your Attio OAuth app (Step 1) |
| `invalid_scope` during OAuth | A scope in Step 3 is not enabled on your OAuth application | Open the Attio OAuth app, enable the scope, save, retry |
| Rate-limit errors during sync | 100 req/min quota exceeded | Reduce sync frequency or request a higher quota from Attio |
| Connector shows **Missing Credentials** | `client_id` or `client_secret` is blank | Fill in both required fields and click **Save** |
