# PlanetScale Connector — Setup

The PlanetScale connector authenticates with a **service token** (not OAuth).
Service tokens are scoped to a single organization and can carry per-database
or per-branch permissions.

## 1. Mint a service token

1. Open <https://app.planetscale.com/> and sign in.
2. Choose **Settings → Service tokens** for the organization you want Shielva
   to manage.
3. Click **Create token**, give it a descriptive name (e.g. `shielva-connector`),
   and grant at least these access levels:
   - **Organization-level:** `read_organization`
   - **Database-level:** `read_database`, `create_database`, `delete_database`
     (omit `create_database`/`delete_database` if you only need read access)
   - **Branch-level:** `read_branch`, `create_branch`, `delete_branch`,
     `create_deploy_request`, `approve_deploy_request`, `create_password`,
     `delete_password` (again, omit what you do not need)
4. Copy both the **Token ID** and the **Token value** — the token value is shown
   only once.

## 2. Install the connector

In the Shielva UI install form, fill in:

| Field                  | Value                                          |
|------------------------|------------------------------------------------|
| **Service Token ID**   | the Token ID from step 1                       |
| **Service Token**      | the Token value from step 1                    |
| **Default Organization** _(optional)_ | the org slug, e.g. `my-org`        |
| **Default Database**   _(optional)_ | a database name to use by default  |
| **PlanetScale API Base URL** _(optional)_ | leave blank to use the default |
| **Rate Limit (requests/min)** _(optional)_ | leave at `100`              |

Click **Install**. The connector validates the credentials and stores them.

## 3. Verify

Run the **Health Check** action. A healthy connector returns:

```
health: healthy
auth_status: connected
message: "PlanetScale API reachable"
```

If you see `invalid_credentials`, the token is missing or revoked — repeat
step 1 with a fresh token.

## 4. Common operations

| Action                | What it does                                      |
|-----------------------|---------------------------------------------------|
| `list_organizations`  | All orgs visible to the service token             |
| `list_databases`      | Databases in an org                               |
| `create_database`     | Provision a new database (`plan` defaults `hobby`) |
| `list_branches`       | Branches of a database                            |
| `create_branch`       | Fork from `parent_branch` (defaults `main`)       |
| `create_deploy_request` | Open a schema deploy from a branch into `main`  |
| `deploy_deploy_request` | Apply an approved deploy request                |
| `create_password`     | Mint a connection password (returned **once**)    |

## 5. Security notes

- The service token value is treated as a secret and stored encrypted by the
  Shielva vault sidecar (port 8054 in dev) — never in plaintext.
- Passwords minted via `create_password` are returned **only once** in
  `plain_text` and must be persisted by the caller immediately.
- Rotate the service token every 90 days. PlanetScale supports multiple
  active tokens, so create the replacement, swap it in, then delete the old
  one — no downtime required.
