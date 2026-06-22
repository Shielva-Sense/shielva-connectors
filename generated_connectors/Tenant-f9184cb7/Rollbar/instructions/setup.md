# Rollbar Connector — Setup Guide

## Overview

This connector syncs your Rollbar error items, raw occurrences (instances), and
deploys into the Shielva knowledge base. It uses the
[Rollbar REST API v1](https://docs.rollbar.com/reference) with project access
token authentication passed as a query parameter (`?access_token=`).

---

## Prerequisites

- A Rollbar account at [rollbar.com](https://rollbar.com).
- At least **read** access to the project you want to sync.

---

## Step 1 — Create a Project Access Token

1. Log in to Rollbar and open the project you want to connect.
2. Go to **Settings → Project Access Tokens**.
3. Click **Create a new access token**.
4. Set the **Scope** to **read** (the connector only reads data).
5. Give the token a descriptive name (e.g. `shielva-connector-read`).
6. Click **Create** and **copy the token immediately** — it is shown only once.

---

## Step 2 — (Optional) Create an Account Access Token

An account-level token enables account-wide operations (e.g. listing all projects).
It is optional for this connector.

1. Go to **Account Settings → Account Access Tokens**.
2. Click **Add access token**.
3. Set the scope to **read** and save the token.

---

## Step 3 — Configure the Connector in Shielva

| Field | Value |
|---|---|
| **Project Access Token (read)** | The project read token from Step 1 |
| **Account Access Token** | (Optional) The account token from Step 2 |

---

## What Gets Synced

| Resource | Rollbar API endpoint |
|---|---|
| **Items (errors)** | `GET /api/1/items/` |
| **Occurrences** | `GET /api/1/instances/` |
| **Deploys** | `GET /api/1/deploys/` |
| **Project info** | `GET /api/1/project/` (health check only) |

Pagination uses page-offset (`?page=N`). Multiple pages are consumed per sync run.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Authentication failed (401)` | Token is invalid or expired | Re-create the project access token |
| `Authentication failed (403)` | Token lacks read scope | Ensure token scope is `read` |
| `resource 'X' not found (404)` | Wrong endpoint or item ID | Verify the item ID in Rollbar |
| `Rate limited (429)` | Too many API requests | The connector retries automatically with backoff |
| Connection refused | Network issue | Verify the connector host can reach `api.rollbar.com` |
