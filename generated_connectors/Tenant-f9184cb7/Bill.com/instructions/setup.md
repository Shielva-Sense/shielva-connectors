# Setup Instructions: Bill.com

## Overview

The Bill.com connector integrates your organization's Bill.com account with the Shielva platform. Once connected, Shielva can list and create vendors and customers, create and pay bills, list invoices, and read the chart of accounts.

Bill.com does **not** use OAuth. Instead, the connector uses session-based authentication: it logs in with your `userName`, `password`, `orgId`, and a `devKey`, and Bill.com returns a `sessionId` that the connector includes on every subsequent request. The connector caches the `sessionId` and silently re-authenticates if Bill.com reports the session has expired.

---

## Prerequisites

Before you begin, make sure you have:

- A **Bill.com account** with API access (Production or Sandbox)
- A Bill.com **Developer Key (devKey)** — apply for one at [developer.bill.com](https://developer.bill.com)
- The **Organization ID** for the Bill.com org you want to connect — found in Bill.com → **Settings → Profile → Organization ID**
- A Bill.com user that has API permissions enabled

---

## Step-by-Step Configuration

### Step 1: Username (`username`) — **Required**

The email address you use to sign in to Bill.com.

- Sign in to Bill.com to confirm the email.
- Paste it into the **Bill.com Username** field in Shielva.

> **Tip:** Use a dedicated service-account user for production integrations so individual employees' password resets do not break the connector.

---

### Step 2: Password (`password`) — **Required**

The password for the Bill.com user above.

- Paste it into the **Bill.com Password** field in Shielva. This field is stored encrypted.
- If the user has MFA enabled, the connector cannot complete the login flow — disable MFA on the service-account user or use a dedicated API-only user.

> **Common mistake:** If you change the password in Bill.com, you must also update this field in Shielva.

---

### Step 3: Organization ID (`org_id`) — **Required**

The Bill.com Organization ID identifies which Bill.com tenant the connector should act against.

1. Sign in to Bill.com.
2. Click your account avatar (top-right) → **Settings**.
3. Under **Your Company → Profile**, copy the **Organization ID** (a 17-character alphanumeric value, e.g. `00800000000000000`).
4. Paste it into the **Bill.com Organization ID** field in Shielva.

---

### Step 4: Developer Key (`dev_key`) — **Required**

The `devKey` authorizes your application to call the Bill.com API.

1. Go to [developer.bill.com](https://developer.bill.com) and request a developer key for your application. Approval typically takes 1–2 business days.
2. Once approved, copy the developer key from the developer portal.
3. Paste it into the **Bill.com Developer Key** field in Shielva. This field is stored encrypted.

> **Important:** Sandbox and Production each have their own devKey. Make sure the devKey matches the environment of the Bill.com user above — using a Sandbox devKey against Production (or vice versa) returns `BDC_1011` / Invalid Developer Key.

---

### Step 5: API Base URL (`base_url`) — **Optional**

- **Default value:** `https://api.bill.com/api/v2`
- Leave blank for Production.
- For the Bill.com Sandbox, set this to `https://api-sandbox.bill.com/api/v2`.

---

### Step 6: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `60` (requests per minute)
- Bill.com's standard quota is 60 requests per minute. Leave blank to use this default.
- If your developer key has been granted a higher quota, enter the approved limit here.

---

## Completing the Connection

After saving your credentials, click **Connect** in the Shielva connector dashboard. Shielva will perform a `POST /Login.json` immediately to verify the credentials and obtain a `sessionId`. If login succeeds, the connector status badge will show **Connected** (green).

The connector caches the `sessionId` and re-uses it for every subsequent call. If Bill.com reports the session has expired (`response_code=BDC_1024` / "Invalid Session"), the connector automatically re-logs-in and retries the failed call once.

---

## Testing the Connection

1. After install completes, the connector status badge should show **Connected** (green).
2. Click **Run Health Check** on the connector card — a successful check confirms Bill.com is reachable and the credentials are still valid.
3. Click **APIs → list_vendors** and run it with the default values — you should see a list of vendors from your Bill.com org.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Invalid Developer Key` (`BDC_1011`) | devKey is for the wrong environment or has been revoked | Verify the devKey at developer.bill.com; make sure it matches Sandbox vs Production |
| `Invalid User Name or Password` (`BDC_1018`) | Wrong username, password, or orgId | Re-enter all three; sign in to Bill.com in a browser to confirm |
| `Invalid Session` (`BDC_1024`) every call | Clock skew between Shielva host and Bill.com | Ensure the Shielva host's clock is in sync via NTP |
| `MFA required` errors during login | The Bill.com user has MFA enabled | Use a dedicated service-account user with MFA disabled, or use a method that supports MFA bypass for API users |
| Connector shows **Missing Credentials** | One of the four required fields is blank | Fill in all four required fields and click **Save** |
| `network error` on every call | Outbound HTTPS to `api.bill.com` is blocked | Add `api.bill.com` (or `api-sandbox.bill.com`) to the egress allowlist |
| `Rate limit exceeded` | Sustained burst above 60 req/min | Lower sync frequency or request a higher quota from Bill.com |
