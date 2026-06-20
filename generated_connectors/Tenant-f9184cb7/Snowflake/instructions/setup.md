# Snowflake Connector — Setup Guide

## Overview

The Shielva Snowflake connector syncs database metadata (databases, schemas, tables) from your Snowflake account into the Shielva knowledge base using the Snowflake SQL API v2. It authenticates via username + password and obtains a session token, which is cached and refreshed automatically.

---

## 1. Account Identifier Format

Your Snowflake **account identifier** is the value you enter in the `account` field. It appears in your Snowflake login URL:

```
https://<account_identifier>.snowflakecomputing.com
```

| Account type | Example identifier |
|---|---|
| Trial / standard | `myorg-account123` |
| Multi-region (legacy) | `abc12345.us-east-1` |
| Business Critical / VPS | `mycompany.privatelink` |

Do **not** include the `.snowflakecomputing.com` suffix — enter only the identifier portion.

---

## 2. Create a Dedicated Service Account

Create a dedicated Snowflake user for the connector with least-privilege access:

```sql
-- 1. Create a role for the connector
CREATE ROLE SHIELVA_CONNECTOR_ROLE;

-- 2. Create the service account user
CREATE USER SHIELVA_USER
  PASSWORD = 'YourStrongPassword123!'
  DEFAULT_ROLE = SHIELVA_CONNECTOR_ROLE
  DEFAULT_WAREHOUSE = COMPUTE_WH
  COMMENT = 'Shielva connector service account';

-- 3. Grant the role to the user
GRANT ROLE SHIELVA_CONNECTOR_ROLE TO USER SHIELVA_USER;

-- 4. Grant USAGE on databases to sync
GRANT USAGE ON DATABASE ANALYTICS TO ROLE SHIELVA_CONNECTOR_ROLE;
GRANT USAGE ON ALL SCHEMAS IN DATABASE ANALYTICS TO ROLE SHIELVA_CONNECTOR_ROLE;
GRANT REFERENCES ON ALL TABLES IN DATABASE ANALYTICS TO ROLE SHIELVA_CONNECTOR_ROLE;

-- Repeat for each database you want to sync:
-- GRANT USAGE ON DATABASE RAW TO ROLE SHIELVA_CONNECTOR_ROLE;
-- GRANT USAGE ON ALL SCHEMAS IN DATABASE RAW TO ROLE SHIELVA_CONNECTOR_ROLE;

-- 5. Grant usage on a warehouse for query execution
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE SHIELVA_CONNECTOR_ROLE;
```

---

## 3. Warehouse Setup

The connector uses a virtual warehouse for SQL execution (health checks and query history). If you do not specify a warehouse in the connector config, Snowflake will use the user's default warehouse.

Recommended configuration for the dedicated warehouse:

```sql
CREATE WAREHOUSE SHIELVA_WH
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60          -- suspend after 60 seconds idle
  AUTO_RESUME = TRUE
  COMMENT = 'Warehouse for Shielva connector — auto-suspends when idle';

GRANT USAGE ON WAREHOUSE SHIELVA_WH TO ROLE SHIELVA_CONNECTOR_ROLE;
```

An `XSMALL` warehouse is sufficient for the connector's metadata queries. Auto-suspend ensures no idle cost.

---

## 4. Enable the Snowflake SQL API

The Snowflake SQL API v2 is enabled by default on all supported Snowflake accounts (version 6.0+). No additional account-level flag is required. Verify your account version:

```sql
SELECT CURRENT_VERSION();
```

If your version is below 6.0, upgrade via your Snowflake account settings or contact Snowflake support.

---

## 5. Network Policy (Optional)

If your Snowflake account uses a **Network Policy** to restrict inbound IPs, add the Shielva egress IP range to the allowlist:

```sql
CREATE NETWORK POLICY SHIELVA_POLICY
  ALLOWED_IP_LIST = ('203.0.113.0/24')  -- replace with Shielva egress IPs
  COMMENT = 'Allow Shielva connector';

ALTER USER SHIELVA_USER SET NETWORK_POLICY = SHIELVA_POLICY;
```

Contact Shielva support for the current egress IP list.

---

## 6. Connector Config Fields

| Field | Required | Example | Notes |
|---|---|---|---|
| `account` | Yes | `myorg-account123` | Account identifier only — no `.snowflakecomputing.com` |
| `username` | Yes | `SHIELVA_USER` | Case-insensitive in Snowflake; stored as uppercase |
| `password` | Yes | `YourStrongPassword!` | Stored encrypted at rest by Shielva |
| `warehouse` | No | `COMPUTE_WH` | Defaults to user's default warehouse |
| `database` | No | `ANALYTICS` | Default database context for SQL execution |
| `role` | No | `SHIELVA_CONNECTOR_ROLE` | Activates the specified role for the session |

---

## 7. Troubleshooting

| Problem | Cause | Solution |
|---|---|---|
| `SnowflakeAuthError: Incorrect username or password` | Wrong credentials | Verify username/password in Snowflake. Reset password with `ALTER USER SHIELVA_USER RESET PASSWORD` if needed. |
| `SnowflakeAuthError: IP address is not allowed` | Network Policy restriction | Add the Shielva egress IP to the network policy allowlist. |
| `SnowflakeNotFoundError` on database | Role lacks USAGE privilege | Run `GRANT USAGE ON DATABASE <db> TO ROLE SHIELVA_CONNECTOR_ROLE`. |
| `SnowflakeRateLimitError` | Too many concurrent requests | Reduce sync frequency or increase the warehouse size. |
| Health check shows DEGRADED | Transient network issue | Retry the health check. Check if the Snowflake account URL is reachable from the Shielva network. |
| Session token expiry during long syncs | Token TTL is 1 hour (conservative) | The connector re-authenticates automatically when the token expires. |

---

## 8. Security Notes

- The password is stored encrypted at rest using AES-256-GCM within Shielva's credential vault.
- The connector uses `credentials: "include"` / session cookie on all API calls.
- No raw credentials are logged. The Shielva audit log records only `username` and `account` for correlation.
- For production deployments use a dedicated service account with a strong, randomly generated password rotated every 90 days.
