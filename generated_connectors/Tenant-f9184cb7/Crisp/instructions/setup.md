# Crisp Connector — Setup

## 1. Get plugin credentials

1. Sign in to [Crisp Marketplace](https://marketplace.crisp.chat/).
2. Create a Plugin (or use an existing one).
3. Open the Plugin → **Tokens** tab.
4. Copy the `identifier` and `key`.

## 2. Configure required permissions

Inside the plugin manifest, request the scopes your use-case needs (read/write
on conversations, people, helpdesk, etc.). Crisp will fail requests with `401`
if the plugin does not have the required scope.

## 3. Install the connector in Shielva

Fill the install form:

| Field | Value |
|-------|-------|
| `identifier` | Plugin identifier from step 1 |
| `key` | Plugin key from step 1 |
| `tier` | `plugin` (default) or `user` |
| `default_website_id` | (optional) website UUID to use for `sync` |
| `base_url` | leave as `https://api.crisp.chat/v1` |
| `rate_limit_per_min` | `60` (default) |

The connector authenticates each request with HTTP Basic
(`Authorization: Basic base64(identifier:key)`) and the `X-Crisp-Tier`
header.

## 4. Verify

After install, run **Health Check** — it calls `GET /user/account`. A green
status confirms the credentials are working.

## 5. Sync (optional)

The `sync` API pulls helpdesk articles and conversations for
`default_website_id` into the configured KB. Schedule it as you would any
other Shielva connector sync.
