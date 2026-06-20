# Lever ATS Connector — Setup Guide

## Overview

The Lever ATS connector syncs your recruiting data — candidates (opportunities), job postings, users, interviews, offers, and pipeline stages — into Shielva's knowledge base via the Lever Data API v1.

Authentication uses **HTTP Basic Auth**: your API key is the username and the password is empty.

---

## Step 1: Generate a Lever API Key

1. Log in to your Lever account at [https://hire.lever.co](https://hire.lever.co).
2. Click your name/avatar in the top-right corner and select **Settings**.
3. In the left sidebar, navigate to **Integrations → API Credentials**.
4. Click **Generate New API Key**.
5. Enter a descriptive label (e.g. `Shielva Integration`) and click **Create**.
6. **Copy the API key immediately** — it is displayed only once and cannot be retrieved again.

---

## Step 2: Choose the right permissions

Lever API keys inherit the permissions of the generating user's account. For this connector:

| Permission level | What you get |
|-----------------|--------------|
| **Read-only (recommended)** | Access to all candidates, postings, users, interviews, offers, and stages for syncing. No ability to create or modify data. |
| **Read-write** | Full API access. Only needed if you intend to write back to Lever (e.g. creating opportunities). The Shielva sync connector only reads. |

Use a **read-only** Lever user account to generate the key if your organization allows it, minimizing security exposure.

---

## Step 3: Sandbox vs. Production

Lever provides separate environments:

| Environment | URL | Notes |
|-------------|-----|-------|
| **Production** | `https://hire.lever.co` | Live recruiting data |
| **Sandbox** | `https://sandbox.hire.lever.co` | Test data — does not affect production |

- Sandbox API keys and production API keys are **not interchangeable**. Generate a key from the environment you want to connect.
- The Lever Data API base URL for both environments is `https://api.lever.co/v1/`. The environment is determined by which key you use.

---

## Step 4: Install the Connector in Shielva

1. In the Shielva ACP, navigate to **Integrations → Lever ATS**.
2. Paste the API key you copied in Step 1 into the **API Key** field.
3. Click **Install**.
4. The connector validates credentials by calling `GET /users?limit=1`. On success, status shows **ONLINE**.

---

## Step 5: Verify the Sync

After installation, trigger a manual sync or wait for the scheduled sync interval:

- **Opportunities** (candidates) — all pipeline stages, owners, contact info, tags
- **Postings** (job listings) — department, team, location, state, apply URLs
- **Users** — team members, roles, email addresses
- **Interviews** — scheduled interviews, interviewers, opportunity links
- **Offers** — per-opportunity offer status

---

## Troubleshooting

### 401 Unauthorized
The API key is invalid or was copied incorrectly. Regenerate a new key at Lever → Settings → Integrations → API Credentials.

### 403 Forbidden
The user account that generated the key lacks access to the requested resource. Use an admin account's key or adjust the user's permissions in Lever → Settings → Users.

### 404 Not Found
A specific resource (opportunity or stage) no longer exists in Lever. This is non-fatal — the sync continues with remaining records.

### 429 Too Many Requests
Lever rate limits the API. The connector automatically retries with exponential backoff (up to 3 attempts). If persistent, reduce your sync frequency.

### Empty results
Verify the API key is for the correct Lever environment (production vs. sandbox). Also confirm that the Lever account has at least one user/posting/opportunity.

---

## Security

- The API key is stored encrypted in the Shielva vault and never written to logs or disk in plaintext.
- Rotate keys at Lever → Settings → Integrations → API Credentials: delete the old key and generate a new one, then re-install the connector in Shielva.
- Use a dedicated Lever service account (read-only) for the integration rather than a personal admin account.
