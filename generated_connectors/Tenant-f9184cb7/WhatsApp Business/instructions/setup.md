# WhatsApp Business Connector — Setup Guide

Connect Shielva to the WhatsApp Business Cloud API (Meta) to sync message templates and phone number metadata.

---

## Prerequisites

- A [Meta Business Account](https://business.facebook.com/)
- A WhatsApp Business Account (WABA) created and approved by Meta
- A WhatsApp phone number registered to that WABA
- System Admin access in Meta Business Suite

---

## Step 1 — Locate your WhatsApp Business Account (WABA) ID

1. Go to [Meta Business Suite](https://business.facebook.com/)
2. Click **Settings** (gear icon) → **Business Settings**
3. In the left sidebar, under **Accounts**, click **WhatsApp Accounts**
4. Select your WABA — the numeric ID appears in the URL and in the account details panel (e.g. `9876543210`)

---

## Step 2 — Locate your Phone Number ID

1. Still in Business Settings → **WhatsApp Accounts** → select your WABA
2. Click **WhatsApp Manager** (or go directly to [WhatsApp Manager](https://business.facebook.com/wa/manage/phone-numbers/))
3. Select the phone number you want to use — the **Phone Number ID** is displayed beneath the number (e.g. `1234567890`)

This is different from the phone number itself (e.g. `+1 555-000-1234`). The connector requires the numeric ID.

---

## Step 3 — Create a System User and Generate a Permanent Access Token

> A System User token is preferred over a personal user token because it does not expire when a person leaves the organisation.

1. In [Meta Business Suite](https://business.facebook.com/) → **Settings** → **Business Settings**
2. Under **Users**, click **System Users**
3. Click **Add** → enter a name (e.g. `Shielva Connector`) → set role to **Admin**
4. Click **Add Assets** → select **WhatsApp Accounts** → choose your WABA → tick **Manage WhatsApp Business**
5. Click **Generate Token**:
   - Select the System User you just created
   - Set token expiration to **Never** (permanent token)
   - Enable the following permissions:
     - `whatsapp_business_management` (required — read templates and phone numbers)
     - `whatsapp_business_messaging` (optional — needed for sending messages)
   - Click **Generate Token**
6. Copy the token immediately — it is shown only once

---

## Step 4 — Install the Connector in Shielva

1. In Shielva ACP, navigate to **Integrations → WhatsApp Business**
2. Click **Connect**
3. Fill in the three fields:

   | Field | Value | Where to find it |
   |-------|-------|-----------------|
   | **Phone Number ID** | Numeric ID of the WhatsApp number | WhatsApp Manager (Step 2) |
   | **System User Access Token** | The permanent token generated in Step 3 | Meta Business Settings |
   | **WhatsApp Business Account ID** | Your WABA numeric ID | Business Settings (Step 1) |

4. Click **Install** — the connector will verify credentials by fetching the phone number details

---

## What Gets Synced

| Resource | Description |
|----------|-------------|
| **Message Templates** | All approved, rejected, and pending templates in the WABA — name, category, language, and all components (header, body, footer, buttons) |
| **Phone Numbers** | Display number, verified business name, quality rating, and connection status (via health check) |
| **WABA Details** | Account name, currency, timezone, and message template namespace |

Sync follows Meta's cursor-based pagination — all pages are retrieved automatically.

---

## Troubleshooting

### Error code 190 — "Invalid or expired access token"

The access token has been revoked or has expired.

**Fix:** Generate a new permanent token in Meta Business Settings → System Users → select the user → Generate Token. Update the connector credentials in Shielva ACP.

### Error code 100 — "Invalid parameter" or "phone_number_id not found"

The Phone Number ID is incorrect or the System User does not have access to it.

**Fix:**
- Confirm the Phone Number ID in WhatsApp Manager (it is the numeric ID, not the phone number string)
- Confirm the System User has **Manage WhatsApp Business** permission on the WABA that owns this number

### Rate limits (429)

Meta enforces rate limits on the Graph API. The connector retries automatically with exponential backoff (up to 3 attempts, honouring the `Retry-After` response header).

If rate limiting is persistent:
- Reduce sync frequency in Shielva
- Contact Meta support to request a rate limit increase

### Health check shows DEGRADED

The connector reached Meta's API but encountered a transient error (network timeout, 5xx from Meta).

**Fix:** Wait a few minutes and check the [Meta Platform Status](https://metastatus.com/) page. The connector will recover automatically when Meta's API is stable.

### Missing permission — `whatsapp_business_management`

When fetching templates, you see an error about insufficient permissions.

**Fix:** Edit the System User's token permissions in Business Settings → System Users → select the user → Edit → add `whatsapp_business_management` → regenerate the token.

---

## Security Notes

- The access token is stored encrypted in Shielva's vault — it is never logged or transmitted in plain text
- Use a System User token (not your personal Facebook token) to avoid access loss if a team member leaves
- Scope the System User's permissions to the minimum required: `whatsapp_business_management` for read-only connector use
- Rotate tokens periodically by generating a new one and updating the connector credentials

---

## Reference Links

- [Meta WhatsApp Business Platform Docs](https://developers.facebook.com/docs/whatsapp)
- [WhatsApp Manager](https://business.facebook.com/wa/manage/)
- [Meta Business Settings](https://business.facebook.com/settings/)
- [Graph API Explorer](https://developers.facebook.com/tools/explorer/)
- [Meta Platform Status](https://metastatus.com/)
