# Miro Connector — Setup Guide

## Overview

The Miro connector syncs boards, sticky notes, cards, shapes, and text items from your Miro workspace into Shielva's knowledge base using the Miro REST API v2 with OAuth 2.0 Authorization Code authentication.

---

## Prerequisites

- A Miro account (Free, Team, Business, or Enterprise)
- Access to create developer apps in Miro (available on all plans)
- Your Shielva ACP instance with connector installation access

---

## Step 1 — Create a Miro Developer App

1. Log in to [miro.com](https://miro.com) with the account you want to use for the integration.
2. Click your **profile avatar** (top-right) → **Settings**.
3. In the left navigation under **Your profile**, click **Your apps**.
4. Click **Create new app**.
5. Fill in:
   - **App name**: `Shielva Connector` (or any descriptive name)
   - **Company**: your company or team name
6. Click **Create app**.

---

## Step 2 — Configure OAuth Scopes

In your newly created app:

1. Scroll to the **Permissions** section.
2. Enable the following OAuth scopes:
   - `boards:read` — read board metadata and content
   - `organizations:read` — read organization membership (optional, for team listing)
   - `team:read` — read team membership
3. Click **Save changes**.

---

## Step 3 — Set the Redirect URI

1. In your app settings, find the **Redirect URI for OAuth2.0** field.
2. Enter your Shielva ACP redirect URI. This is typically:
   ```
   https://<your-acp-host>/connectors/oauth/callback
   ```
3. Click **Save changes** (or the `+` button to add the URI).
4. Copy the exact URI you entered — you will need it during connector installation.

---

## Step 4 — Copy Credentials

From the app overview page:

- **Client ID** — visible at the top of the app settings page.
- **Client Secret** — click **Show client secret** and copy the value.

Keep the Client Secret private. It cannot be recovered — if lost, you must regenerate it.

---

## Step 5 — Install the Connector in Shielva ACP

1. In Shielva ACP, navigate to **Connectors** → **Add Connector** → **Miro**.
2. Enter:
   - **Client ID** — from Step 4
   - **Client Secret** — from Step 4
   - **Redirect URI** — the URI you registered in Step 3 (optional if using the default)
3. Click **Install**.
4. Click **Authorize** — you will be redirected to Miro's OAuth consent screen.
5. Select the Miro team/workspace to authorize and click **Allow access**.
6. You will be redirected back to ACP with the connector now connected.

---

## Step 6 — Board Sharing Settings

Miro's API only returns boards your authorized account can access. To ensure full coverage:

1. **Personal boards**: automatically included if the authorized account owns them.
2. **Team boards**: included when the authorized account is a team member.
3. **Shared boards**: ensure the authorized account has at least **View** access.
4. **Enterprise boards**: verify the account has the `boards:read` scope granted at the organization level.

For enterprise deployments, a service account (dedicated Miro account added as a member to all relevant teams) is recommended to avoid sync gaps when individual users leave.

---

## Step 7 — Verify the Connection

After installing:

1. Click **Health Check** in ACP — you should see `Connected — user: <your name>, team: <team name>`.
2. Click **Sync** to trigger the first synchronization.
3. Boards and items will appear in your knowledge base within minutes.

---

## Scopes Reference

| Scope | Purpose |
|-------|---------|
| `boards:read` | List and read board metadata and all item types (sticky notes, cards, shapes, text, frames, images) |
| `organizations:read` | Read organization structure (required for team listing via `/v2/orgs/{org_id}/teams`) |
| `team:read` | Read team membership and team-level board access |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Auth error (401)` | Access token expired or revoked | Re-authorize the connector in ACP |
| `Auth error (403)` | Insufficient scopes | Add missing scopes in Miro app settings and re-authorize |
| `Not found (404)` | Board deleted or access revoked | Boards removed from Miro will be skipped during next sync |
| `Rate limited (429)` | Too many requests | The connector retries automatically with exponential backoff |
| Boards missing from sync | Account not a member of the board's team | Add the authorized account to the relevant Miro team |

---

## Security Notes

- Client Secret and access tokens are stored AES-256-GCM encrypted in the Shielva vault.
- The connector uses read-only scopes — it cannot create, modify, or delete Miro content.
- Access tokens are short-lived; the connector handles refresh automatically when the SDK is present.
- For compliance, use a dedicated service account rather than a personal user account.
