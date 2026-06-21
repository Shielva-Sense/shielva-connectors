# Setup Instructions: Elasticsearch

## Overview

The Elasticsearch connector integrates your cluster (self-hosted or Elastic Cloud) with the Shielva platform. Once connected, Shielva can manage indices, index and retrieve documents (single or bulk), run full Query DSL searches with aggregations, and maintain mappings.

The connector authenticates with either a Kibana-issued **API key** (preferred) or a **username + password** HTTP Basic pair. Both ride the standard `Authorization` header; the connector picks `ApiKey` whenever an API key is present.

For bulk operations the connector emits the canonical `application/x-ndjson` body Elasticsearch expects — paired action + source documents, newline-terminated.

---

## Prerequisites

- An Elasticsearch cluster reachable from the Shielva environment (port 443 for Elastic Cloud, 9200 for self-hosted)
- **Either** a Kibana-issued API key with the privileges your usage requires, **or** an Elasticsearch username + password (basic auth)
- (Optional) TLS verification disabled only for self-signed dev clusters — leave **enabled** in production

---

## Step-by-Step Configuration

### Step 1: Cluster URL (`host`) — **Required**

Paste the full URL of your cluster. Examples:

- Elastic Cloud: `https://my-cluster.es.us-central1.gcp.cloud.es.io:443`
- Self-hosted with TLS: `https://elasticsearch.internal.example.com:9200`
- Self-hosted dev (no TLS): `http://localhost:9200`

The URL **must** include the scheme (`http://` or `https://`) and the port.

### Step 2: API Key (`api_key`) — **Preferred**

In Kibana → **Stack Management** → **Security** → **API Keys** → **Create API Key**, copy the **Encoded** value (the long base64 string) and paste it here. The connector sends it as `Authorization: ApiKey <key>`.

Use API keys over basic auth wherever possible — they can be revoked individually, scoped with role descriptors, and don't expose a user password.

### Step 3: Username + Password (`username`, `password`) — *fallback*

Only fill these in if you can't issue an API key (e.g. local-dev cluster with basic security only). The connector sends `Authorization: Basic base64(user:pass)`.

If both an API key and a username+password pair are provided, the API key wins.

### Step 4: Verify TLS (`verify_ssl`) — **Default true**

Leave checked in production. Disable only for self-signed dev clusters; an attacker who can MITM your egress traffic can read every document you index when verification is off.

### Step 5: Rate Limit (`rate_limit_per_min`) — **Default 600**

Soft client-side cap on requests/min. Elasticsearch's hard limit depends on your cluster sizing and circuit-breaker config; 600 is a safe default that won't trip the standard breakers on a Basic-tier cluster.

---

## Test the connection

Click **Test connection** in the Shielva install wizard, or call `install()` programmatically. The connector probes `GET /` against your cluster; success returns `HEALTHY` + `CONNECTED`.

---

## Troubleshooting

| Symptom                                | Likely cause                                | Fix                                                                                                  |
|----------------------------------------|---------------------------------------------|------------------------------------------------------------------------------------------------------|
| `401 Unauthorized`                     | Wrong API key, expired key, wrong password  | Re-issue the API key in Kibana, or reset the basic-auth password. Update the connector config.        |
| `403 Forbidden`                        | Key/user lacks the required privileges      | Grant `manage` / `read` / `write` cluster + index privileges in Kibana role descriptors.              |
| `404 index_not_found_exception`        | Index doesn't exist                         | Call `create_index()` first, or correct the index name.                                              |
| `429 too_many_requests` / circuit_breaking | Cluster overloaded                       | Reduce `rate_limit_per_min`, use `bulk()` instead of single-document calls, or scale the cluster.    |
| `transport error … SSL: CERTIFICATE_VERIFY_FAILED` | Self-signed cert                  | Add the cluster CA to the Shielva trust store, **or** (dev only) disable `verify_ssl`.                |
| `transport error … Connection refused` | Wrong host/port, cluster down               | Re-check `host`, confirm the cluster is up and reachable from the Shielva network.                    |
| Egress firewall blocks Elastic Cloud   | Allowlist misses `*.cloud.es.io`            | Add the Elastic Cloud domain (and your region's data endpoint) to your egress allowlist.              |

---

## Security notes

- The `api_key` and `password` fields are stored encrypted at rest by the Shielva vault.
- The connector never logs request bodies, response bodies, or the `Authorization` header value.
- TLS verification is enabled by default; turning it off is a deliberate, audited action.
