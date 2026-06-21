# Setup Instructions: Insightly

## Overview

The Insightly connector integrates your Insightly CRM account with the Shielva platform. Once connected, Shielva can read and write contacts, organisations, opportunities, projects, leads, and tasks via the Insightly REST API. Authentication uses a single API key — no OAuth handshake is required, so the connector is ready to use as soon as you save the API key and POD region.

This connector requires an Insightly account with API access enabled. The free Insightly plan does **not** include API access — you must be on a paid plan or trial.

---

## Prerequisites

Before you begin, make sure you have:

- An **Insightly account** with API access (Plus, Professional, or Enterprise tier)
- Administrator permission to read and manage the records the connector will sync
- The Shielva platform open in another tab

---

## Step-by-Step Configuration

### Step 1: API Key (`api_key`) — **Required**

1. Sign in to [Insightly](https://crm.insightly.com).
2. Click your **profile avatar** in the top-right corner.
3. Choose **User Settings**.
4. Scroll down to the **API Key** section.
5. Click **Copy** next to the long alphanumeric key (format: `XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX`).
6. Paste this value into the **API Key** field in Shielva. This field is stored encrypted.

> **Important:** Treat the API key like a password. Anyone with the key can read and modify every record in your Insightly account. If it leaks, regenerate it from the same User Settings page — the old key stops working immediately.

---

### Step 2: POD Region (`pod`) — **Required**

Insightly hosts each customer on a regional pod. The pod is part of every URL when you are signed in to Insightly.

1. While signed in to Insightly, look at the URL in your browser address bar.
2. The pod is the subdomain before `.insightly.com` — for example, `https://crm.na1.insightly.com/...` means your pod is `na1`.
3. Common pod values:
   - `na1` — North America (default for most US/Canada accounts)
   - `eu1` — Europe
   - `ap1` — Asia-Pacific
4. Enter the three-character pod code (no dots, no protocol) into the **POD Region** field.

> **Common mistake:** If you see `404 Not Found` on every call, the pod is wrong. Double-check the URL while signed in.

---

### Step 3: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `60`
- Insightly's standard quota is roughly 10 requests per second per pod, but bursts above 60 requests per minute are routinely throttled with `429 Too Many Requests`.
- Lower this value if your plan has a tighter quota; raise it if you have a custom limit negotiated with Insightly support.

---

## Testing the Connection

1. After saving the API key and pod, the connector status badge should show **Connected** (green).
2. Click **Run Health Check** on the connector card — a successful check confirms the API key is valid and your pod is reachable.
3. Click **Sync Now** to pull the first batch of contacts. Check the sync log for the contact count and any errors.
4. To test writes, open **APIs → create_contact**, fill in `first_name` and `email`, and click **Run**. A successful response includes a new `CONTACT_ID`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | API key is wrong, expired, or revoked | Re-copy the key from User Settings and paste again |
| `404 Not Found` on every endpoint | POD region is wrong | Check the URL while signed in to Insightly and enter the correct pod (`na1`, `eu1`, …) |
| `429 Rate limit exceeded` repeatedly | Sync running too fast for your quota | Lower **Rate Limit** to 30 and retry |
| Sync finishes with zero contacts | The API key belongs to a sandbox or empty account | Verify in Insightly UI that contacts exist; check that the key has full account access |
| `Network error` in health check | Pod URL unreachable from this network | Confirm outbound HTTPS access to `api.{pod}.insightly.com` is allowed |
| Connector shows **Missing Credentials** | `api_key` or `pod` is blank | Fill in both required fields and click **Save** |
