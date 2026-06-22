# Box Connector — Setup Guide

This guide walks you through connecting Shielva to Box so that files and folders from your Box account are synced into your Shielva knowledge base.

---

## Prerequisites

- A Box account (Personal, Business, or Enterprise)
- Access to the [Box Developer Console](https://developer.box.com/console/)
- The Shielva integration builder open at https://localhost:8055

---

## Step 1 — Create a Box App

1. Go to the [Box Developer Console](https://developer.box.com/console/).
2. Click **Create New App**.
3. Select **Custom App**.
4. Choose **OAuth 2.0 with JWT (User Authentication)** — or **Standard OAuth 2.0** if you want user-delegated authorization.
5. Give your app a name (e.g. "Shielva Connector").
6. Click **Create App**.

---

## Step 2 — Configure OAuth 2.0 Settings

1. In the app settings, go to the **Configuration** tab.
2. Under **OAuth 2.0 Credentials**, copy the **Client ID** and **Client Secret** — you will need them in Step 4.
3. Under **OAuth 2.0 Redirect URIs**, add:
   ```
   https://localhost:8000/connectors/oauth/callback
   ```
   This must match exactly what you configure in Shielva.
4. Under **Application Scopes**, ensure **Read all files and folders stored in Box** is checked (this corresponds to the `root_readonly` scope).
5. Click **Save Changes**.

---

## Step 3 — Submit for App Review (if required)

For user-facing apps with `root_readonly` scope:

- Box requires you to **submit your app for review** before it can be used by other users.
- For personal development and testing, you can use the app immediately with your own Box account without review.
- Navigate to **App Authorization** in the Developer Console → **Submit and Review**.

---

## Step 4 — Install the Connector in Shielva

1. Open the Shielva integration builder.
2. Navigate to **Connectors** → **Box** → **Install**.
3. Enter the following values:
   - **Box App Client ID**: the `Client ID` from the Box Developer Console
   - **Box App Client Secret**: the `Client Secret` from the Box Developer Console
   - **Redirect URI**: `https://localhost:8000/connectors/oauth/callback`
4. Click **Install**.

---

## Step 5 — Complete the OAuth Flow

1. After install, click **Connect with Box**.
2. You will be redirected to the Box OAuth consent screen.
3. Sign in with the Box account whose files you want to sync.
4. Review the requested permissions:
   - **Read all files and folders stored in Box** (`root_readonly`)
5. Click **Grant access to Box**.
6. You will be redirected back to Shielva, and the connector status will change to **Connected**.

---

## Step 6 — Sync Files

1. In the connector detail view, click **Sync Now**.
2. The connector will recursively traverse all folders from the root, collecting all files.
3. Files are normalized into ConnectorDocuments and ingested into the selected knowledge base.
4. Subsequent syncs can be triggered manually or scheduled.

---

## Required OAuth Scopes

| Scope | Purpose |
|-------|---------|
| `root_readonly` | Read all files and folders stored in Box |

---

## API Operations

| Method | Description |
|--------|-------------|
| `install()` | Validates client_id and client_secret |
| `authorize()` | Returns the Box OAuth2 authorization URL |
| `health_check()` | Fetches `GET /users/me` to verify connectivity |
| `sync()` | Recursively syncs all files from root using BFS traversal |
| `list_folder(folder_id, limit, offset)` | Lists items in a folder |
| `get_file(file_id)` | Fetches metadata for a single file |
| `get_folder(folder_id)` | Fetches metadata for a single folder |
| `search(query, limit, offset)` | Searches Box content |

---

## Troubleshooting

### 401 Unauthorized — Token expired
The access token has expired and the refresh token could not renew it. Re-authorize the connector by clicking **Reconnect** in the connector settings. `health_check()` returns `DEGRADED/TOKEN_EXPIRED`.

### 403 Forbidden
The authenticated user does not have permission to access the requested resource. Verify the Box account has access to the files you are trying to sync.

### redirect_uri_mismatch
The redirect URI configured in Shielva does not match what is registered in the Box Developer Console. They must be byte-for-byte identical. Check for trailing slashes or `http` vs `https` mismatches.

### 429 Rate limit exceeded
The Box API quota has been exceeded. The connector retries automatically with exponential backoff (1 s base, 2x factor, 32 s max). If this persists, check the Box API rate limit documentation and consider reducing sync frequency.

### Connector shows OFFLINE after install
The `client_id` or `client_secret` is missing or incorrect. Re-open the connector settings and re-enter both values. Also verify the app is not in a suspended state in the Box Developer Console.

### Files not appearing after sync
Confirm that:
1. The connected Box account has files in its storage.
2. The connector status shows **Connected** (not **Token expired**).
3. The correct knowledge base is selected during sync.
4. The Box app is authorized and not pending review.
