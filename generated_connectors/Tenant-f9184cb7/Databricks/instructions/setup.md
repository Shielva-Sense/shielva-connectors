# Databricks Connector — Setup Guide

## Overview

This connector integrates Databricks with Shielva to sync your clusters, jobs, notebooks, MLflow experiments, registered models, and SQL warehouses. Databricks uses a **Personal Access Token (PAT)** for API authentication — a single bearer token tied to your workspace user account.

---

## Step 1 — Locate your Workspace URL

Your Databricks workspace URL is the base URL you see in the browser address bar when logged in, for example:

- Azure: `https://adb-123456789012345.1.azuredatabricks.net`
- AWS: `https://dbc-abcde123-4567.cloud.databricks.com`
- GCP: `https://1234567890123456.7.gcp.databricks.com`

Copy this full URL — you will need it in the Shielva installation form.

---

## Step 2 — Generate a Personal Access Token

1. Log in to your Databricks workspace.
2. Click your **username** in the top-right corner → **User Settings**.
3. In the left sidebar, click **Developer**.
4. Under **Access tokens**, click **Manage**.
5. Click **Generate new token**.
6. Enter a descriptive comment such as `shielva-connector` and set a lifetime (90 days recommended).
7. Click **Generate**.
8. **Copy the token now** — Databricks will only show it once.

> **Service principal alternative:** For production use, generate the token under a Databricks service principal (via the Accounts Console → Service principals → Generate PAT) to avoid tying the connector to a personal account.

---

## Step 3 — Required permissions

The token must belong to an account with read access to workspace resources. The built-in **Workspace Admin** or a custom group with the following permissions is sufficient:

| Resource | Required permission |
|---|---|
| Clusters | CAN READ or Can Restart |
| Jobs | CAN VIEW |
| Workspace (notebooks) | CAN READ |
| MLflow experiments | Read (via workspace file permissions) |
| MLflow registered models | Can Read |
| SQL warehouses | CAN USE |

For a principle-of-least-privilege setup, create a **group** with only the read permissions above and add the service principal to that group.

---

## Step 4 — Enter credentials in Shielva

In the Shielva connector installation form, enter:

- **Workspace URL** — the full URL from Step 1 (e.g. `https://adb-123456.azuredatabricks.net`)
- **Personal Access Token** — the token generated in Step 2

Click **Install** to validate. The connector calls `GET /api/2.0/preview/scim/v2/Me` using the token to confirm it is valid before saving.

---

## What gets synced

| Resource | API endpoint | Notes |
|---|---|---|
| Clusters | `GET /api/2.0/clusters/list` | All clusters (all-purpose + job clusters) |
| Jobs | `GET /api/2.1/jobs/list` | All jobs, paginated (25 per page) |
| Notebooks | `GET /api/2.0/workspace/list` | Top-level workspace objects |
| MLflow Experiments | `GET /api/2.0/mlflow/experiments/search` | All experiments (not synced to KB by default) |
| Registered Models | `GET /api/2.0/mlflow/registered-models/list` | All MLflow registered models |
| SQL Warehouses | `GET /api/2.0/sql/warehouses` | All SQL warehouses (Databricks SQL) |

The **sync** operation indexes clusters, jobs, and notebooks into the Shielva knowledge base. Experiments, models, and SQL warehouses are available via API methods but are not included in the default sync to keep the index focused.

---

## Troubleshooting

**403 Forbidden / Invalid token** — The PAT is invalid, has expired, or belongs to a user without sufficient workspace access. Regenerate the token or check group permissions.

**401 Unauthorized** — The token has been revoked. Generate a new token and reinstall the connector.

**404 on workspace/list** — The workspace URL is incorrect, or the path does not exist. Confirm the full URL from the browser address bar.

**Empty cluster list** — Ensure the user or service principal has at least CAN READ permission on the target clusters. Admins can verify this under **Clusters → Permissions**.

**No notebooks returned** — The root `/` path may be empty if notebooks are stored under user home directories or team folders. Use the `list_notebooks(path="/Shared")` method to query a specific path.

**Token expires** — Databricks PATs have a maximum lifetime of 730 days. Set a reminder to rotate the token before expiry to avoid a connector outage.
