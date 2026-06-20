# ServiceNow Connector — Setup Guide

## Overview

The ServiceNow connector syncs incidents, change requests, users, and CMDB configuration items from your ServiceNow instance into Shielva. It uses the ServiceNow Table REST API with HTTP Basic authentication (username + password).

---

## Step 1 — Identify your ServiceNow instance name

Your instance name is the subdomain in your ServiceNow URL:

```
https://<instance>.service-now.com
```

For example, if your URL is `https://dev12345.service-now.com`, the instance name is `dev12345`.

---

## Step 2 — Create or identify a service account

For production use, create a dedicated service account:

1. Log in to ServiceNow as an administrator.
2. Go to **User Administration → Users**.
3. Click **New** to create a user.
4. Fill in **User ID**, **First name**, **Last name**, **Email**, and **Password**.
5. Assign the user the **itil** role (for incident and change read access) or a custom read-only role.
6. Click **Submit**.

> For development/evaluation, you can use your own admin credentials, but a dedicated service account is strongly recommended for production.

---

## Step 3 — Grant required permissions

The service account needs read access to these tables:

| Table | Required Role |
|-------|--------------|
| `incident` | `itil` or `admin` |
| `change_request` | `itil` or `change_manager` |
| `sys_user` | `admin` or custom ACL |
| `cmdb_ci` (and sub-classes) | `itil` or `asset` |

The built-in **itil** role covers incidents and change requests. For `sys_user` and CMDB, you may need to grant additional ACLs or use an admin account.

---

## Step 4 — Gather your install fields

| Field | Description | Example |
|-------|-------------|---------|
| **Instance Name** | Your ServiceNow instance identifier | `dev12345` |
| **Username** | ServiceNow username of the service account | `svc_shielva` |
| **Password** | Password for the service account | `P@ssw0rd!` |

---

## Step 5 — Install in Shielva ACP

1. In the Shielva ACP, navigate to **Integrations → Add Connector → ServiceNow**.
2. Fill in the three install fields: **Instance Name**, **Username**, **Password**.
3. Click **Install**. Shielva calls `GET /api/now/table/sys_user?sysparm_limit=1` to verify credentials.
4. On success, the connector status shows **ONLINE**.

---

## Required Permissions Summary

| Resource | Endpoint | Required Role |
|----------|----------|---------------|
| Health check | `GET /api/now/table/sys_user?sysparm_limit=1` | Any authenticated user |
| Incidents | `GET /api/now/table/incident` | `itil` |
| Single incident | `GET /api/now/table/incident/{sys_id}` | `itil` |
| Change requests | `GET /api/now/table/change_request` | `itil` or `change_manager` |
| Single change | `GET /api/now/table/change_request/{sys_id}` | `itil` or `change_manager` |
| Users | `GET /api/now/table/sys_user` | `admin` or custom ACL |
| CMDB items | `GET /api/now/table/{class_name}` | `itil` or `asset` |

---

## Troubleshooting

### 401 Unauthorized

**Cause:** Incorrect username or password.

**Fix:**
- Verify the username and password are correct.
- Ensure the account is not locked. Check **User Administration → Users** and unlock if needed.
- ServiceNow may lock accounts after multiple failed login attempts.

---

### 403 Forbidden

**Cause:** The user account lacks permission to read the requested table.

**Fix:**
- Assign the **itil** role to the service account for incident/change access.
- For `sys_user` table access, grant the **admin** role or create a custom ACL rule:
  `Table: sys_user | Operation: read | Condition: none`

---

### 404 Not Found

**Cause:** The instance name is incorrect, or a specific `sys_id` no longer exists.

**Fix:**
- Double-check the instance name — it is the part before `.service-now.com` in your URL.
- Verify the record exists in ServiceNow before calling `get_incident()` or `get_change()`.

---

### 429 Too Many Requests

**Cause:** ServiceNow enforces rate limits (varies by instance and plan).

**Fix:**
- The connector automatically retries with exponential backoff (up to 3 attempts).
- Consider reducing sync frequency if rate limits are consistently hit.
- Contact your ServiceNow administrator to increase rate limit allocations.

---

### Sync returns 0 documents

**Cause:** The service account may not have access to any incidents or change requests, or the tables are empty.

**Fix:**
- Log in to ServiceNow with the service account credentials directly to verify table access.
- Check that incidents exist by navigating to **Incident → All** in ServiceNow.
- Verify the `itil` role is assigned to the service account.

---

### `ModuleNotFoundError: No module named 'aiohttp'`

**Fix:** Install dependencies:
```bash
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
pip install aiohttp>=3.9.0
```

---

### Tests fail: `asyncio_mode` warning

**Fix:** Ensure `pytest.ini` contains:
```ini
[pytest]
asyncio_mode = auto
```
This is already configured in the connector root.
