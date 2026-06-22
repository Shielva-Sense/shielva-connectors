# Marketo Connector — Setup Guide

## Overview

The Marketo connector authenticates using **OAuth 2.0 Client Credentials** via a Marketo LaunchPoint Custom service. You will need three values: **Client ID**, **Client Secret**, and **Munchkin ID**.

---

## Step 1: Find your Munchkin ID

1. Log in to your Marketo instance.
2. Go to **Admin** (top navigation) → **Munchkin** (under the *Integration* section).
3. Copy the **Munchkin Account ID** — it looks like `abc-123-xyz` (three alphanumeric segments separated by hyphens).

This value is used to construct your instance's REST API base URL:
```
https://{munchkin_id}.mktorest.com/rest/v1/
```

---

## Step 2: Create a dedicated API-only user and role

Marketo requires an **API-only** user with a role that has REST API permissions. If you already have one, skip to Step 3.

1. Go to **Admin** → **Users & Roles** → **Roles** tab.
2. Click **New Role**.
3. Name it (e.g. `Shielva API Role`) and enable **Access API** under the *Permissions* section. At minimum enable:
   - Access API
   - Read-Only Lead
   - Read-Only List
   - Read-Only Campaign
   - Read-Only Activity
4. Click **Create**.
5. Go to the **Users** tab → **Invite New User**.
6. Check **API Only**. Assign the role you just created. Complete the invite flow.

---

## Step 3: Create a LaunchPoint Custom service

1. Go to **Admin** → **LaunchPoint** (under *Integration*).
2. Click **New** → **New Service**.
3. Set:
   - **Display Name**: `Shielva Connector` (or any label you prefer)
   - **Service**: `Custom`
   - **Description**: optional
   - **API Only User**: select the API-only user you created in Step 2
4. Click **Create**.

---

## Step 4: Retrieve Client ID and Client Secret

1. In the **LaunchPoint** list, find the service you just created.
2. Click **View Details**.
3. Copy the **Client ID** and **Client Secret**.

---

## Step 5: Enter credentials in Shielva

In the Shielva connector install form, enter:

| Field | Value |
|---|---|
| **Client ID** | Copied from LaunchPoint → View Details |
| **Client Secret** | Copied from LaunchPoint → View Details |
| **Munchkin ID** | Copied from Admin → Munchkin (e.g. `abc-123-xyz`) |

Shielva will exchange these for an OAuth access token automatically. Tokens expire every 3600 seconds and are refreshed transparently.

---

## REST API user role requirement

The API-only user associated with the LaunchPoint service **must** have a role that includes **Access API** permission. Without it, all API calls will return error code `601` (Unauthorized). The role must also have read permissions for each object type you want to sync (leads, lists, campaigns, programs).

---

## Token URL

```
GET https://{munchkin_id}.mktorest.com/identity/oauth/token
    ?grant_type=client_credentials
    &client_id={client_id}
    &client_secret={client_secret}
```

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `601 Unauthorized` | API-only user lacks **Access API** role permission | Add the permission in Admin → Users & Roles → Roles |
| `600 Access denied` | LaunchPoint service is disabled or deleted | Re-enable or recreate the service |
| `606 Rate limit exceeded` | Too many API calls per day (10,000 default) | Reduce sync frequency or request a limit increase |
| `Munchkin ID not found` | Wrong Munchkin ID format | Copy exact value from Admin → Munchkin |
| `invalid_client` error on token endpoint | Wrong client_id or client_secret | Re-copy from LaunchPoint → View Details |
