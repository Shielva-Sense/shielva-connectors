# LaunchDarkly Connector — Setup Guide

## Overview

The LaunchDarkly connector syncs feature flags, projects, environments, members, and audit log entries from the LaunchDarkly REST API v2 into Shielva's knowledge base.

**Auth type:** API Key (single key, no OAuth)
**API version:** `20220603` (sent as `LD-API-Version` header on every request)
**Auth header:** `Authorization: {api_key}` — **no "Bearer" prefix**

---

## Step 1 — Create a LaunchDarkly Personal Access Token

1. Log in to [app.launchdarkly.com](https://app.launchdarkly.com)
2. Click your avatar in the bottom-left corner → **Account settings**
3. Navigate to **Authorization** in the left-hand menu
4. Under **Personal access tokens**, click **+ Token**
5. Enter a descriptive name (e.g. `shielva-sync`)
6. Set the **Role** to **Reader** — this grants read-only access to all resources and is sufficient for the connector
   - For custom roles, ensure the token has `viewProject`, `readFlag`, `readEnvironment`, `viewMembers`, and `viewAuditLog` permissions
7. Set an expiry date (recommended: 1 year)
8. Click **Save token**
9. **Copy the token immediately** — LaunchDarkly only shows it once

> **Note:** Service tokens (account-level tokens without an associated member) are also supported via the same Authorization header format. These are preferred for production integrations.

---

## Step 2 — Enter the API key in Shielva

In the Shielva connector setup wizard:

| Field     | Value                                      |
|-----------|--------------------------------------------|
| API Key   | The token you copied in Step 1             |

No other fields are required.

---

## Authentication details

LaunchDarkly uses a **raw API key** (not Bearer token). The connector sets:

```
Authorization: <your-api-key>
LD-API-Version: 20220603
```

Do **not** add "Bearer " before the key — LaunchDarkly will reject the request with a 401 if you do.

---

## Resources synced

| Resource       | LaunchDarkly endpoint                            | Notes                                       |
|----------------|--------------------------------------------------|---------------------------------------------|
| Projects       | `GET /api/v2/projects`                           | All projects in the account                 |
| Feature flags  | `GET /api/v2/flags/{projectKey}`                 | All flags per project, paginated             |
| Environments   | `GET /api/v2/projects/{projectKey}/environments` | All environments per project                |
| Members        | `GET /api/v2/members`                            | All account members, paginated               |
| Audit log      | `GET /api/v2/auditlog`                           | Recent audit entries, cursor-paginated       |

---

## Permissions required

The **Reader** built-in role is sufficient for all resources above. If using a custom role, the token needs:

- `viewProject` on all projects
- `readFlag` on all flags
- `readEnvironment` on all environments
- `viewMembers` (account-level)
- `viewAuditLog` (account-level)

---

## Troubleshooting

| Error                | Cause                                        | Fix                                                    |
|----------------------|----------------------------------------------|--------------------------------------------------------|
| 401 Unauthorized     | Token is invalid or has been revoked         | Generate a new token in Account Settings → Authorization |
| 403 Forbidden        | Token lacks required permissions             | Use a Reader role token or add missing permissions      |
| 429 Too Many Requests| Rate limit hit                               | The connector retries automatically with backoff        |
| 404 Not Found        | Project or flag key does not exist           | Verify the resource exists in your LaunchDarkly account |

---

## API version

This connector targets **LaunchDarkly REST API version `20220603`**. This is sent via the `LD-API-Version` header on every request. See [LaunchDarkly API versioning docs](https://apidocs.launchdarkly.com/#section/Overview/Versioning) for details.
