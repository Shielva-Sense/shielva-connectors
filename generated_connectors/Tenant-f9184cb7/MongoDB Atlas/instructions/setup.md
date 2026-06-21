# MongoDB Atlas connector — setup

This connector talks to the MongoDB Atlas **Admin API v2** (control plane: clusters, projects, database users, network access, alerts). It does NOT proxy MongoDB wire-protocol traffic.

## 1. Create an Atlas Programmatic API Key

1. Sign in to https://cloud.mongodb.com.
2. Pick the **Organization** you want this connector to manage.
3. **Access Manager → API Keys → Create API Key**.
4. Give it a descriptive name (e.g. `shielva-atlas-connector`).
5. Grant roles — for full management capability, choose **Organization Owner** (or **Organization Project Creator** + per-project **Project Owner** as a tighter alternative).
6. Click **Next**. Atlas shows you the **Public Key** and **Private Key**.
7. **Copy the private key now** — Atlas only displays it once.

## 2. Whitelist the caller IP

On the same API Key detail page, scroll to **Access List** and add the egress IP(s) of the host that will run the connector (or `0.0.0.0/0` for unrestricted — discouraged in production).

## 3. Install the connector in Shielva

1. In the Shielva ARC connectors UI, install **MongoDB Atlas**.
2. Paste:
   - **Atlas Public Key** — from step 1.
   - **Atlas Private Key** — from step 1.
   - **Default Organization ID** (optional) — the 24-char hex shown in the URL when viewing the org.
   - **Default Project ID** (optional) — the 24-char hex shown in the URL when viewing the project.
   - Leave **Base URL** and **API Version** at defaults unless you have a private Atlas deployment.
3. Click **Install**, then **Run Health Check**. A green status confirms the Digest credentials are valid.

## 4. Verify

```bash
# From the gateway, call list_orgs to confirm the credentials work end-to-end.
curl -X POST http://localhost:8000/connectors/<connector_id>/methods/list_orgs \
     -H 'X-Shielva-Tenant: <tenant_id>' \
     -d '{"items_per_page": 5}'
```

You should see the orgs your API key has access to.

## 5. Common Atlas error codes

| Status | Meaning |
|--------|---------|
| 401    | Bad public/private key, or Digest challenge failed |
| 403    | Key authenticated but lacks the role to perform the action |
| 404    | Org / project / cluster id doesn't exist or isn't visible to this key |
| 429    | Rate-limited — the HTTP client retries up to 3× automatically |

If you see persistent 403s, double-check the API key roles in **Access Manager → API Keys**.
