# Setup Instructions: HiBob

## Overview

The HiBob connector integrates your Bob HR tenant with the Shielva platform
via the HiBob REST API. Once connected, Shielva can list / search / read /
create / update employees, read employments, manage time-off requests, read
payroll history, watch lifecycle changes, and list departments + sites.

This connector authenticates with a **Service User** (HiBob's first-party
"server-to-server" credential) sent as HTTP Basic auth on every request.

---

## Prerequisites

- A **HiBob (Bob)** account with admin access.
- Permission to create a **Service User** under **Settings -> Integrations**.
- The roles/scopes you want to grant the Service User (People R/W, Time-off
  R/W, Payroll R, Lifecycle R, Reports R).

---

## Step 1: Create a Service User in HiBob

1. Sign in to https://app.hibob.com as an admin.
2. Open **Settings -> Integrations -> Service Users**.
3. Click **New service user**.
4. Give it a recognisable name — e.g. `Shielva Integration`.
5. Grant the minimum permissions for your use case:
   - **People** (Read/Write)
   - **Time-off** (Read/Write)
   - **Payroll** (Read)
   - **Lifecycle** (Read)
   - **Reports** (Read) if you want to list saved reports
6. **Save**. HiBob displays the **Service User ID** (looks like `SERVICE-12345`)
   and the **Service User Token** exactly once — copy both immediately.

> **Important:** the Service User Token is shown only at creation. If you lose
> it, you must rotate the Service User in Bob.

---

## Step 2: Service User ID (`service_user_id`) — **Required**

Paste the **Service User ID** (e.g. `SERVICE-12345`) into the **Service User
ID** field in Shielva. It is sent as the HTTP Basic **username**.

---

## Step 3: Service User Token (`service_user_token`) — **Required**

Paste the **Service User Token** into the **Service User Token** field in
Shielva. It is sent as the HTTP Basic **password**.

---

## Step 4: Base URL (`base_url`) — Optional

Defaults to `https://api.hibob.com/v1`. Override only if your Bob tenant lives
behind a private endpoint (rare).

---

## Step 5: Rate Limit (`rate_limit_per_min`) — Optional

Defaults to `60`. HiBob enforces its own quotas; this setting governs the
connector's client-side soft cap.

---

## Verification

After saving the config, the connector runs **health_check** automatically. A
green status means the Service User credentials are valid and the Bob API is
reachable. A red status with "401 Unauthorized" or "403 Forbidden" means the
Service User was disabled or lacks one of the scopes selected in Step 1 —
re-open it in Bob, grant the missing scope, and re-run health check.
