# Auth0 Connector Setup Guide

## Overview

The Auth0 connector uses the **Auth0 Management API v2** with **OAuth 2.0 Machine-to-Machine (M2M)** client credentials to sync users, roles, applications, connections, and audit log events from your Auth0 tenant.

---

## Prerequisites

- An Auth0 account with an active tenant
- Admin access to the Auth0 Dashboard

---

## Step 1: Create a Machine-to-Machine Application

1. Log in to the [Auth0 Dashboard](https://manage.auth0.com/).
2. Navigate to **Applications** → **Applications** in the left sidebar.
3. Click **+ Create Application**.
4. Enter a name (e.g. `Shielva Sync`) and select **Machine to Machine Applications**.
5. Click **Create**.

---

## Step 2: Authorize the Application for the Management API

After creating the M2M application, Auth0 prompts you to select an API:

1. From the **Select an API** dropdown, choose **Auth0 Management API**.
2. Under **Permissions**, grant the following scopes:
   - `read:users`
   - `read:roles`
   - `read:clients`
   - `read:connections`
   - `read:logs`
3. Click **Authorize**.

> **Tip:** You can always update permissions later from the application's **APIs** tab.

---

## Step 3: Collect Your Credentials

On the application's **Settings** tab, copy the following values:

| Field | Where to find it |
|---|---|
| **Domain** | **Domain** field — format: `yourapp.auth0.com` or `yourapp.us.auth0.com` (EU/AU tenants use regional suffixes) |
| **Client ID** | **Client ID** field |
| **Client Secret** | **Client Secret** field (click the eye icon to reveal) |

---

## Step 4: Verify Your Domain Format

Auth0 domain format rules:

- **US tenants**: `{your-tenant}.auth0.com`
- **EU tenants**: `{your-tenant}.eu.auth0.com`
- **AU tenants**: `{your-tenant}.au.auth0.com`
- **Custom domains**: Your custom domain (e.g. `auth.yourcompany.com`) — must match the tenant's custom domain setting

Do **not** include `https://` or a trailing slash. Enter only the bare domain, e.g. `myapp.auth0.com`.

---

## Step 5: Enter Credentials in Shielva

In the Shielva connector setup form, enter:

- **Auth0 Domain**: `myapp.auth0.com`
- **Client ID**: the value from Step 3
- **Client Secret**: the value from Step 3

The connector will:
1. Exchange your credentials for a Management API access token via `POST https://{domain}/oauth/token` with `audience=https://{domain}/api/v2/`.
2. Cache the token (valid 24 hours) and auto-refresh before expiry.
3. Sync users, roles, clients, connections, and logs.

---

## Permissions Reference

| Permission | Endpoint accessed |
|---|---|
| `read:users` | `GET /api/v2/users`, `GET /api/v2/users/{id}` |
| `read:roles` | `GET /api/v2/roles` |
| `read:clients` | `GET /api/v2/clients` |
| `read:connections` | `GET /api/v2/connections` |
| `read:logs` | `GET /api/v2/logs` |

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `access_denied` on token request | M2M app not authorized for Management API | Re-do Step 2 |
| `insufficient_scope` on API call | Missing permission scope | Add the missing scope in the API authorization panel |
| `401 Unauthorized` | Wrong Client ID or Client Secret | Re-copy credentials from the Dashboard |
| Domain format error | Included `https://` or trailing `/` | Enter the bare domain only |
| `403 Forbidden` on `/api/v2/clients` | `read:clients` scope not granted | Add scope in Dashboard → Applications → {Your App} → APIs |
