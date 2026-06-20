# Heap Analytics Connector — Setup Guide

## Overview

The Heap Analytics connector integrates with Heap's REST API and Server-Side API to sync users, events, segments, and user properties into Shielva. It also supports server-side event tracking and user identification.

---

## Prerequisites

- An active Heap account with at least **Viewer** access
- API access enabled in your Heap project (see below)

---

## Step 1: Obtain your API Key

1. Log in to your Heap account at [https://heapanalytics.com](https://heapanalytics.com)
2. Navigate to **Account** (top-right avatar menu) → **Account Settings**
3. Go to the **Privacy & Security** tab
4. Scroll to the **API Keys** section
5. Copy the **API Key** (this is your Bearer token for REST API calls)

> If no API key exists, click **Generate API Key**. The key will only be shown once — store it securely.

---

## Step 2: Find your App ID (Account ID)

The **App ID** is your Heap application identifier. You can find it in multiple places:

1. **Dashboard URL** — When logged into Heap, your URL contains your App ID: `https://heapanalytics.com/app/{APP_ID}/...`
2. **Account Settings** → **General** → **App ID**
3. **In your Heap snippet** — The JavaScript snippet in your codebase will contain: `heap.load("{APP_ID}")`

> The App ID is a numeric string (e.g., `3887229184`). This is required for server-side API calls.

---

## Step 3: Install the Connector

In the Shielva connector setup form:

| Field | Value |
|-------|-------|
| **API Key** | Bearer token from Step 1 |
| **App ID / Account ID** | App ID from Step 2 |

---

## API Details

### REST API (read operations)
- Base URL: `https://heapanalytics.com/api/`
- Authentication: `Authorization: Bearer {api_key}`
- Resources: users, events, segments, user_properties

### Server-Side API (write operations)
- Endpoint: `POST https://heapanalytics.com/api/track` and `/api/identify`
- Authentication: `app_id` in request body + Bearer token in header
- Track events: sends named events with optional properties to a user identity
- Identify users: associates a user identity with a set of user properties

---

## Rate Limits

Heap enforces per-account rate limits on API requests:

- **REST reads**: Limited by your Heap plan tier. Heavy usage may trigger 429 responses.
- **Server-Side (track/identify)**: More permissive, designed for high-volume event ingestion.
- When a `429 Too Many Requests` response is received, the connector will automatically retry with exponential backoff, honouring the `Retry-After` header if present.
- For sustained high-volume use, consider Heap's server-side bulk import API or contact Heap support to increase limits.

---

## Data Synced

| Resource | Description |
|----------|-------------|
| **Users** | Identified users and their properties |
| **Events** | Aggregated event counts (last 30 days by default) |
| **Segments** | All defined user segments with counts |
| **User Properties** | Per-user custom properties |

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Authentication failed (401)` | Invalid API key | Re-generate and paste the correct API key |
| `Authentication failed (403)` | API access not enabled for this plan | Contact Heap support to enable API access |
| `App ID not found` | Wrong App ID | Double-check the App ID in Heap dashboard settings |
| `Rate limited (429)` | Too many requests | The connector retries automatically; reduce sync frequency if persistent |
| `Connection error` | Network/firewall | Ensure egress to `heapanalytics.com:443` is allowed |

---

## Further Reading

- [Heap REST API Reference](https://developers.heap.io/reference/)
- [Heap Server-Side API](https://developers.heap.io/reference/server-side-api)
- [Heap Privacy & Security](https://help.heap.io/privacy-and-security/)
