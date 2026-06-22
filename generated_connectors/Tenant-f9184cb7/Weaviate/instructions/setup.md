# Setup Instructions: Weaviate

## Overview

The Weaviate connector integrates a Weaviate vector database cluster with the Shielva platform. Shielva uses it to manage collections (classes), insert vectors and objects, and run `nearVector`, `nearText`, and BM25 semantic searches via the Weaviate v1 REST + GraphQL APIs.

This connector targets both **Weaviate Cloud** (which requires an API key) and **open / self-hosted instances** (which may not require auth).

---

## Prerequisites

- A running Weaviate cluster — either:
  - A Weaviate Cloud cluster at `https://<name>.weaviate.network`, OR
  - A self-hosted Weaviate instance reachable from the Shielva platform
- An **API key** if your cluster has anonymous access disabled (Weaviate Cloud always requires one)
- Optional: an **OpenAI API key** if your cluster uses the `text2vec-openai` vectorizer module

---

## Step-by-Step Configuration

### Step 1: Weaviate Cluster URL (`cluster_url`) — **Required**

1. Open the Weaviate Cloud Console (or your self-hosted dashboard) and copy your cluster's HTTPS endpoint.
   - Weaviate Cloud format: `https://<your-cluster>.weaviate.network`
   - Self-hosted format: `https://weaviate.internal.example.com` or `http://localhost:8080`
2. Paste this value into the **Weaviate Cluster URL** field in Shielva.

> **Tip:** Either include the `https://` scheme or omit it — the connector will normalize the URL either way.

---

### Step 2: Weaviate API Key (`api_key`) — **Optional**

- **Weaviate Cloud:** Required. Open your cluster → Details → API Keys → copy the active admin key.
- **Self-hosted:** Required only when you've enabled `AUTHENTICATION_APIKEY_ENABLED=true`.
- The connector sends this value as `Authorization: Bearer <api_key>` on every request.

> **Common mistake:** Pasting the cluster's read-only key when an admin-level key is needed for schema mutations. Use the key matching the operations you intend to run.

---

### Step 3: Default Class (`default_class`) — **Optional**

- A convenience hint for downstream actions that want a sensible default class name. Leave blank if your workflows always specify the class explicitly.

---

### Step 4: OpenAI API Key (`openai_api_key`) — **Optional**

- Required only if the target class is configured with the `text2vec-openai` vectorizer module.
- The connector forwards this as the `X-OpenAI-Api-Key` header so Weaviate can call OpenAI to generate embeddings on your behalf.
- Leave blank when using a `none` vectorizer (you supply vectors yourself) or any non-OpenAI module.

---

### Step 5: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `200`
- A soft cap on requests per minute the connector will attempt against the Weaviate API. Tune downward if you observe 429s.

---

## Completing Installation

1. Click **Save / Install** in the Shielva connector dashboard. The connector validates the cluster URL and persists the API key encrypted.
2. Click **Run Health Check** — this hits `GET /v1/.well-known/ready` on your cluster. A green badge confirms the cluster is reachable and the API key (if any) is valid.
3. Click **List Collections** to confirm the schema is queryable.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | Wrong or missing API key | Re-copy the API key from the Weaviate Cloud console |
| `404 Not Found` on `get_collection` | Class name doesn't exist | Verify capitalization — Weaviate class names are case-sensitive |
| `Connection refused` / network error | Cluster URL wrong, or cluster not reachable from the platform's network | Check the URL, firewall rules, and that the cluster is running |
| 429 rate-limit retries | Bursty traffic | Lower `rate_limit_per_min`, or upgrade your Weaviate Cloud plan |
| GraphQL `vectorizer "..." not found` | Class configured with a module not enabled on the cluster | Re-create the class with `vectorizer: "none"` and supply vectors yourself |
