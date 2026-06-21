# Setup Instructions: Cohere

## Overview

The Cohere connector integrates your organization's Cohere account with the Shielva platform. Once connected, Shielva can call Cohere's chat, embed, rerank, classify, tokenize, and model-discovery endpoints from any agent or workflow that needs LLM inference. The connector uses a long-lived API key — no OAuth dance is required.

This connector requires a Cohere account with at least one **Production** API key.

---

## Prerequisites

- A **Cohere account** at [dashboard.cohere.com](https://dashboard.cohere.com).
- A **Production API key** (trial keys also work but are rate-limited and not recommended for shared use).
- Network egress to `api.cohere.com` from your Shielva deployment.

---

## Step-by-Step Configuration

### Step 1: Cohere API Key (`api_key`) — **Required**

1. Open the [Cohere Dashboard](https://dashboard.cohere.com).
2. In the left sidebar, click **API Keys**.
3. Click **+ New Production Key** (or copy an existing one).
4. Copy the value — Cohere shows it only once.
5. Paste it into the **Cohere API Key** field in Shielva. The value is stored encrypted at rest.

> **Tip:** Trial keys are also accepted but are subject to a 20 request/min ceiling; production keys default to 100 req/min.

---

### Step 2: Cohere API Base URL (`base_url`) — **Optional**

- **Default:** `https://api.cohere.com/v2`
- Leave blank for the public Cohere API. Override only if your deployment routes Cohere traffic through an approved proxy or a Cohere-private endpoint.

---

### Step 3: Default Chat Model (`default_chat_model`) — **Optional**

- **Default:** `command-r-plus`
- The model used by the `chat` action when the caller does not specify one. Change to `command-r` for cheaper / faster inference.

---

### Step 4: Default Embed Model (`default_embed_model`) — **Optional**

- **Default:** `embed-v4.0`
- The model used by the `embed` action when the caller does not specify one.

---

### Step 5: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default:** `100`
- Matches the standard production tier. Raise only if Cohere has granted your account a higher tier.

---

## Testing the Connection

1. After saving, the connector status badge should show **Connected** (green) — the installer calls `GET /models` to verify the key.
2. Click **Run Health Check** on the connector card.
3. Open **APIs → chat**, send a single user message (`[{"role":"user","content":"hello"}]`) with `model=command-r-plus`, and click **Run**. A successful response includes `message.content[0].text` with the assistant reply.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on install | Invalid or revoked API key | Generate a new key in the Cohere dashboard and re-save |
| `404 Not Found` on `get_model` | Model id mismatched | Call `list_models` to discover valid ids |
| `429 Rate limited` repeatedly | Exceeded production-tier quota | Lower call concurrency, or request a higher tier from Cohere |
| `Network error` | Egress to `api.cohere.com` blocked | Allowlist `api.cohere.com:443` in your firewall / proxy |
| Connector shows **Missing Credentials** | `api_key` was left blank | Re-open install and paste the key |
