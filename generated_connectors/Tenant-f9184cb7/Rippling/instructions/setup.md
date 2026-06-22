# Rippling Connector — Setup Guide

## Overview

The Rippling connector syncs HR and organizational data from your Rippling account into the Shielva knowledge base. It fetches employees, departments, teams, roles, and leave requests.

---

## Generating a Rippling API Key

1. Log in to your Rippling account as an **Administrator** or **Super Admin**.
2. Navigate to **Settings** (gear icon) → **API** → **API Keys**.
3. Click **Create API Key**.
4. Give the key a descriptive name (e.g., `Shielva Integration`).
5. Select the required scopes (see below).
6. Click **Create** — the API key is shown only once. Copy it immediately.

---

## Required Permissions / Scopes

The API key must have **read** access to the following resources:

| Resource      | Scope / Permission          |
|---------------|-----------------------------|
| Employees     | `employees:read`            |
| Departments   | `departments:read`          |
| Teams         | `teams:read`                |
| Roles         | `roles:read`                |
| Leave Requests| `leaves:read`               |
| Company       | `company:read`              |

> **Minimum access:** If you only need employee data, `employees:read` + `company:read` is sufficient for install validation and basic sync.

---

## Rate Limits

Rippling enforces the following rate limits on the Platform API:

- **Default**: 100 requests per minute per API key.
- **429 Too Many Requests**: The connector automatically retries with exponential backoff, honouring the `Retry-After` header.
- **Bulk sync**: Initial full syncs for large organizations (1000+ employees) may take a few minutes.

---

## Configuration

When installing the connector in Shielva ACP, provide:

| Field   | Description                         | Required |
|---------|-------------------------------------|----------|
| API Key | Your Rippling Platform API key      | Yes      |

---

## Data Synced

| Resource      | Fields                                                                         |
|---------------|--------------------------------------------------------------------------------|
| Employees     | Name, email, job title, department, start date, employment type, status        |
| Departments   | Name, description, head count                                                  |
| Teams         | Name, description                                                              |
| Roles         | Name, description                                                              |
| Leaves        | Employee, type, start/end date, status                                         |

---

## Troubleshooting

| Error                  | Likely Cause                            | Fix                                          |
|------------------------|-----------------------------------------|----------------------------------------------|
| 401 Unauthorized       | Invalid or expired API key              | Regenerate the API key in Rippling Settings  |
| 403 Forbidden          | Missing scopes                          | Add the required read scopes to the API key  |
| 429 Rate Limited       | Too many requests                       | Connector will auto-retry; wait and retry    |
| 404 Not Found          | Endpoint not available for your plan    | Verify your Rippling plan includes API access|

---

## Rippling API Reference

- Base URL: `https://api.rippling.com/platform/api/`
- Documentation: [https://developer.rippling.com/docs](https://developer.rippling.com/docs)
- API Key management: Rippling → Settings → API
