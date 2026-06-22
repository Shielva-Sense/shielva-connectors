# SugarCRM Connector — Setup

This connector talks to SugarCRM's REST API v11. It supports two OAuth2 grants:

| Grant                | When to use                                       |
|----------------------|---------------------------------------------------|
| `password`           | On-prem / Sugar Sell / Sugar Serve self-hosted    |
| `authorization_code` | SugarCloud or any Sugar OAuth application with a redirect URI |

You only need ONE of them per install.

## 1. Find your SugarCRM Site URL

Open SugarCRM in your browser and copy the URL up to the first `/` after the
host. Examples:

- `https://acme.sugarondemand.com`
- `https://sugar.internal.acme.com`
- `https://crm.acme.com:8443`

This becomes the `site_url` install field. The connector automatically appends
`/rest/v11` to reach the REST API and `/rest/v11/oauth2/token` to reach the
token endpoint.

## 2. Provision a service account (password grant)

In SugarCRM as an admin:

1. Go to **Admin → User Management → Create New User**.
2. Set User Type = **Regular User**.
3. Grant only the modules the connector will touch — at minimum **Contacts**,
   **Accounts**, **Opportunities**, **Leads**, **Meetings**.
4. Set a strong password and disable interactive login methods other than
   password.
5. Note the username and password — these are the `username` / `password`
   install fields.

Use the built-in `sugar` client ID; leave `client_secret` blank.

## 3. Provision an OAuth application (authorization_code grant)

In SugarCRM as an admin:

1. Go to **Admin → OAuth Keys → Create OAuth Key**.
2. Set OAuth Version = **OAuth 2.0**.
3. Client Type = **Confidential**.
4. Register the redirect URI exactly as Shielva will call it (e.g.
   `https://your-shielva-instance.example.com/oauth/callback`).
5. Save and copy the consumer key + consumer secret — these become
   `client_id` / `client_secret`.
6. Set `grant_type=authorization_code` and the `redirect_uri` install field.

## 4. Install fields summary

| Field                | Required when                  | Notes                                              |
|----------------------|--------------------------------|----------------------------------------------------|
| `site_url`           | always                         | `https://...` — no trailing slash needed           |
| `client_id`          | always                         | `sugar` (default) or your custom OAuth client ID   |
| `client_secret`      | custom OAuth client            | Blank for built-in `sugar` client                  |
| `username`           | password grant                 | SugarCRM service-account username                  |
| `password`           | password grant                 | SugarCRM service-account password (encrypted)      |
| `grant_type`         | optional (default `password`)  | `password` or `authorization_code`                 |
| `platform`           | optional (default `api`)       | Sugar's session-bucket key                         |
| `redirect_uri`       | authorization_code grant       | Must match the OAuth client registration exactly   |
| `rate_limit_per_min` | optional (default 60)          | Match your SugarCRM license tier                   |

## 5. Verifying the install

After install, run a health check from the Shielva gateway. The connector
calls `GET /me` — a 200 with `{"current_user": ...}` means the OAuth token is
live and the SugarCRM site is reachable.

## 6. Troubleshooting

| Symptom                                                  | Likely cause                                                  |
|----------------------------------------------------------|---------------------------------------------------------------|
| Install fails with `SugarCRM rejected credentials`       | Wrong `username` / `password`, or user is locked out          |
| Install fails with `SugarCRM unreachable`                | Wrong `site_url`, or upstream firewall blocking the call      |
| Healthcheck flips between healthy/unhealthy              | Sugar session expiring; check `expires_in` and refresh policy |
| `convert_lead` returns 400                               | `modules` payload missing required `last_name` on Contacts    |
| `create_opportunity` returns 400 `date_closed required`  | Pass an explicit `date_closed` (`YYYY-MM-DD`)                 |
| 429 errors                                               | Raise `rate_limit_per_min` (or your Sugar license tier)       |

## 7. Security notes

- Always provision a dedicated service account; never reuse a real user.
- Use the smallest module set you actually need.
- Sugar stores tokens server-side; the connector keeps the access token in
  memory and never logs the value.
- Rotate the service-account password and the OAuth client secret on the
  schedule mandated by your SOC 2 / ISO 27001 program.
