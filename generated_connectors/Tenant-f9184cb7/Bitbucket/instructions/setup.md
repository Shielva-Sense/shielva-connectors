# Setup Instructions: Bitbucket

## Overview

The Bitbucket connector integrates your organization's Bitbucket Cloud workspaces with the Shielva platform. Once connected, Shielva can read workspaces, repositories, pull requests, issues, branches, commits, and raw file content, and can create or merge pull requests and issues on your behalf. The connector uses Bitbucket's OAuth 2.0 Authorization Code flow — your team never shares passwords; instead, Bitbucket issues a short-lived access token that the connector renews automatically.

This connector requires a Bitbucket Cloud workspace and an OAuth consumer (Client Key and Secret) registered against that workspace.

---

## Prerequisites

Before you begin, make sure you have:

- A **Bitbucket Cloud account** with administrator access to the workspace you want to connect
- The Shielva **redirect URI** that your platform administrator provides — you will register it on the OAuth consumer before connecting

---

## Step-by-Step Configuration

### Step 1: OAuth2 Client ID (`client_id`) — **Required**

1. In Bitbucket, click your workspace avatar → **Workspace settings**.
2. Under **Apps and features**, select **OAuth consumers** → **Add consumer**.
3. Fill in **Name** (e.g. "Shielva"), **Description**, and the **Callback URL** (paste the Shielva redirect URI exactly as provided).
4. Tick the permissions: **Account: read**, **Repositories: read/write**, **Pull requests: read/write**, **Issues: read/write**.
5. Click **Save**. Bitbucket reveals the **Key** (your `client_id`) and a separate **Secret**.
6. Paste the **Key** into the **OAuth2 Client ID** field in Shielva.

> **Tip:** The Key is not a secret — it identifies your OAuth consumer. The Secret (Step 2) must be kept confidential.

---

### Step 2: OAuth2 Client Secret (`client_secret`) — **Required**

1. On the same OAuth consumer detail page, copy the **Secret** value.
2. Paste it into the **OAuth2 Client Secret** field in Shielva. This field is stored encrypted at rest.

> **Common mistake:** If you regenerate the Secret in Bitbucket, you must update this field in Shielva — the old Secret immediately stops working.

---

### Step 3: OAuth2 Scopes (`scopes`) — **Optional**

- **Default value:** `account repository repository:write pullrequest pullrequest:write issue issue:write`
- Leave this field **blank** to use the default. The default grants read + write on repositories, pull requests, and issues.
- If you remove `repository:write` or `pullrequest:write`, the **Create Pull Request** and **Merge Pull Request** actions will fail.
- If you remove `issue:write`, the **Create Issue** action will fail.
- Scopes must be space-separated and must match the permissions you ticked when you created the OAuth consumer (Step 1).

---

### Step 4: OAuth2 Authorization URL (`auth_url`) — **Optional**

- **Default value:** `https://bitbucket.org/site/oauth2/authorize`
- Leave blank unless directed by your IT administrator.

---

### Step 5: OAuth2 Token URL (`token_url`) — **Optional**

- **Default value:** `https://bitbucket.org/site/oauth2/access_token`
- Leave blank.

---

### Step 6: Bitbucket API Base URL (`base_url`) — **Optional**

- **Default value:** `https://api.bitbucket.org/2.0`
- Leave blank. Override only if your organization routes Bitbucket Cloud traffic through an approved proxy.

---

### Step 7: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `60`
- Bitbucket Cloud's standard quota is one thousand requests per hour per OAuth consumer. The default of sixty requests per minute keeps you well under that ceiling.
- Increase only if your workspace has a documented quota tier above the default.

---

## Completing the OAuth Authorization

After saving your credentials, click **Connect** in the Shielva connector dashboard. You will be redirected to Bitbucket's consent screen. Sign in with the Bitbucket account you want to connect, review the requested permissions, and click **Grant access**.

Shielva will receive an authorization code, exchange it for access and refresh tokens, and store them securely. The connector will renew the access token automatically before it expires — you will not need to re-authorize unless you explicitly revoke the OAuth consumer in Bitbucket.

---

## Testing the Connection

1. After OAuth completes, the connector status badge should show **Connected** (green).
2. Click **Run Health Check** on the connector card — a successful check confirms the access token is valid and `/user` is reachable.
3. Click **List Workspaces** — you should see every workspace the OAuth consumer can access.
4. Pick one workspace, list its repositories, and open a pull request via **Create Pull Request** as a smoke test.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | Access token expired and refresh failed | Click **Re-authorize** on the connector card; complete the OAuth flow again |
| `403 Forbidden` on **Create Pull Request** | `pullrequest:write` scope missing | Re-authorize with the default scopes (Step 3 must include `pullrequest:write`) |
| `403 Forbidden` on **Create Issue** | `issue:write` scope missing | Re-authorize with `issue:write` included |
| `invalid_client` during OAuth | Wrong Key or Secret | Double-check both values against Bitbucket → Workspace settings → OAuth consumers |
| `redirect_uri_mismatch` during OAuth | Shielva's redirect URI not registered on the OAuth consumer | Edit the consumer in Bitbucket and paste the exact URI under **Callback URL** |
| Rate limit errors during sync | Quota exceeded | Lower the sync frequency or move to a higher quota tier with Atlassian |
| Connector shows **Missing Credentials** | `client_id` or `client_secret` is blank | Fill in both required fields and click **Save** |
