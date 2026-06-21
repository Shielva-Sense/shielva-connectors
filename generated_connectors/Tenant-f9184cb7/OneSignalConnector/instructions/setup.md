# Setup Instructions: OneSignal

## Overview

The OneSignal connector lets Shielva send push notifications, manage apps, players (devices), and segments through your OneSignal account. It authenticates via two API keys defined by OneSignal:

- **REST API Key** — scoped to a single OneSignal app. Required.
- **User Auth Key** — scoped to your OneSignal account. Optional; required only if you want Shielva to manage apps (`list_apps`, `create_app`, `update_app`).

This connector talks to `https://api.onesignal.com`.

---

## Prerequisites

- A OneSignal account with at least one app configured ([dashboard.onesignal.com](https://dashboard.onesignal.com)).
- For push delivery: a Firebase Cloud Messaging (Android) or APNs (iOS) integration already set up inside that OneSignal app.
- Owner or admin role inside the OneSignal account if you want to generate a User Auth Key.

---

## Step-by-Step Configuration

### Step 1: REST API Key (`rest_api_key`) — **Required**

1. Sign in at [dashboard.onesignal.com](https://dashboard.onesignal.com).
2. Open the app you want Shielva to send notifications from.
3. Go to **Settings → Keys & IDs**.
4. Copy the **REST API Key**.
5. Paste it into the **REST API Key** field in Shielva. This field is stored encrypted.

> **Important:** The REST API Key authorizes everything notification-, player-, and segment-related for that **one** app. If you connect a second OneSignal app, install a second connector instance.

---

### Step 2: User Auth Key (`user_auth_key`) — **Optional**

Only needed if you want Shielva to list, create, or update OneSignal **apps**.

1. From any page in the OneSignal dashboard, click your account avatar (top-right).
2. Choose **Account & API Keys**.
3. Under **User Auth Key**, click **View** (you may be asked to confirm your password).
4. Copy the value and paste it into the **User Auth Key** field in Shielva.

> If you leave this blank, the connector still works for sending notifications and managing players/segments — only the `/apps` management endpoints are disabled.

---

### Step 3: Default App ID (`default_app_id`) — **Optional**

- The UUID of the OneSignal app the REST API Key belongs to. Found on the same **Settings → Keys & IDs** page as the REST API Key.
- When set, you can omit `app_id` on most action calls and the connector substitutes this value.
- Format: `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.

---

### Step 4: Base URL (`base_url`) — **Optional**

- **Default:** `https://api.onesignal.com`
- Leave blank unless OneSignal directs you to a regional endpoint.

---

### Step 5: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default:** `60` requests per minute.
- OneSignal generally throttles around this level for free accounts. Higher tiers may set this higher.

---

## Testing the Connection

1. After saving credentials, click **Connect** in the connector card. The status badge should turn green (**Connected**).
2. Click **Run Health Check**:
   - If `user_auth_key` is set, this calls `GET /apps`.
   - Otherwise it calls `GET /apps/{default_app_id}` with the REST API Key.
3. To test a real send, open **APIs → send_notification**, fill in:
   - `app_id`: your app UUID
   - `contents`: `{"en": "Hello from Shielva"}`
   - `included_segments`: `["Subscribed Users"]`
   Click **Run**. A response with `id` and `recipients` confirms delivery.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on `list_apps` | Wrong or missing User Auth Key | Re-copy from **Account & API Keys** in OneSignal |
| `401 Unauthorized` on `send_notification` | Wrong REST API Key or key belongs to a different app than `app_id` | Verify both — each app has its own REST API Key |
| `403 Forbidden` | Key revoked or account suspended | Regenerate the key in OneSignal and update the connector |
| `404 Not Found` on `get_app` / `get_notification` | Wrong `app_id` or wrong `notification_id` | Double-check IDs in the OneSignal dashboard |
| `429 Rate limit` | Sending too fast | The connector retries with backoff; raise your plan if it persists |
| `user_auth_key is required for /apps endpoints` | You called `list_apps` / `create_app` / etc. without configuring it | Set `user_auth_key` in Step 2 |
| `app_id is required and no default_app_id is configured` | Action invoked without `app_id` and no default set | Pass `app_id` explicitly or fill **Default App ID** (Step 3) |
