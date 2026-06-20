# Splunk Connector — Setup Guide

## Overview

This connector integrates Splunk Enterprise or Splunk Cloud with Shielva to sync saved searches, indexes, apps, and users into the Shielva knowledge base. Authentication uses a Splunk Bearer token (session token or HTTP Event Collector token) sent as an `Authorization: Bearer` header on every REST API call.

---

## Step 1 — Generate a Splunk API Token

### Option A: Session Token (recommended for Splunk Enterprise)

1. Log in to your Splunk Web interface.
2. Navigate to **Settings → Tokens** (Splunk Enterprise 8.0+ / Splunk Cloud).
3. Click **New Token**.
4. Set an audience (e.g. `shielva-connector`) and an optional expiry date.
5. Click **Create**.
6. **Copy the token now** — it is only shown once.

### Option B: Service Account Session Token (via REST API)

```bash
curl -k -u admin:password \
  https://your-splunk-host:8089/services/auth/login \
  -d "username=admin&password=yourpassword" \
  --data-urlencode "output_mode=json"
```

Use the `sessionKey` value from the response as your token.

---

## Step 2 — Required Permissions

The token's associated user must have at minimum:

| Resource | Required capability |
|---|---|
| Server info / health | `list_settings` or any admin role |
| Indexes | `list_storage_passwords` or `indexes_list_all` |
| Saved searches | `schedule_search` (read) |
| Apps | `list_settings` |
| Users | `list_users` |

For least-privilege access, create a dedicated **read-only** service account with the built-in `user` role, or a custom role granting the capabilities above.

---

## Step 3 — Locate Your Splunk Management Port

The Splunk REST API is available on the **management port** (separate from the web UI port 8000).

- **Default management port**: `8089`
- Confirm by checking: **Settings → Server Settings → General Settings** → *Management port*
- For Splunk Cloud, the management port is typically `8089` or provided in your welcome email.

---

## Step 4 — SSL Certificate Considerations

By default the connector verifies SSL certificates. If your Splunk instance uses a self-signed certificate:

- Set **Verify SSL** to `false` in the connector installation form (development / private deployments only).
- For production, install a valid TLS certificate on your Splunk instance or add Splunk's CA to your trust store.

---

## Step 5 — Enter Credentials in Shielva

In the Shielva connector installation form, enter:

- **Splunk Host** — hostname or IP (e.g. `splunk.example.com` or `192.168.1.10`)
- **Management Port** — leave blank to use the default `8089`
- **API Token** — the Bearer token from Step 1
- **Default Index** — optional, e.g. `main` (used to scope ad-hoc searches)
- **Verify SSL** — leave blank or enter `true` to verify; enter `false` to skip (self-signed certs)

Click **Install** to validate and connect. The connector calls `GET /services/server/info` to confirm the token is valid before saving.

---

## What gets synced

| Resource | Endpoint | Notes |
|---|---|---|
| Saved searches | `GET /services/saved/searches` | All saved searches with SPL, schedule, and app context |
| Indexes | `GET /services/data/indexes` | All indexes — event counts, sizes, retention settings |
| Apps | `GET /services/apps/local` | All installed Splunk apps with version and status |

Ad-hoc search (`run_search`) is available as a direct call but is not part of the scheduled sync.

---

## Troubleshooting

**401 Unauthorized** — The token is invalid or has expired. Generate a new token in Settings → Tokens.

**403 Forbidden** — The token's user account lacks the required capabilities. Add the necessary read permissions to the user or role.

**Connection refused / network error** — Verify the host and port are correct and reachable from the Shielva runtime. Ensure the Splunk management port (8089) is not blocked by a firewall.

**SSL error** — If using a self-signed certificate, set **Verify SSL** to `false`, or install a valid certificate.

**Empty sync results** — Ensure the service account has permission to list indexes and saved searches. Check that the Splunk instance has at least one app and one index defined.
