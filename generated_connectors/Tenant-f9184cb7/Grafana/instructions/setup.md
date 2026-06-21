# Setup Instructions: Grafana

## Overview

The Grafana connector integrates a Grafana instance — Grafana Cloud or self-hosted — with the Shielva platform. Once connected, Shielva can read and manage dashboards, folders, datasources, users, teams, and alert rules, and can run datasource queries on your behalf.

The connector authenticates via a **Service Account token** — a long-lived API credential scoped to a single Grafana organization. You will mint this token inside Grafana and paste it into Shielva once.

---

## Prerequisites

- A Grafana instance you can reach over HTTPS (e.g. `https://myorg.grafana.net`) or HTTP on your private network (e.g. `http://localhost:3000`)
- An Admin or Editor role on that Grafana org — required to create a Service Account
- Grafana **10.2+** for the Service Accounts UI (older versions use API Keys; the same token field works for either)

---

## Step-by-Step Configuration

### Step 1: Grafana Instance URL (`instance_url`) — **Required**

The base URL of your Grafana installation, without a trailing slash.

- Grafana Cloud: `https://<stack-name>.grafana.net`
- Self-hosted: `https://grafana.example.com` (or `http://localhost:3000` for local dev)

Paste this into the **Grafana Instance URL** field in Shielva.

---

### Step 2: Service Account Token (`service_account_token`) — **Required**

1. In Grafana, open **Administration → Service Accounts**.
2. Click **Add service account**.
3. Give it a name (e.g. `shielva-connector`) and choose a role:
   - **Viewer** — read-only sync (dashboards, folders, datasources, alert rules, users, teams)
   - **Editor** — read + create/update dashboards, folders, datasources
   - **Admin** — full access (required if you intend to call `create_datasource` against secure datasources or manage org-level settings)
4. Click **Create**. On the service account detail page, click **Add service account token**.
5. Give the token a display name (e.g. `shielva-prod`), pick an expiration (recommended: 1 year or **No expiration** with a calendar reminder to rotate), and click **Generate token**.
6. **Copy the token immediately** — Grafana shows it only once. It starts with `glsa_`.
7. Paste it into the **Service Account Token** field in Shielva. This field is stored encrypted.

---

### Step 3: Org ID (`org_id`) — **Optional**

- **Default:** `1` (Grafana's default org)
- Multi-org Grafana installations: enter the numeric ID of the org you want to scope the connector to. The service account token is already org-scoped, so this field is informational unless you operate cross-org tooling.

---

### Step 4: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default:** `300` requests per minute
- Grafana Cloud applies a global per-stack limit; self-hosted has no inherent limit but downstream datasources may. Lower this if you observe `429` responses during sync.

---

## Completing the Connection

1. After saving the fields above, click **Connect** in the Shielva connector dashboard.
2. Shielva calls `GET /api/health` on your Grafana instance to verify the token and reachability.
3. The connector status badge should show **Connected** (green).

---

## Testing the Connection

| What | How |
|---|---|
| Token valid | Click **Run Health Check** — should return `database=ok`. |
| Read access | Open **APIs → list_dashboards** and click **Run**. You should see your dashboards. |
| Write access (Editor+) | Open **APIs → create_folder**, supply a title, and click **Run**. The folder should appear in Grafana. |
| Query passthrough | Open **APIs → query_datasource**, pass `datasource_id` from `list_datasources`, and a Prometheus expression like `up`. |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on health check | Token revoked / wrong instance URL | Re-mint the token (Step 2) and confirm `instance_url` matches the host where the SA lives |
| `404 Not Found` on health check | Wrong `instance_url` (extra path, trailing slash) | Remove any trailing `/` and any path after the host |
| `403 Forbidden` on `create_dashboard` | Service account role is Viewer | Promote the SA to Editor or Admin (Administration → Service Accounts) |
| `429 Too Many Requests` | Burst exceeded rate limit | Lower `rate_limit_per_min`; the connector retries automatically with backoff |
| `Connector shows Missing Credentials` | `instance_url` or `service_account_token` is blank | Fill both required fields and click **Save** |
| `Connection refused / network error` | Grafana not reachable from Shielva | Confirm firewall + DNS; for `localhost` URLs, make sure Shielva runs on the same host |
