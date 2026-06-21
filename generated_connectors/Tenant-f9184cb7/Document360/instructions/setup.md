# Setup Instructions: Document360

## Overview

The Document360 connector integrates your Document360 knowledge base with the Shielva platform. Once connected, Shielva can list and manage projects, versions, categories, and articles, and can sync published articles into your knowledge base for retrieval and search.

This connector uses Document360's static **API Token** authentication — there is no OAuth flow. You generate a token in your Document360 portal and paste it into Shielva.

---

## Prerequisites

Before you begin, make sure you have:

- A **Document360 account** with administrator or knowledge-base manager privileges
- At least one **project** created in Document360 (so the token has something to read)
- A clear sense of which **version** and **language** you want Shielva to default to (optional — you can set these later)

---

## Step-by-Step Configuration

### Step 1: API Token (`api_token`) — **Required**

1. Sign in to your Document360 portal at `https://<your-org>.document360.io/`.
2. In the left sidebar, click **Settings** → **API Tokens** (sometimes labelled **Knowledge Base Site API Tokens**).
3. Click **+ New API Token**.
4. Give the token a memorable name like **Shielva Integration**.
5. Grant the token the scopes Shielva needs: **Projects (read)**, **Versions (read)**, **Categories (read/write)**, **Articles (read/write/publish)**, **Search (read)**.
6. Click **Generate Token**. Document360 will show the token **once** — copy it immediately.
7. Paste the token into the **API Token** field in Shielva and click **Save**. This field is stored encrypted.

> **Tip:** If you lose the token you cannot retrieve it. Regenerate a new one in Document360 and paste it back into Shielva.

---

### Step 2: Default Project ID (`default_project_id`) — **Optional**

- Leave blank to let Shielva pick the first project the token can see.
- To pin a specific project: in Document360, open the project, copy the **Project ID** from the URL (`/projects/<project_id>/...`) or from **Settings → Project info**, and paste it here.

---

### Step 3: Default Version ID (`default_version_id`) — **Optional**

- Leave blank to walk **all** versions of the default project.
- To pin a specific version: in Document360 open the version, copy the **Version ID** from the URL or version settings, and paste it here.

---

### Step 4: Default Language Code (`default_language_code`) — **Optional**

- **Default value:** `en`
- Use the ISO 639-1 code of the language you want Shielva to sync (e.g. `en`, `fr`, `de`, `ja`, `es`).
- Only articles in this language are synced when the field is set. Leave blank to use `en`.

---

### Step 5: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default value:** `100`
- Document360's standard per-token quota is 100 requests per minute.
- If your plan grants a higher quota, enter the approved limit here. Shielva uses this as a hint for throttling during long syncs.

---

## Testing the Connection

1. After saving the API Token, click **Run Health Check** on the connector card. Shielva calls `GET /Projects` — a green tick confirms the token is valid and the Document360 API is reachable.
2. Click **List Projects** in the connector's API explorer to see what your token can read.
3. If you set a default project, click **Sync Now** to ingest the first batch of articles into the Shielva knowledge base.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 auth failed` on health check | API token revoked, regenerated, or wrong | Generate a new token in Document360 → Settings → API Tokens and paste it into Shielva |
| `403 auth failed` on a write call | Token missing the scope (e.g. **Articles → write**) | Regenerate the token with the required scopes and paste it back into Shielva |
| `404 not found` on get_project / get_article | Wrong ID, or the token does not have access to that project | Re-copy the ID from Document360 and confirm the token's project access |
| `429 rate limit` during sync | Burst exceeded the per-token quota | Shielva retries automatically with backoff. If it persists, lower the rate_limit_per_min or request a quota increase from Document360 |
| Connector shows **Missing Credentials** | api_token is blank | Fill in the API Token field and click **Save** |
| Sync completes but no articles ingested | default_project_id has no published articles in the chosen language | Verify language_code, publish at least one article, or leave default_version_id blank to walk all versions |
