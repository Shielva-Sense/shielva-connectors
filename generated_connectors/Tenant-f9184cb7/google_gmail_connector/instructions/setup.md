# Setup Instructions: Google Gmail

## Overview

The Google Gmail connector allows the Shielva platform to ingest email messages from a Gmail account, keep the knowledge base in sync as messages are added or deleted, and perform message management operations such as trashing or permanently deleting messages and threads. It is intended for administrators who want to make a Gmail inbox searchable and manageable through the Shielva platform.

---

## Prerequisites

Before you begin, make sure you have:

- A Google account with access to the Gmail inbox you want to connect.
- Permission to authorize third-party OAuth 2.0 applications in that Google account (some organizations restrict this via Google Workspace admin policy — check with your Google Workspace administrator if authorization fails).
- Admin access to the Shielva platform to add and configure connectors.

---

## Step-by-Step Configuration

### Step 1: Authorize Gmail Access (`allow_permanent_delete`)

**Field label**: Allow Permanent Delete  
**Field key**: `allow_permanent_delete`  
**Type**: Boolean (toggle)  
**Required**: No (defaults to disabled)

This setting controls whether the connector is allowed to **permanently delete** messages and threads from Gmail. When disabled (the default), the connector can only move messages to the Trash, which keeps them recoverable for 30 days. When enabled, the connector gains the ability to call the Gmail `messages.delete` and `threads.delete` endpoints, which remove messages immediately and irreversibly.

**How to configure:**

- If you only need soft-delete (Trash) behavior, leave this toggle **off**. No additional action is needed.
- If your use case requires permanent, unrecoverable deletion, set this toggle to **on**.

**Important:** Enabling permanent delete causes the connector to request an additional OAuth scope (`https://mail.google.com/`) during the authorization step. If you change this setting after the initial authorization, you must re-authorize the connector so Google issues a new access token with the updated scope.

---

### Step 2: Authorize the Connector

This connector uses **OAuth 2.0 Authorization Code** flow. You do not need to enter an API key or client secret — the Shielva platform manages the credentials automatically.

1. After saving your configuration, click the **Connect** button on the connector settings page.
2. You will be redirected to the Google sign-in page. Sign in with the Gmail account you want to connect.
3. Google will display a consent screen listing the permissions the connector is requesting:
   - `https://www.googleapis.com/auth/gmail.modify` — read and move messages to Trash (always requested)
   - `https://mail.google.com/` — permanent delete (only requested if **Allow Permanent Delete** is enabled)
4. Click **Allow** to grant access.
5. You will be redirected back to the Shielva platform, and the connector status will change to **Connected**.

**Tip:** If the consent screen shows "This app isn't verified," this is a Google warning for apps in development or review. Click **Advanced** → **Go to Shielva (unsafe)** to proceed if your organization's IT team has pre-approved this connector. For production deployments, the connector should be Google-verified and this warning will not appear.

---

## Testing the Connection

After completing the authorization step:

1. On the connector settings page, click **Check Health** (or **Test Connection**).
2. A successful health check returns a **Connected** status and displays the email address of the connected Gmail account (for example, `Connected as user@example.com`).
3. Run a manual **Sync** from the connector dashboard to verify that messages are being ingested into your knowledge base. The sync result will display the number of messages found and successfully ingested.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Health check shows **Token Expired** | The OAuth access token has expired and cannot be refreshed. | Click **Re-authorize** on the connector settings page and complete the Google consent flow again. |
| Health check shows **Insufficient Scopes** | The connected account does not have the required OAuth scopes. | Click **Re-authorize** to go through the Google consent flow. Make sure to click **Allow** on the consent screen. If you changed the **Allow Permanent Delete** setting, re-authorization is required to request the new scope. |
| Authorization fails immediately after clicking **Connect** | The Google account or Workspace organization restricts third-party OAuth applications. | Contact your Google Workspace administrator and ask them to allow OAuth access for the Shielva connector client ID. |
| Permanent delete operations return a **Permission Denied** error | Permanent delete is enabled in config, but the OAuth token was issued before the `https://mail.google.com/` scope was requested. | Re-authorize the connector so a new token is issued with the full `mail.google.com` scope. |
| Sync returns zero documents | The Gmail inbox may be empty, or the query filter may not match any messages. | Check the sync log for the `q=` query parameter used. For a full sync, trigger it with **full sync** mode from the dashboard. |
| Sync shows deleted messages still in knowledge base | Deletion propagation requires at least one successful incremental sync after the messages were removed from Gmail. | Wait for the next scheduled sync or trigger a manual sync. The connector diffs the previous known IDs against the current Gmail list response and removes stale entries. |
