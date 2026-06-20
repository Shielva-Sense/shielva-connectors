# Setup Instructions: QuickBooks Online

## Overview

The QuickBooks Online connector integrates your Intuit QuickBooks company with the Shielva platform. Once connected, Shielva can read and sync your invoices, customers, and chart-of-accounts into the knowledge base, and expose them as structured data for automation, reporting, and AI-powered workflows.

The connector uses Intuit's OAuth 2.0 Authorization Code flow — your team never shares QuickBooks credentials with Shielva. Instead, Intuit issues short-lived access tokens that the connector renews automatically via a refresh token.

---

## Prerequisites

Before you begin, make sure you have:

- An **Intuit Developer account** — create one at [developer.intuit.com](https://developer.intuit.com) if you don't have one.
- An **Intuit app** created in the developer portal (either Production or Sandbox environment).
- The **QuickBooks Accounting** scope enabled on your app (`com.intuit.quickbooks.accounting`).
- The **Shielva redirect URI** provided by your platform administrator — you will register it in your Intuit app before connecting.
- The **QuickBooks company** (realm) you want to connect must be accessible to the Intuit account you will use during the OAuth consent step.

---

## Step-by-Step Configuration

### Step 1: OAuth2 Client ID (`client_id`) — **Required**

1. Sign in to the [Intuit Developer Portal](https://developer.intuit.com) and select your app (or create a new one).
2. In the left navigation, click **Keys & OAuth**.
3. Select the environment tab: **Production** for a live company, **Sandbox** for testing.
4. Copy the **Client ID** shown — it is a long alphanumeric string.
5. Paste this value into the **OAuth2 Client ID** field in Shielva.

> **Tip:** The Client ID is not secret — it is safe to store in a configuration file. The Client Secret (Step 2) must be kept confidential.

---

### Step 2: OAuth2 Client Secret (`client_secret`) — **Required**

1. On the same **Keys & OAuth** page, copy the **Client Secret**.
2. Paste it into the **OAuth2 Client Secret** field in Shielva. This field is stored encrypted and never displayed again after saving.

> **Common mistake:** If you rotate the client secret in the Intuit Developer Portal, you must update this field in Shielva immediately — the old secret stops working instantly.

---

### Step 3: Redirect URI (`redirect_uri`) — **Optional**

- This is the URL Intuit will redirect to after the user grants consent.
- If your Shielva instance has a registered OAuth callback URL, paste it here and also add the same URI to your Intuit app's **Redirect URIs** list (found under **Keys & OAuth → Redirect URIs**).
- If you leave this blank, Shielva defaults to the Intuit OAuth2 Playground URI (`https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl`), which is only suitable for manual testing.

**To register the redirect URI in Intuit:**
1. In your app's **Keys & OAuth** page, scroll to **Redirect URIs**.
2. Click **Add URI**, paste the URI provided by Shielva, and click **Save**.

---

## Completing the OAuth Authorization

After saving your credentials:

1. Click **Connect** (or **Authorize**) in the Shielva connector dashboard.
2. You will be redirected to the Intuit consent screen.
3. Sign in with the Intuit account that has access to your QuickBooks company.
4. Select the QuickBooks company you want to connect (the **realm**) and click **Connect**.
5. Shielva receives an authorization code, exchanges it for access and refresh tokens, and stores them encrypted.

The connector renews the access token automatically using the refresh token. Intuit refresh tokens expire after **100 days of inactivity** — if the connector goes unused for that period, you will need to re-authorize.

---

## Testing the Connection

1. After OAuth completes, the connector status badge should show **Connected** (green).
2. Click **Run Health Check** — a successful response confirms the access token is valid and the QuickBooks API is reachable.
3. Click **Sync Now** to pull your first batch of invoices, customers, and accounts. Check the sync log for entity counts and any errors.
4. To test individual APIs, open **APIs → List Invoices** and click **Run**. You should receive a `QueryResponse` object containing your invoices.

---

## Environment: Production vs. Sandbox

| Setting | Production | Sandbox |
|---|---|---|
| Client ID / Secret | Production keys | Sandbox keys |
| QBO company data | Live company data | Intuit-provided sample data |
| API base URL | `quickbooks.api.intuit.com` | `sandbox-quickbooks.api.intuit.com` |
| Suitable for | Live deployments | Development & testing |

> The connector defaults to the Production API endpoint. For Sandbox testing, you will need to override the base URL or use a sandbox-specific build of the connector.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | Access token expired and refresh failed | Click **Re-authorize** on the connector card; complete the OAuth flow again |
| `invalid_client` during OAuth | Wrong Client ID or Client Secret | Double-check both values against Intuit Developer Portal → Keys & OAuth |
| `redirect_uri_mismatch` during OAuth | Shielva's redirect URI is not registered in your Intuit app | Add the URI under Keys & OAuth → Redirect URIs |
| `invalid_scope` during OAuth | `com.intuit.quickbooks.accounting` scope not enabled | Go to Intuit Developer Portal → your app → Scopes, and add the accounting scope |
| Connector shows **Missing Credentials** | `client_id` or `client_secret` is blank | Fill in both required fields and click **Save** |
| Connector shows **Pending OAuth** | OAuth flow has not been completed | Click **Authorize** and complete the Intuit consent screen |
| Rate limit errors during sync | QBO API throttling (default 500 req/min per app) | Reduce sync frequency or contact Intuit to raise your quota |
| Refresh token expired (after 100 days) | Intuit refresh tokens expire after 100 days of inactivity | Click **Re-authorize** to issue a fresh token pair |
| Company not found (`404`) | Wrong `realm_id` stored, or company was deleted | Re-authorize — the OAuth callback provides the correct `realmId` automatically |
