# Setup Instructions: Pinecone

## Overview

The Pinecone connector integrates your organization's Pinecone vector database with the Shielva platform. Once connected, Shielva can manage indexes, upsert and query vectors, and snapshot indexes as collections on your behalf. The connector authenticates with a Pinecone **API key** — there is no OAuth flow.

Pinecone exposes two API planes:

- **Control plane** (`https://api.pinecone.io`) — index and collection CRUD.
- **Data plane** (`https://{index_host}`) — vector upsert, query, fetch, delete, stats.

This connector handles both planes automatically: it fetches each index's host via `describe_index` on first use and caches the mapping for subsequent calls. The API key travels in the `Api-Key` header (NOT `Authorization`) on every request.

---

## Prerequisites

Before you begin, make sure you have:

- A **Pinecone account** with at least one project ([app.pinecone.io](https://app.pinecone.io)).
- An **API key** for the target project (Console → API Keys).
- The **environment** tag of the project (e.g. `us-east-1-aws`).
- (Optional) The **name of an existing index** if you want a default.

---

## Step-by-Step Configuration

### Step 1: API Key (`api_key`) — **Required**

1. Sign in at [app.pinecone.io](https://app.pinecone.io).
2. Select your project.
3. In the left sidebar, open **API Keys**.
4. Click **+ Create API key** (or copy an existing one).
5. Paste the key into the **Pinecone API Key** field in Shielva. This field is stored encrypted.

> **Important:** Pinecone sends the key in the `Api-Key` header — **NOT** `Authorization` with a Bearer prefix. The connector handles this automatically.

> **Tip:** API keys are scoped to a single Pinecone project. If you need to talk to multiple projects, install one Pinecone connector per project.

---

### Step 2: Environment (`environment`) — **Required**

1. In your Pinecone Console, open the **Project Settings** for the project the API key belongs to.
2. Copy the **Environment** tag — e.g. `us-east-1-aws`, `us-west1-gcp`, `gcp-starter`.
3. Paste it into the **Environment** field in Shielva.

This value is used as a fallback when building legacy data-plane hosts; modern serverless indexes return their host via `describe_index` automatically, but pod-based indexes may need the environment to construct the URL.

---

### Step 3: Project ID (`project_id`) — Optional

If your Pinecone tenant exposes the project UUID (newer dashboards do), paste it here. The connector uses it only as a fallback to build `https://{index}-{project_id}.svc.{environment}.pinecone.io` when the control plane is unreachable. Safe to leave blank.

---

### Step 4: Default Index (`default_index`) — Optional

- **Default:** *blank*.
- When set, API calls that omit `index_name` will fall back to this value. Leave blank to require an explicit index on every call.

---

### Step 5: Default Namespace (`default_namespace`) — Optional

- **Default:** `""` (the default namespace).
- Pinecone partitions vectors inside an index by namespace. Set this if your team uses a specific namespace by convention; leave blank to keep operations targeted at the default namespace unless overridden per-call.

---

### Step 6: Control Plane Base URL (`control_url`) — Optional

- **Default:** `https://api.pinecone.io`.
- Override only if your organization routes Pinecone API traffic through an approved egress proxy.

---

### Step 7: API Version (`api_version`) — Optional

- **Default:** `2025-01`.
- Sent as the `X-Pinecone-API-Version` header. Pin this to a known-good date so future Pinecone API changes do not break your integration.

---

### Step 8: Rate Limit (`rate_limit_per_min`) — Optional

- **Default:** `100` requests/min (Pinecone serverless plan default).
- Lower this if you hit 429 errors; raise it if your Pinecone plan permits more throughput.

---

## Testing the Connection

1. After saving credentials, the connector status badge should show **Connected** (green).
2. Click **Run Health Check** — it calls `GET /indexes` against the control plane. A successful response confirms the API key is valid.
3. Open **APIs → list_indexes** and click **Run** to see all indexes in the project.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | API key invalid or revoked | Regenerate the key in Pinecone Console → API Keys; update Shielva |
| `403 Forbidden` on health check | Key is valid but lacks permission | Check project membership; confirm key was created for the correct project |
| `404 Not Found` on `describe_index` | Index name typo or wrong project | Run `list_indexes` first; copy the exact `name` field |
| `429 Too Many Requests` during upsert | Plan rate limit exceeded | The connector retries with backoff; reduce `rate_limit_per_min` or upgrade your Pinecone plan |
| `No index specified and no default_index configured` | Call omitted `index_name` and no default was set | Pass `index_name` per call or set Default Index in Step 4 |
| Vector upserts succeed but counts are 0 | Wrong namespace | Confirm the namespace matches the one used at insert time |
| `describe_index returned no 'host' field` | Legacy pod index in an environment the API doesn't list | Confirm the index exists and is in the same project as the API key; set `environment` and `project_id` |
