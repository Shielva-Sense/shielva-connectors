# Setup Instructions: Algolia

## Overview

The Algolia connector integrates your organization's Algolia application with the Shielva platform. Once connected, Shielva can list and manage indices, push documents (single or bulk), run keyword and federated searches, replace settings, and orchestrate indexing tasks via task polling.

The connector authenticates with two values supplied by Algolia:

- **Application ID** — public identifier for your Algolia application (not secret)
- **Admin API Key** — full read/write credential (treat as a secret)

It uses Algolia's official high-availability host-rotation pattern: read requests go to `<app>-dsn.algolia.net` first and then to shuffled `algolianet.com` fallback hosts; write requests go to `<app>.algolia.net` first with the same fallback ring. A 5xx response or transport error on one host transparently falls through to the next.

---

## Prerequisites

Before you begin, make sure you have:

- An **Algolia account** with administrator access to the application you want to connect
- The **Application ID** for that application (Algolia Dashboard → Settings → API Keys)
- The **Admin API Key** for that application (same page; click **Show** to reveal)
- *(Optional)* A **search-only API key** if your security policy requires defense-in-depth on read paths

---

## Step-by-Step Configuration

### Step 1: Application ID (`application_id`) — **Required**

1. Open the [Algolia Dashboard](https://dashboard.algolia.com/).
2. In the top-left, make sure the correct application is selected.
3. Open **Settings → API Keys** (or **API Keys** in the sidebar, depending on dashboard version).
4. Copy the **Application ID** value (a short uppercase alphanumeric string, e.g. `LATENCY`).
5. Paste it into the **Application ID** field in Shielva.

> The Application ID is not a secret — it appears in every HTTP request to Algolia and is safe to copy into a text editor.

---

### Step 2: Admin API Key (`admin_api_key`) — **Required**

1. On the same **API Keys** page, locate the **Admin API Key** row.
2. Click **Show** to reveal the key, then click the copy icon.
3. Paste it into the **Admin API Key** field in Shielva. This field is stored encrypted.

> The Admin API Key grants **full read and write access** to every index on the application — including delete. Never paste it into a public document, chat, or commit. If you suspect it's been leaked, regenerate it from the dashboard and update the Shielva field immediately — the old key stops working instantly.

---

### Step 3: Search-only API Key (`search_api_key`) — **Optional**

- Leave blank to have the connector use the Admin API Key for both read and write paths.
- For defense-in-depth, generate a **search-only key** in Algolia Dashboard → API Keys → **+ New API Key**, restrict it to the indices you want searched, and paste it here. The connector will use this key for `list_indices`, `search`, `multi_search`, and other read paths; writes always use the Admin API Key.

---

### Step 4: Default Index (`default_index`) — **Optional**

- A friendly identifier for the dashboard. The connector does **not** auto-route operations to this index — every API call still takes an `index_name` parameter explicitly. Leave blank if you don't have a single "primary" index.

---

### Step 5: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `100` (requests per minute)
- Algolia's hard quota depends on your plan tier. Leave at `100` unless you've confirmed a higher limit on your plan. Lowering this number throttles connector-initiated requests further.

---

## Testing the Connection

1. Click **Install** in the Shielva connector dashboard. The connector probes `GET /1/indexes` to verify the credentials work; on success the status badge turns **Connected** (green).
2. Click **Run Health Check** — a successful check confirms the API is reachable and the Admin API Key is still valid.
3. Open **APIs → list_indices** and click **Run** — you should see a JSON envelope listing your indices.
4. To test writes, open **APIs → save_object**, supply an `index_name` (e.g. `test_index`) and an `object_data` payload like `{"title": "hello", "tags": ["demo"]}`, and click **Run**. The response includes a `taskID` and the generated `objectID`.
5. *(Optional)* Open **APIs → wait_task** with the same `index_name` and the `taskID` from the previous step to confirm the indexing task reached `published` status.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` / `403 Forbidden` | Wrong Application ID, wrong Admin API Key, or the Admin API Key was rotated | Copy both values fresh from Algolia Dashboard → API Keys and re-install the connector |
| Connector shows **Invalid Credentials** | Same as 401/403 | Same fix |
| Connector shows **Offline** with "Algolia unreachable" | Network blocked from Shielva to `*.algolia.net` and `*.algolianet.com` | Allowlist both domains in your egress firewall — Algolia rotates between them for HA |
| `404` on **wait_task** | Task ID belongs to a different index, or the task expired (Algolia retains task status for ~24h) | Re-issue the operation and use the new task ID |
| Search returns 0 hits | Index is empty, attributes aren't searchable, or `filters` is too restrictive | Verify with `list_indices` and `get_settings`; check `searchableAttributes` |
| `429 Rate Limit` errors during bulk indexing | Algolia operations-per-second limit on your plan | Lower `rate_limit_per_min`, batch fewer objects per `save_objects` call, or upgrade your plan |
| Connector shows **Missing Credentials** | `application_id` or `admin_api_key` is blank | Fill in both required fields and click **Save** |

---

## Security notes

- The Admin API Key is the only secret in this connector — it is stored encrypted at rest. If you accidentally paste it into a non-secret field (or a screenshot), **regenerate it in Algolia** immediately.
- If you set a search-only key (Step 3), make sure its index-name restriction matches the indices you want the connector to read.
- The connector never logs the Admin API Key or the search-only key — only the Application ID appears in audit logs for correlation.
