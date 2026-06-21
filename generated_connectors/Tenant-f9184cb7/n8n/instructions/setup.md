# Setup Instructions: n8n

## Overview

The n8n connector integrates your n8n workflow-automation instance — Cloud or self-hosted — with the Shielva platform. Once connected, Shielva can list and manage workflows, trigger activation / deactivation, inspect execution history, and manage credentials and tags on your behalf, all through n8n's public REST API.

This connector uses **API key** authentication. No OAuth flow is required.

---

## Prerequisites

Before you begin, make sure you have:

- An **n8n instance** you administer — Cloud (`https://yourorg.app.n8n.cloud`) or self-hosted.
- **Owner or admin role** on that instance (required to create an API key on most n8n editions).
- The instance reachable from the Shielva platform (no IP allow-list blocking, valid TLS certificate if HTTPS).

---

## Step-by-Step Configuration

### Step 1: n8n Instance URL (`instance_url`) — **Required**

This is the base URL where your n8n editor is served — the URL you visit in your browser to open the n8n UI.

- **Cloud example:** `https://yourorg.app.n8n.cloud`
- **Self-hosted example:** `https://n8n.example.com` or `http://10.0.0.5:5678`

Do not include the `/api/v1` suffix — the connector appends it automatically.

> **Tip:** Open the n8n editor in your browser and copy the URL up to the host name. Paste that into the **n8n Instance URL** field in Shielva.

---

### Step 2: API Key (`api_key`) — **Required**

1. In your n8n instance, click your avatar (top-right) → **Settings**.
2. In the left sidebar, open **n8n API**.
3. Click **Create an API key**.
4. Give the key a descriptive label such as `shielva-connector`.
5. n8n displays the key **once**. Copy it immediately.
6. Paste the value into the **API Key** field in Shielva. This field is stored encrypted.

> **Common mistake:** If you close the n8n dialog without copying the key, you cannot retrieve it later — you must create a new one and update Shielva.

---

### Step 3: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `60` (requests per minute)
- Leave blank unless your n8n instance has a custom rate-limit policy. The connector handles transient 429 / 5xx responses with automatic exponential-backoff retry, so most operators do not need to tune this.

---

## Testing the Connection

1. After saving your credentials, click **Connect** in the Shielva connector dashboard. The connector marks itself **Connected** immediately because API-key auth has no separate authorization phase.
2. Click **Run Health Check** on the connector card — this calls `GET /workflows?limit=1` against your instance. A success badge confirms both the URL and the API key are valid.
3. Open **APIs → list_workflows** and click **Run** with no filters. You should see the workflows visible to the API key.
4. To test write access, run **create_tag** with a name like `shielva-test` — then **list_tags** to confirm it appears.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | API key is wrong, expired, or revoked | Generate a new key in n8n → Settings → n8n API and update the connector. |
| `403 Forbidden` on a write call | The API key's owner lacks permission on the target workflow / credential | Confirm the user who created the key has owner / admin role; or recreate the key under a privileged user. |
| `404 Not Found` on `get_workflow` | The workflow ID does not exist or is in a project the API key cannot see | List workflows first to discover valid IDs. |
| `429 Too Many Requests` repeated | Sustained burst above the instance's rate limit | The connector retries transparently up to 3 times. If you see persistent 429s, reduce concurrent calls or raise your instance's rate limit. |
| Connector shows **Missing Credentials** | `instance_url` or `api_key` is blank | Fill in both required fields and click **Save**. |
| Network errors / timeouts | n8n instance unreachable from Shielva | Verify the instance URL is correct, the host is reachable, and no firewall / VPN blocks egress. |
| `instance_url` field accepts but health check fails | URL contains `/api/v1` or trailing `/` | The connector tolerates both — but if your instance is behind a reverse proxy, ensure the proxy rewrites `/api/v1/*` paths to the n8n container. |
