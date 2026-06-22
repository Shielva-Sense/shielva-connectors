# Setup Instructions: Nutshell

## Overview

The Nutshell connector links your Nutshell SMB sales CRM with the Shielva
platform. Once connected, Shielva can list and manage contacts, leads,
accounts, activities, and users via Nutshell's JSON-RPC 2.0 API.

Authentication is **HTTP Basic** — your Nutshell login email is the username
and an **API key** issued from your Nutshell account is the password. The
connector never stores your account password and never uses interactive
OAuth.

---

## Prerequisites

- A **Nutshell account** with **administrator** (Owner or Administrator)
  permission on the workspace you want to connect.
- The login email of a user who can act on the Nutshell records you want
  Shielva to read or write.
- API access enabled for your Nutshell plan (Pro and above include
  API access; Starter accounts may need to upgrade).

---

## Step-by-Step Configuration

### Step 1: Nutshell Login Email (`username`) — **Required**

1. Open Nutshell at <https://app.nutshell.com> and sign in.
2. Use the **email address** of the user account whose API key you will
   issue in Step 2. Common mistake: pasting the workspace name here — the
   field requires the user's email.
3. Paste the email into the **Nutshell Login Email** field in Shielva.

---

### Step 2: Nutshell API Key (`api_key`) — **Required**

1. In Nutshell, click your avatar (top-right) → **Setup**.
2. In the **Setup** menu, choose **API keys** (under *Integrations*).
3. Click **+ New API Key**.
4. Give the key a descriptive name (e.g. `Shielva Integration`) and click
   **Create**.
5. Nutshell displays the key **only once**. Copy it immediately.
6. Paste the key into the **Nutshell API Key** field in Shielva. This
   field is stored encrypted.

> **Tip:** If you rotate the API key in Nutshell, you must also update
> this field in Shielva — the previous key is revoked instantly.

---

### Step 3: Base URL (`base_url`) — **Optional**

- **Default:** `https://app.nutshell.com/api/v1/json`
- Leave blank unless your organization has been migrated to a regional
  Nutshell pod by Nutshell support. The connector uses JSON-RPC 2.0
  exclusively against this single endpoint.

---

### Step 4: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default:** `60` requests per minute
- Nutshell publishes a standard rate of approximately 60 RPM per API key.
  Lower this value if you share the same key across multiple Shielva
  connectors so requests don't queue at the gateway.

---

## Testing the Connection

1. Click **Save** in the Shielva connector dashboard.
2. Click **Run Health Check** on the connector card. A successful check
   means Shielva can reach `app.nutshell.com` and your credentials are valid.
3. Click **List Contacts** in the **APIs** panel to confirm the connector
   can read records. The first page (50 contacts) should return within a
   couple of seconds.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | Wrong username or revoked API key | Re-issue an API key (Step 2) and re-save |
| `Authentication failed: ...` on install | Username is not the user's email, or trailing whitespace in the API key | Re-copy both fields without surrounding spaces |
| Sporadic `429 Rate limited` | Multiple integrations share the same API key | Lower `rate_limit_per_min` or mint a dedicated key for Shielva |
| `Method not found` JSON-RPC error | Nutshell plan does not include API access | Upgrade the Nutshell plan or contact Nutshell support |
| `Not Found` on get/update contact | The contact ID was deleted or belongs to a different workspace | Confirm the ID in Nutshell and that the API key user can see it |
| Connector shows **Missing Credentials** | `username` or `api_key` is blank | Fill in both required fields and click **Save** |
