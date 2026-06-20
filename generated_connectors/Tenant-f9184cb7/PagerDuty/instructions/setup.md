# PagerDuty Connector — Setup Guide

## Overview

This connector syncs **incidents, services, teams, schedules, users, and on-call assignments** from PagerDuty into Shielva. Authentication uses a PagerDuty API v2 token.

---

## Step 1 — Create a PagerDuty API Key

1. Log in to PagerDuty at [https://app.pagerduty.com](https://app.pagerduty.com).
2. Click your **avatar** (top-right corner) → select **My Profile**.
3. Go to the **User Settings** tab.
4. Scroll to the **API Access** section and click **Create New API Key**.
5. Enter a description such as `Shielva Connector`.
6. Select **Read-only** — the connector only reads data, never writes.
7. Click **Create Key**.
8. **Copy the token immediately** — PagerDuty only shows it once.

> If you need an account-level token (not tied to a specific user), an Admin can create one at **Configuration → API Access → Create New API Key**.

---

## Step 2 — Configure the Shielva Connector

| Field | Required | Description |
|---|---|---|
| **REST API Key** | Yes | The token you copied in Step 1. Stored encrypted at rest. |

---

## Step 3 — Verify

After saving, Shielva performs a health check by calling `GET /abilities`. A green status indicator confirms the token is valid and the connector is ready to sync.

---

## API Details

| Detail | Value |
|---|---|
| API base URL | `https://api.pagerduty.com/` |
| Auth header | `Authorization: Token token={your_api_key}` |
| Version header | `Accept: application/vnd.pagerduty+json;version=2` |
| Pagination | Offset-based (`limit` + `offset` + `more` boolean) |

---

## Rate Limits

PagerDuty enforces **60 API requests per minute** per token by default. The connector handles `429 Too Many Requests` responses automatically with exponential backoff. For large accounts with thousands of incidents, keep sync intervals above 5 minutes.

---

## Resources Synced

| Resource | API Endpoint | What is captured |
|---|---|---|
| Incidents | `GET /incidents` | Title, status, urgency, service, assignees, timestamps |
| Services | `GET /services` | Name, description, status, owning team |
| Teams | `GET /teams` | Name, description |
| Schedules | `GET /schedules` | Name, timezone, description |
| Users | `GET /users` | Name, email, role, job title, timezone |
| On-calls | `GET /oncalls` | Current on-call assignments |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Authentication failed (401)` | Token is invalid or was revoked | Regenerate the API key in PagerDuty and update the connector |
| `Authentication failed (403)` | Token lacks read permission | Use a Read-only token or ensure your user has the correct role |
| `Rate limited (429)` | Too many requests | The connector retries automatically; increase your sync interval if persistent |
| `Connection timeout` | PagerDuty API unreachable | Check network connectivity; the connector retries up to 3 times |

---

## Security Notes

- API keys are stored encrypted using Shielva's credential vault — never stored in plaintext.
- The connector requests **read-only** access. It never creates, updates, or deletes PagerDuty resources.
- Rotate the API key in PagerDuty if you suspect it has been compromised, then update the connector.
