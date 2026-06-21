# Qdrant Connector — Setup

This connector talks to a Qdrant cluster (Qdrant Cloud or self-hosted) over
the REST API using a single API key. No OAuth, no service-account JSON —
just the lowercase `api-key` header on every request.

---

## 1. Provision a cluster

### Qdrant Cloud (recommended)

1. Sign in to **https://cloud.qdrant.io**.
2. **Clusters → Create Cluster**. Pick region + tier (the free tier is enough
   to validate the connector).
3. When the cluster is **Healthy**, open it and copy the **Cluster URL** —
   it looks like `https://<cluster-id>.<region>.aws.cloud.qdrant.io:6333`.
   Paste that into the connector's **Cluster URL** field.

### Self-hosted

If you run Qdrant yourself, the URL is whatever your operator
publishes — the connector treats it as an opaque string. Anything
TLS-terminated (`https://…`) is fine; HTTP-only listeners are
discouraged because the api-key travels as a plain header.

---

## 2. Create an API key

1. In the Qdrant Cloud Dashboard open **API Keys → Create**. Choose
   **read-write** access (the default) so the connector can perform both
   search and upsert calls.
2. Name the key after the Shielva tenant that will own it
   (e.g. `shielva-acme-prod`).
3. Copy the value (`qdr-…`) immediately — the Dashboard only shows it
   once. Paste it into the connector's **API Key** field.

> **Tip:** Create one key per Shielva tenant / environment. Qdrant Cloud
> usage, audit logs, and revocation are scoped per key, so a per-tenant
> key gives clean attribution and the ability to revoke without
> impacting other tenants.

---

## 3. Configure the install fields

| Field                | Required | Default | Purpose                                              |
|----------------------|----------|---------|------------------------------------------------------|
| `cluster_url`        | yes      | —       | Your cluster URL (e.g. `https://…:6333`)             |
| `api_key`            | yes      | —       | Cloud Dashboard key — sent as the `api-key` header   |
| `default_collection` | no       | —       | Default collection for shorthand calls               |
| `rate_limit_per_min` | no       | `600`   | Client-side cap on requests/min; match your tier     |

The `cluster_url` is tenant-specific — Qdrant Cloud assigns each cluster
its own subdomain, so there is no public default to fall back to.

---

## 4. Verify connectivity

After install, run **Health Check** from the connector page. It issues
`GET /` against the cluster, which is the canonical low-cost probe:

- It returns even when no collections exist.
- It requires a valid `api-key` header, so a green check proves the key works.
- The response body carries Qdrant's build version, which the connector
  surfaces in the health result `details.version` field.

A failed health check with `kind=auth` means the key is wrong or revoked;
`kind=network` means the cluster URL is not reachable from the Shielva pod
(check DNS, security groups, and that the URL includes `:6333`).

---

## 5. Useful endpoints

Once installed the connector exposes 17 endpoints in addition to
`install` and `health_check`:

| Concern        | Endpoints                                                       |
|----------------|-----------------------------------------------------------------|
| Cluster        | `cluster_info`                                                  |
| Collections    | `list_collections`, `get_collection`, `create_collection`, `delete_collection`, `update_collection` |
| Point writes   | `upsert_points`, `delete_points`                                |
| Point reads    | `retrieve_points`, `search_points`, `batch_search`, `recommend`, `scroll`, `count_points` |
| Indexes        | `create_payload_index`                                          |
| Snapshots      | `create_snapshot`, `list_snapshots`                             |

### Creating your first collection

```python
await connector.create_collection(
    collection_name="shielva-kb",
    vectors={"size": 768, "distance": "Cosine"},  # match your embedding model
    on_disk_payload=True,
)
```

### Upserting points

```python
await connector.upsert_points(
    collection_name="shielva-kb",
    points=[
        {"id": 1, "vector": [0.12, 0.84, ...], "payload": {"tenant_id": "t-1"}},
        {"id": 2, "vector": [0.41, 0.07, ...], "payload": {"tenant_id": "t-1"}},
    ],
    wait=True,  # block until persisted; set False for fire-and-forget bulk loads
)
```

### Filtered similarity search

```python
await connector.search_points(
    collection_name="shielva-kb",
    vector=[0.10, 0.42, ...],
    limit=5,
    score_threshold=0.7,
    filter={"must": [{"key": "tenant_id", "match": {"value": "t-1"}}]},
)
```

> **Multi-tenant note:** Qdrant has no native tenant scoping. Always
> include `tenant_id` (or whatever your tenant key is) in the point
> payload and pass it as a `must` clause in the filter — and create a
> payload index on that field with
> `create_payload_index(..., field_name="tenant_id", field_schema="keyword")`
> so the filter is fast.

---

## 6. Rotating the key

1. Create a new key in the Cloud Dashboard (don't revoke the old one yet).
2. Edit the connector and paste the new value into **API Key**.
3. Run **Health Check** to confirm.
4. Revoke the old key from the Dashboard.

This zero-downtime rotation is the recommended pattern — Qdrant Cloud
processes key revocation immediately, so a single-key swap risks a
~minute of failed requests.

---

## 7. Reference docs

- **Qdrant API reference:** <https://api.qdrant.tech/>
- **Qdrant Cloud Dashboard:** <https://cloud.qdrant.io>
- **Search filtering:** <https://qdrant.tech/documentation/concepts/filtering/>
- **Payload indexes:** <https://qdrant.tech/documentation/concepts/indexing/>
- **Snapshots:** <https://qdrant.tech/documentation/concepts/snapshots/>
- **Multi-tenancy patterns:** <https://qdrant.tech/documentation/tutorials/multiple-partitions/>
