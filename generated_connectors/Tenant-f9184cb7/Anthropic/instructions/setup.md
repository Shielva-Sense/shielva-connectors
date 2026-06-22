# Anthropic Connector — Setup

This connector talks to the public Anthropic (Claude) Messages API using a
single API key. No OAuth, no service-account JSON — just a `x-api-key`
header and the `anthropic-version` header on every request.

---

## 1. Create an API key

1. Sign in to the Anthropic Console at **https://console.anthropic.com**.
2. Open **Settings → API Keys** (direct link:
   <https://console.anthropic.com/settings/keys>).
3. Click **Create Key**. Give it a name that identifies the Shielva tenant
   that will own it (e.g. `shielva-acme-prod`).
4. Copy the value (`sk-ant-api03-…`) immediately — the Console only shows it
   once. Paste it into the Shielva connector **API Key** field.

> **Tip:** Create a separate key per Shielva tenant/environment. Anthropic
> rate limits, usage caps, and billing are tracked per key, so a per-tenant
> key gives you clean attribution and the ability to revoke without
> affecting other tenants.

---

## 2. Pick a workspace tier

Your account's **workspace tier** determines per-minute throughput and
per-day token budgets. Both are enforced on the Anthropic side; the
connector also enforces a client-side ceiling via `rate_limit_per_min`
to avoid wasted 429s.

| Tier   | Requests/min | Notes                              |
|--------|--------------|------------------------------------|
| Build  | 50           | Default; matches connector default |
| Scale  | 1,000        | Raise `rate_limit_per_min` to 1000 |
| Custom | Negotiated   | Set to your contracted ceiling     |

Current rate-limit reference:
<https://docs.anthropic.com/en/api/rate-limits>.

---

## 3. Configure the install fields

| Field                | Required | Default                        | Purpose                                          |
|----------------------|----------|--------------------------------|--------------------------------------------------|
| `api_key`            | yes      | —                              | Console key — sent as `x-api-key` header         |
| `anthropic_version`  | no       | `2023-06-01`                   | Value of the `anthropic-version` request header  |
| `base_url`           | no       | `https://api.anthropic.com/v1` | Override only for a private proxy                |
| `rate_limit_per_min` | no       | `50`                           | Client-side cap; match your workspace tier       |

### The `anthropic-version` header

Anthropic versions the API by date, not semver. The current stable date is
`2023-06-01` — leave the field blank to pin to it. Only change this if you
have a specific reason (e.g. opting into a preview version mentioned in
Anthropic's docs).

Reference: <https://docs.anthropic.com/en/api/versioning>.

---

## 4. Verify connectivity

After install, run **Health Check** from the connector page. It issues a
1-token ping to `POST /messages` using `claude-haiku-4-5` with
`max_tokens=1`. A green check means:

- The api key is valid and not revoked.
- The configured `base_url` is reachable from the Shielva pod.
- The `anthropic-version` header is accepted.

Health check cost is roughly 1 input + 1 output token — fractions of a
cent — so it is safe to schedule periodically.

---

## 5. Useful model + feature docs

- **Models overview:** <https://docs.anthropic.com/en/docs/about-claude/models>
- **Messages API reference:** <https://docs.anthropic.com/en/api/messages>
- **Message Batches:** <https://docs.anthropic.com/en/docs/build-with-claude/batch-processing>
- **Token counting:** <https://docs.anthropic.com/en/api/messages-count-tokens>
- **Streaming SSE:** <https://docs.anthropic.com/en/api/messages-streaming>
  (Use the lower-level HTTP client; the connector's `create_message`
  helper does not stream.)

---

## 6. Rotating the key

1. Create a new key in the Console (don't revoke the old one yet).
2. Edit the connector and paste the new value into **API Key**.
3. Run **Health Check** to confirm.
4. Revoke the old key from the Console.

This zero-downtime rotation is the recommended pattern — Anthropic
processes key revocation immediately, so a single-key swap risks a
~minute of failed requests.
