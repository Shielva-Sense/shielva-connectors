# Tableau Connector — Setup Guide

## Overview

The Tableau connector authenticates via a Personal Access Token (PAT) and syncs Workbooks, Views, and Datasources from Tableau Server or Tableau Cloud into your Shielva knowledge base using the Tableau REST API v3.21.

---

## Step 1: Generate a Personal Access Token

1. Sign in to your Tableau Server or Tableau Cloud instance.
2. Click your profile avatar (top right) → **My Account Settings**.
3. Scroll to the **Personal Access Tokens** section.
4. Click **Create a new token**.
5. Enter a memorable **Token Name** (e.g. `shielva-connector`).
6. Click **Create**.
7. Immediately copy both values — the **Token Name** and the **Token Secret** (the secret is only shown once).

---

## Step 2: Find Your Server URL

Your `server_url` is the base URL of your Tableau instance:

| Deployment | Example URL |
|---|---|
| Tableau Cloud (US West) | `https://10ax.online.tableau.com` |
| Tableau Cloud (US East) | `https://us-east-1.online.tableau.com` |
| Tableau Server (on-premise) | `https://tableau.yourcompany.com` |

Do **not** include a trailing slash or any path component.

---

## Step 3: Find Your Site Name

- **Tableau Server default site**: leave `site_name` blank (empty string).
- **Tableau Cloud / named site**: use the `contentUrl` value of your site, which appears in the URL after `/site/`. For example, if your URL is `https://10ax.online.tableau.com/#/site/mycompany/...`, your site name is `mycompany`.

---

## Step 4: Install the Connector in Shielva

When installing the connector in Shielva, provide these values:

| Field | Description |
|---|---|
| **Server URL** | Base URL of your Tableau Server / Cloud (see Step 2) |
| **Personal Access Token Name** | The token name you created in Step 1 |
| **Personal Access Token Secret** | The token secret copied in Step 1 |
| **Site Name** | Your site's `contentUrl` (leave blank for the default site) |

Click **Connect** — Shielva will call `sign_in()` to validate the credentials and store them securely.

---

## Step 5: Verify the Connection

After installation, Shielva runs `health_check()` automatically. A green status badge indicates the connector is **healthy** and the Tableau REST API is reachable.

---

## Data Synced

| Resource | Fields |
|---|---|
| Workbook | id, name, description, contentUrl, project.name, owner.name, createdAt, updatedAt |
| View | id, name, contentUrl, workbook.id, owner.name, createdAt, updatedAt |
| Datasource | id, name, description, contentUrl, type, project.name, owner.name, createdAt, updatedAt |

All resources are normalized to a `ConnectorDocument` with a stable 16-character ID (SHA-256 of `<type>:<tableau_id>`).

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Authentication failed` (401) | PAT expired, revoked, or wrong secret | Regenerate the PAT in Tableau → My Account Settings and update the connector |
| `Forbidden` (403) | The user account lacks API access | Grant the user **Site Administrator Explorer** or **Creator** role |
| `Not found` (404) | Site name does not exist or is misspelled | Verify the `contentUrl` for your site in Tableau Admin → Sites |
| Rate limited (429) | Too many API calls in the polling window | The connector has built-in exponential backoff and respects `Retry-After` |
| Connection timeout | Tableau Server is unreachable | Check network/firewall rules; verify the server URL is reachable from Shielva |
| Empty workbooks / views | PAT user has no access to site content | Assign the user to the relevant site with at least Viewer permissions |

---

## Required Tableau Permissions

The user account associated with the PAT must have:

- **Site Role**: Explorer (can publish) or higher, on the target site
- **Content Permissions**: Read access to Workbooks, Views, and Datasources you want to sync
- For Tableau Server with multiple sites, create a PAT on the account that has access to the target site

---

## API Reference

All calls use Tableau REST API v3.21:

- `POST /api/3.21/auth/signin` — PAT sign-in
- `POST /api/3.21/auth/signout` — sign out
- `GET /api/3.21/sites` — list sites
- `GET /api/3.21/sites/{site_id}/workbooks` — list workbooks (paginated)
- `GET /api/3.21/sites/{site_id}/views` — list views (paginated)
- `GET /api/3.21/sites/{site_id}/datasources` — list datasources (paginated)
- `GET /api/3.21/sites/{site_id}/users` — list users (paginated)
- `GET /api/3.21/sites/{site_id}/projects` — list projects
