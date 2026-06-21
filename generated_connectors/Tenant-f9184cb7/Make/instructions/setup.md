# Setup Instructions: Make (formerly Integromat)

## Overview

The Make connector integrates your Make account with the Shielva platform.
Once connected, Shielva can list and manage your organizations, teams,
scenarios, executions, and webhooks via the Make REST API (v2).

Make authenticates with a long-lived **API token** issued from your user
profile. Tokens never expire unless rotated, so you only need to set this up
once per environment.

---

## Prerequisites

- A **Make account** (free or paid) with access to the resources you want to
  manage.
- Your Make **zone** — the regional subdomain that appears in the URL after
  you log in (e.g. `eu2.make.com`, `us1.make.com`).
- Permission to create scenarios/webhooks in the teams you plan to use.

---

## Step 1 — Generate an API token

1. Log in to <https://www.make.com>.
2. Open the user menu (top-right) → **Profile**.
3. Switch to the **API** tab.
4. Click **Add token**.
5. Give the token a descriptive label (for example, `Shielva integration`).
6. Select the scopes your use case requires. At minimum, enable:
   - `organizations:read`
   - `teams:read`
   - `scenarios:read` and `scenarios:write`
   - `hooks:read` and `hooks:write`
7. Copy the generated token. Make will not show it again.

---

## Step 2 — Find your zone

Look at the URL of any Make page after you log in. The leading subdomain
(everything before `.make.com`) is your zone. Common values:

| URL prefix      | Zone value |
|-----------------|------------|
| `eu1.make.com`  | `eu1`      |
| `eu2.make.com`  | `eu2`      |
| `us1.make.com`  | `us1`      |
| `us2.make.com`  | `us2`      |

---

## Step 3 — (Optional) Note your default IDs

If you only intend to manage resources in one team or organization, you can
pre-fill the default IDs in the connector configuration. To find them:

- **Organization ID** — open your organization page; the trailing number in
  the URL `/organization/<id>` is the value.
- **Team ID** — open the team page; the trailing number in
  `/team/<id>` is the value.

---

## Step 4 — Install the connector in Shielva

When prompted by the Shielva connector installer, supply:

| Field                    | Value                                            |
|--------------------------|--------------------------------------------------|
| API Token                | The token from Step 1                            |
| Zone                     | The zone from Step 2 (e.g. `eu2`)                |
| Default Team ID          | *(optional)* from Step 3                         |
| Default Organization ID  | *(optional)* from Step 3                         |
| Rate limit (requests/min)| `60` (default — matches Make's standard quota)   |

The installer probes `GET /users/me` immediately and surfaces an error if
your token is rejected. A successful install reports
`AuthStatus.CONNECTED` + `ConnectorHealth.HEALTHY`.

---

## Troubleshooting

- **`401 Invalid token`** — the token was revoked or never had the scopes
  the connector needs. Re-issue the token with the scopes listed in Step 1.
- **Zone mismatch** — Make's API is regional. Calling `eu2` with a token
  issued in `us1` will silently return empty result sets. Re-check the URL
  in your browser after login.
- **429 Rate limit** — the connector backs off and retries automatically.
  If you hit this consistently, lower the rate-limit setting or split heavy
  workloads across multiple connector installs.
