# Setup Instructions: Mattermost

## Overview

The Mattermost connector lets Shielva read and write to your self-hosted or
cloud Mattermost workspace using the official **REST v4 API**. Authentication
is done with a **Personal Access Token (PAT)** issued by Mattermost itself —
Shielva never receives your Mattermost password.

The token is sent as `Authorization: Bearer <token>` on every request. There is
no refresh cycle; the same token is used until it is manually revoked in
Mattermost.

---

## Prerequisites

Before you begin, make sure you have:

- A **Mattermost server URL** (e.g. `https://mattermost.acme.com`).
- A **Mattermost account** with permission to create a Personal Access Token,
  **or** a system-admin-provisioned **Bot Account** with an attached token.
- The **system administrator** of your Mattermost server has enabled Personal
  Access Tokens (System Console → Integrations → Integration Management →
  **Enable Personal Access Tokens**: `true`).

---

## Step-by-Step Configuration

### Step 1: Mattermost Server URL (`server_url`) — **Required**

1. Copy the base URL of your Mattermost server. Examples:
   - `https://mattermost.acme.com`
   - `https://chat.acme.com`
   - `http://192.168.1.50:8065` (self-hosted, internal)
2. Paste it into the **Mattermost Server URL** field in Shielva.

> **Do not** include the `/api/v4` path suffix — the connector adds it
> automatically. A trailing `/` is fine and will be stripped.

---

### Step 2: Personal Access Token (`personal_access_token`) — **Required**

1. In Mattermost, click your **profile picture** → **Profile**.
2. Go to the **Security** tab.
3. Scroll to **Personal Access Tokens** → **Create New Token**.
4. Give the token a descriptive name (e.g. `Shielva Integration`) and click
   **Save**.
5. Mattermost will show the **Access Token** value **once**. Copy it
   immediately.
6. Paste it into the **Personal Access Token** field in Shielva. This field is
   stored encrypted.

> **Bot account alternative:** A system admin can create a **Bot Account**
> (System Console → Integrations → Bot Accounts) and attach a token. Use that
> token here — same field, same behavior. Bot tokens are recommended for
> production because they outlive the human user.

> **Common mistake:** If you regenerate or revoke the token in Mattermost, you
> must update this field in Shielva — the old token immediately stops working.

---

### Step 3: Default Team ID (`default_team_id`) — **Optional**

- Leave blank to require the team ID explicitly on every action.
- To prefill it, copy the **Team ID** from Mattermost: open the team, click the
  **team name** at the top-left → **View Members** → URL contains `/teams/<id>`,
  or use System Console → Teams → click the team.

---

### Step 4: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default:** `200` requests per minute.
- This is a soft cap that the connector observes between calls. The connector
  also respects `Retry-After` headers from Mattermost when the server itself
  returns `429`.

---

## Testing the Connection

1. Click **Save** in the Shielva connector panel. The installer will
   immediately probe `GET /users/me` to verify the URL + token.
2. The status badge should turn **Connected** (green). The message will read
   `Connected to Mattermost as <username>`.
3. Click **Run Health Check** to re-verify on demand.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on install | Wrong or revoked token | Regenerate the PAT in Mattermost and re-enter it |
| `404 Not Found` on install | Wrong `server_url`, or `/api/v4` not exposed | Confirm the URL works in a browser; remove any path suffix |
| `403 Forbidden` on `post_message` | Token user lacks channel membership | Add the user to the channel, or use a bot account |
| Connector shows **Missing Credentials** | One of `server_url` or `personal_access_token` is blank | Fill in both required fields |
| `Personal Access Tokens are disabled` error | System admin has not enabled PATs | Ask the system admin to flip the toggle in System Console |
| Frequent 429s during bulk sync | Concurrent jobs exceeding quota | Lower `rate_limit_per_min`; the connector will back off automatically |
