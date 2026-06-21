# Setup Instructions: OpenAI

## Overview

The OpenAI connector integrates your organization's OpenAI account with the Shielva platform. Once connected, Shielva can run chat completions, embed text into vectors, generate images, synthesize speech, moderate content, and manage uploaded files — all through the OpenAI REST API at `https://api.openai.com/v1`.

Authentication is API-key based. The connector sends `Authorization: Bearer <api_key>` on every request and optionally adds an `OpenAI-Organization: <organization_id>` header so usage is attributed to the correct billing org.

---

## Prerequisites

Before you begin, make sure you have:

- An **OpenAI account** with billing configured at [platform.openai.com](https://platform.openai.com).
- Permission to create **API keys** in that account.
- (Optional) An **Organization ID** if your account belongs to more than one org and you want to pin usage to a specific one.

---

## Step-by-Step Configuration

### Step 1: API Key (`api_key`) — **Required**

1. Sign in at [platform.openai.com](https://platform.openai.com).
2. Open the API keys page at **[platform.openai.com/api-keys](https://platform.openai.com/api-keys)**.
3. Click **+ Create new secret key**.
4. Give the key a clear name (for example `shielva-prod`) so you can revoke it later if needed.
5. (Recommended) Select **Restricted** and grant only the permissions Shielva needs: `Models — Read`, `Chat — Write`, `Embeddings — Write`, `Files — Write`, `Images — Write`, `Audio — Write`, `Moderations — Write`.
6. Click **Create secret key**. **Copy the key immediately** — OpenAI will never show it again.
7. Paste the key into the **OpenAI API Key** field in Shielva. The field is stored encrypted at rest.

> **Security note:** never paste an OpenAI API key into source code, a Slack message, or an unencrypted document. If a key is ever exposed, revoke it from the API keys page and create a new one.

---

### Step 2: Organization ID (`organization_id`) — **Optional**

1. Open the organization settings page at **[platform.openai.com/account/organization](https://platform.openai.com/account/organization)**.
2. Find the **Organization ID** field (format: `org-xxxxxxxxxxxxxxxxxxxxxxxx`). Click the copy icon.
3. Paste the value into the **OpenAI Organization ID** field in Shielva.

Leave this field blank if:

- Your account belongs to only one organization, **or**
- You want OpenAI to use your account's default org.

When set, every request will include the `OpenAI-Organization` header and usage will be attributed to (and billed against) that organization.

---

### Step 3: API Base URL (`base_url`) — **Optional**

- **Default:** `https://api.openai.com/v1`
- Override only if your organization routes OpenAI traffic through an approved proxy, or if you're pointing the connector at an OpenAI-compatible endpoint (for example a private model gateway).
- The connector appends paths like `/models`, `/chat/completions`, etc. directly to this base URL — do **not** include a trailing slash.

---

### Step 4: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default:** `60` requests per minute.
- The connector enforces this as an in-process token bucket on the outbound side, so requests are paced before they hit OpenAI. This is **independent of** OpenAI's server-side limits.
- Your actual ceiling depends on your tier — see [platform.openai.com/account/limits](https://platform.openai.com/account/limits). Set this value at or below your tier's RPM so you never see HTTP 429.
- If you do hit 429, the connector retries with exponential backoff and honours OpenAI's `Retry-After` header.

---

## Pricing

OpenAI charges per token (chat / embeddings) and per image or audio second. Live pricing is at [openai.com/api/pricing](https://openai.com/api/pricing). Set monthly **usage limits** on the Billing → Limits page so a runaway sync cannot exhaust your budget.

---

## Testing the Connection

1. After saving the credentials, click **Run Health Check** on the connector card. A successful response confirms both the API key and the network path are good.
2. Open **APIs → list_models** and click **Run** — you should see the model list returned in under a second.
3. Open **APIs → create_chat_completion**, set `model` to `gpt-4o-mini` and pass a small `messages` array (`[{"role": "user", "content": "Say hi"}]`). The response should include an assistant message.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | API key is invalid or revoked | Create a new key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys); paste it into the **OpenAI API Key** field; click **Save** |
| `401` with `"No such organization"` | `organization_id` does not match any org the key can access | Verify the org ID on [platform.openai.com/account/organization](https://platform.openai.com/account/organization), or clear the field to use the key's default org |
| `403` on `create_image` / `create_speech` | Restricted key was created without `Images — Write` / `Audio — Write` | Create a new key with the missing scope, or use a non-restricted key |
| `429 Too Many Requests` repeatedly | Outbound RPM exceeds your tier limit | Lower `rate_limit_per_min` below your tier's RPM cap |
| `429 Quota exceeded` | Hit the monthly spending cap | Raise the limit on the Billing → Limits page |
| `Network error: Connection refused` | Outbound network is blocked or proxy is down | Confirm `https://api.openai.com` is reachable from the Shielva host; if using `base_url`, check the proxy |
| Connector shows **Missing Credentials** | `api_key` is blank | Paste the key into the **OpenAI API Key** field and click **Save** |
