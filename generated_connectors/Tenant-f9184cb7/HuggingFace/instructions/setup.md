# Setup Instructions: HuggingFace

## Overview

The HuggingFace connector lets Shielva talk to both the **HuggingFace Hub** (list / search models, datasets, Spaces, organizations) and the **Inference API** (text generation, embeddings, classification, summarization, translation, image classification) using a single API token.

Authentication uses a HuggingFace user access token — no OAuth round-trip is required.

---

## Prerequisites

Before you begin, make sure you have:

- A **HuggingFace account** at [huggingface.co](https://huggingface.co)
- A **user access token** with **read** scope (sufficient for inference + public Hub reads) or **write** scope (required if you plan to push artifacts later)
- The model IDs you want to use against the Inference API — e.g. `meta-llama/Llama-3-8B-Instruct`, `sentence-transformers/all-MiniLM-L6-v2`, `facebook/bart-large-cnn`

---

## Step-by-Step Configuration

### Step 1: HuggingFace API Token (`api_token`) — **Required**

1. Sign in to [huggingface.co](https://huggingface.co).
2. Open [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens).
3. Click **New token**, give it a memorable name (e.g. `shielva-connector`), and select the **read** role (or **write** if you need to publish to the Hub).
4. Click **Generate a token**, then copy the value starting with `hf_…`.
5. Paste it into the **HuggingFace API Token** field in Shielva. The field is stored encrypted at rest.

> **Common mistake:** A `fine-grained` token scoped only to a single repo will not be able to call the Inference API. Use a classic `read` or `write` token unless you specifically need the scoping.

---

### Step 2: Hub Base URL (`hub_base_url`) — **Optional**

- **Default value:** `https://huggingface.co/api`
- Leave blank for huggingface.co. Override only if you run a private HuggingFace mirror (Enterprise Hub on-prem).

---

### Step 3: Inference Base URL (`inference_base_url`) — **Optional**

- **Default value:** `https://api-inference.huggingface.co/models`
- Leave blank for the public serverless Inference API.
- If you use **HuggingFace Inference Endpoints**, set this to your endpoint's hostname (the `/models/{id}` suffix in your inference calls will still be appended).
- If you self-host a TGI / TEI gateway, point this at the gateway base URL.

---

### Step 4: Default Model (`default_model`) — **Optional**

- Suggested value: `meta-llama/Llama-3-8B-Instruct` (text generation) or `sentence-transformers/all-MiniLM-L6-v2` (embeddings).
- This is a hint for clients of the connector — calls to `text_generation()`, `feature_extraction()`, etc. always require an explicit `model` parameter, but UIs can pre-fill from this default.

---

### Step 5: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `60` (requests per minute)
- HuggingFace's free-tier Inference API allows roughly 60 requests/minute per user. Pro and Enterprise plans get higher limits — raise this number to match your plan.

---

## Testing the Connection

1. After saving the token, click **Run Health Check** on the connector card. This calls `GET /whoami-v2` and confirms the token is valid.
2. Open **APIs → list_models** and try `filter=text-generation`, `sort=downloads`, `limit=5` — you should see the top trending generative models.
3. Open **APIs → text_generation**, set `model = gpt2`, `inputs = "Hello,"`, and click **Run**. A successful call returns an array of generated-text dicts.
4. The first call to a cold model may take 10–30 seconds while HuggingFace warms it up — the connector handles this automatically (it parses the `503 {"error": "Model X is currently loading", "estimated_time": N}` response and retries after `N` seconds).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | Token is wrong, revoked, or expired | Generate a new token at huggingface.co/settings/tokens and update the **HuggingFace API Token** field |
| `403 Forbidden` on model call | The model is gated and your account hasn't accepted its license | Visit the model page (e.g. `huggingface.co/meta-llama/Llama-3-8B-Instruct`), accept the license, and retry |
| `404 Not Found` on `get_model` | Misspelled or private model ID | Check the model URL on huggingface.co — IDs are case-sensitive |
| `503 Model is currently loading` retries forever | A very large model on the free tier may exceed the connector's `max_retries=3` window | Either pre-warm the model by calling it once outside Shielva, switch to a smaller variant, or move to **Inference Endpoints** with a dedicated GPU |
| `429 Too Many Requests` | Free-tier rate limit hit | Reduce concurrency, upgrade to Pro / Enterprise, or set `rate_limit_per_min` to a lower number to back off proactively |
| Inference response is `{"error": "Authorization header is invalid…"}` | Token missing the right scope | Regenerate with **read** (or **write**) scope, not fine-grained |
