# Setup Instructions: Personio (HR)

## Overview

The Personio connector integrates your organization's Personio HR account with the Shielva platform. Once connected, Shielva can list employees, read absences (time-offs) and attendances, file new absences, record attendances, and surface document metadata, projects, and custom attributes.

The connector authenticates with Personio's v1 REST API using a **Client ID + Client Secret** pair issued in the Personio admin panel. Each successful Personio response carries a fresh bearer token in the `Authorization` HTTP header — the connector rotates to that token automatically, so you never need to refresh credentials manually.

---

## Prerequisites

Before you begin, make sure you have:

- A **Personio account** with administrator privileges
- Permission to enable the **Personio REST API** and to create **API Credentials** in **Settings → Integrations → API Credentials**

---

## Step-by-Step Configuration

### Step 1: Personio Client ID (`client_id`) — **Required**

1. Sign in to your Personio admin panel at `https://<your-company>.personio.de`.
2. Open **Settings → Integrations → API Credentials**.
3. Click **Generate new credentials**.
4. Give the credentials a descriptive name (e.g. `Shielva connector`) and select the scopes you want to grant. At a minimum:
   - Employees: read, write
   - Time-offs: read, write
   - Attendances: read, write
   - Projects: read
   - Documents: read
5. Click **Generate**. Personio displays a **Client ID** and **Client Secret** — both are shown only **once**.
6. Copy the **Client ID** value and paste it into the **Personio Client ID** field in Shielva.

---

### Step 2: Personio Client Secret (`client_secret`) — **Required**

1. From the same dialog as Step 1, copy the **Client Secret** value.
2. Paste it into the **Personio Client Secret** field in Shielva. This field is stored encrypted.

> **Common mistake:** if you close the Personio dialog before copying the secret, you must delete the credentials and generate a new pair. The secret cannot be retrieved later.

---

### Step 3: Partner ID (`partner_id`) — **Optional**

- **Default:** `SHIELVA`
- Leave blank unless Personio support has issued your tenant a custom partner identifier. The connector sets the `X-Personio-Partner-ID` header to this value on every request.

---

### Step 4: App ID (`app_id`) — **Optional**

- **Default:** `shielva-connector`
- Leave blank to use the Shielva default. The connector sets the `X-Personio-App-ID` header to this value, which Personio uses for audit attribution.

---

### Step 5: Personio API Base URL (`base_url`) — **Optional**

- **Default:** `https://api.personio.de/v1`
- Leave blank. Override only if Personio has migrated your tenant to a regional endpoint.

---

### Step 6: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default:** `60` requests per minute
- Personio's standard tier permits 60 req/min per credential. Increase only if your account has a documented quota uplift.

---

## Completing the Authentication

After saving your credentials, click **Connect** in the Shielva connector dashboard. Shielva will:

1. POST `client_id` + `client_secret` to `https://api.personio.de/v1/auth`.
2. Read the `Authorization` response header (`Bearer <jwt>`) and cache the token.
3. Verify connectivity by calling `GET /company/employees?limit=1`.

If the health check succeeds, the connector status badge turns **Connected**. On every subsequent Personio call the connector rotates its cached token to the new value Personio returns — no manual refresh required.

---

## Testing the Connection

1. Click **Run Health Check** — the connector calls `GET /company/employees?limit=1` and reports OK if reachable.
2. Click **Sync Now** — the connector pages through `GET /company/employees` and ingests each record.
3. To exercise writes, open the API tester for `create_absence`, fill in a known `employee_id`, `time_off_type_id`, `start_date`, and `end_date`, and click **Run**. A `201` response with the new absence ID confirms write permissions.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on `authenticate` | Wrong Client ID/Secret, or credentials revoked in Personio | Regenerate credentials in Personio (Step 1) and re-enter both values |
| `401 Unauthorized` on subsequent calls | Cached token went stale before rotation | The connector re-runs `/auth` automatically on a single 401; if it persists, click **Re-authorize** |
| `403 Forbidden` on `create_absence` | The API credential lacks the **Time-offs: write** scope | Edit the credential in Personio (Step 1) and add the missing scope |
| `404 Not Found` on `get_employee` | The numeric employee ID does not exist | Verify the ID with `list_employees` first |
| `429 Too Many Requests` on sync | Quota exceeded | The connector retries with exponential backoff; lower sync frequency or request a quota uplift from Personio |
| Connector shows **Missing Credentials** | `client_id` or `client_secret` blank | Fill in both required fields and click **Save** |
